"""OnlyOffice executor — serves the office domain.

Uses the OnlyOffice Document Server HTTP APIs with JWT (HS256) auth:
``/ConvertService.ashx`` for conversion and ``/coauthoring/CommandService.ashx``
for commands. WOPI and document callbacks are inbound-only and are deferred
honestly; docbuilder/editor editing flows are not wired and are deferred.

Live-verified against the running documentserver: version command returned
9.4.0.129 and a hosted docx converted to a real PDF (FileResult + download
checked with a ``%PDF-`` header).
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import math
import os
import re
from typing import Any

from server.execution.executors.base import (
    DomainExecutor,
    ExecutorError,
    ExecutionContext,
    action_result,
    deferred_action,
    executor_result,
)
from server.execution.executors.http import HttpClient
from server.text_prose import clean_prose as _clean_prose, is_topic_fragment as _is_topic_fragment

CONVERT_ACTIONS = {"office.converter.convert", "office.editor.download_as"}
COMMAND_ACTIONS = {"office.command.version", "office.command.info", "office.command.meta",
                   "office.command.drop", "office.command.forcesave"}
CREATE_ACTIONS = {"office.editor.create"}
EDIT_ACTIONS = {"office.editor.edit_content", "office.editor.format_content", 
                "office.editor.add_title", "office.editor.insert_image",
                "office.editor.insert_table", "office.editor.remove_image",
                "office.editor.add_content", "office.editor.remove_table",
                "office.editor.modify_table"}
_COMMAND_KIND = {"office.command.info": "info", "office.command.meta": "meta",
                 "office.command.drop": "drop", "office.command.forcesave": "forcesave"}
_DEFERRED_SUFFIXES = ("callback.", "wopi.", "docbuilder.", "editor.")
_NEEDS_URL = {"office.editor.mail_merge"}
_CELL_EXTENSIONS = ("xlsx", "xls", "ods", "csv")


def _read_cell_sheet(file_path: str) -> tuple[list[str], list[list[str]]]:
    """Read a spreadsheet back as (headers, data rows) of stripped strings."""
    import openpyxl
    from server.execution.executors.file_storage import _full

    path = _full(file_path)
    if not path.exists():
        return [], []
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        ws = wb.active
        rows = []
        for row in ws.iter_rows(values_only=True):
            cells = ["" if c is None else str(c).strip() for c in row]
            rows.append(cells)
    finally:
        wb.close()
    rows = [r for r in rows if any(r)]
    if not rows:
        return [], []
    return rows[0], rows[1:]


def _cell_sig(row: list[str]) -> tuple[str, ...]:
    return tuple(str(c).strip() for c in row)


def _cell_contains_rows(file_path: str, rows_data: list[list[str]]) -> bool:
    """True when every provided data row is already present in the sheet.

    Powering an idempotency guard: retries / repeated requests must not append
    the same row twice.
    """
    if not rows_data:
        return False
    _, data = _read_cell_sheet(file_path)
    for wanted in rows_data:
        w_sig = _cell_sig(wanted)
        if not any(_cell_sig(r)[: len(w_sig)] == w_sig for r in data):
            return False
    return True


def _describe_cell_append(file_path: str, rows_data: list[list[str]]) -> str:
    """Human description of the appended rows (with 1-based row numbers)."""
    _, data = _read_cell_sheet(file_path)
    lines = []
    for wanted in rows_data:
        w_sig = _cell_sig(wanted)
        row_num: int | None = None
        for idx, row in enumerate(data, start=2):
            if _cell_sig(row)[: len(w_sig)] == w_sig:
                row_num = idx  # keep LAST match = the appended row
        label = " | ".join(str(c) for c in wanted)
        lines.append(f"Ligne {row_num} ajoutée : {label}" if row_num else f"Ligne ajoutée : {label}")
    return " ; ".join(lines)


def _as_list(value: Any, split: bool = False) -> list[str]:
    """Normalise a value to a list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        if split:
            parts = [p.strip() for p in re.split(r"[,;|]+", value) if p.strip()]
            return parts or [value.strip()]
        return [value.strip()]
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            norm = _as_list(item, split=True)
            out.extend(norm)
        return out
    return [str(value)]


def _build_format_request(params: dict[str, Any]) -> str:
    """Reconstruct a natural-language edit request from classified format fields.

    build_edit_script matches on keywords, so we emit French phrases it
    recognises for bold / italic / underline / colour / size / alignment.
    """
    parts: list[str] = []
    fmt = str(params.get("text_format") or "").lower()
    style = str(params.get("text_style") or "").lower()
    color = str(params.get("text_color") or "").lower()
    size = params.get("text_size")
    target = str(params.get("target_text") or "").strip()
    obj = f" le texte {target}" if target else " le text"

    if fmt:
        if any(k in fmt for k in ["gras", "bold"]):
            parts.append(f"mets{obj} en gras")
        elif any(k in fmt for k in ["italique", "italic"]):
            parts.append(f"met{obj} en italique")
        elif any(k in fmt for k in ["soulign", "underline", "souligner"]):
            parts.append(f"souligne{obj}")
    if style:
        if any(k in style for k in ["centr", "center", "centre"]):
            parts.append(f"aligne{obj} au centre")
        elif any(k in style for k in ["justif", "justify"]):
            parts.append(f"justifie{obj}")
        elif any(k in style for k in ["gauche", "left"]):
            parts.append(f"aligne{obj} à gauche")
        elif any(k in style for k in ["droite", "right"]):
            parts.append(f"aligne{obj} à droite")
    if color:
        parts.append(f"met{obj} en {color}")
    if size:
        parts.append(f"met{obj} en taille {size}")
    return " et ".join(parts)


def _is_generic_placeholder(text: str) -> bool:
    """True when a classified 'text' is a vague placeholder rather than real content.

    e.g. "le résumé", "le text", "le texte à ajouter", "du contenu", "le contenu".
    """
    if not text:
        return True
    t = text.lower().strip()
    if len(t) <= 12:
        return True
    # A repeated placeholder line (e.g. "du contenu\n du contenu") is still a
    # placeholder even though it is longer than the short-text threshold.
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    if lines:
        first = lines[0]
        if all(ln == first for ln in lines):
            t = first
    return any(
        phrase in t
        for phrase in [
            "le résumé", "le resume", "le texte à ajouter", "le text à ajouter",
            "le contenu", "le text", "le texte", "le résumé du fichier",
            "le resume du fichier", "texte à insérer", "text à insérer",
            "du contenu", "du texte", "un contenu", "un texte", "le paragraphe",
            "le paragraph", "nouveau paragraphe", "nouvelle paragraph",
            "nouveau contenu", "nouvelle section", "a ajouter", "à ajouter",
        ]
    )


def _extract_answer_text(data: dict[str, Any]) -> str:
    """Pull a human-readable text blob from an executor action's data dict.

    Handles general (answer), rag (response.answer / response), odoo and any
    other domain that surfaced a meaningful free-text result, by walking the
    dict and preferring known textual keys before falling back to the longest
    string value.
    """
    if not isinstance(data, dict):
        return ""
    for key in ("answer", "text", "content", "summary", "resume", "result"):
        v = data.get(key)
        if isinstance(v, str) and v.strip() and len(v.strip()) > 20:
            return v.strip()
    resp = data.get("response")
    if isinstance(resp, dict):
        for key in ("answer", "text", "content", "summary", "resume"):
            v = resp.get(key)
            if isinstance(v, str) and v.strip() and len(v.strip()) > 20:
                return v.strip()
        for v in resp.values():
            if isinstance(v, str) and len(v.strip()) > 20:
                return v.strip()
    elif isinstance(resp, str) and resp.strip() and len(resp.strip()) > 20:
        return resp.strip()
    best = ""
    for v in data.values():
        if isinstance(v, str) and len(v.strip()) > len(best):
            best = v.strip()
    return best if len(best) > 20 else ""


def _prior_text_content(ctx: ExecutionContext) -> str:
    """Pull reusable free-text produced by an earlier domain's executor in this
    turn (resume, redaction, analysis, odoo info, rag answer, ...) so it can be
    injected into a document instead of a placeholder."""
    if not getattr(ctx, "executor_log", None):
        return ""
    for rec in reversed(ctx.executor_log):
        if str(rec.get("domain") or "").lower() == "office":
            continue  # office results are edit confirmations, never prose to inject
        for res in reversed(rec.get("results") or []):
            data = res.get("data") or {}
            if data.get("deferred") or not data.get("ok", True):
                continue
            text = _extract_answer_text(data)
            if text:
                return text
    return ""


_FR_NUM = {
    "zero": 0, "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5,
    "six": 6, "sept": 7, "huit": 8, "neuf": 9, "dix": 10, "onze": 11,
    "douze": 12, "treize": 13, "quatorze": 14, "quinze": 15, "seize": 16,
    "vingt": 20, "trente": 30, "quarante": 40, "cinquante": 50, "soixante": 60,
    "cent": 100, "mille": 1000,
}


def _to_int(value: Any) -> int | None:
    """Parse an int or a French number word into an int (e.g. 'deux' -> 2)."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        s = value.strip().lower().replace(",", ".")
        if re.fullmatch(r"-?\d+", s):
            return int(s)
        if s in _FR_NUM:
            return _FR_NUM[s]
        # handle simple compositions: "vingt deux" / "vingt-deux"
        parts = re.split(r"[\s\-]+", s)
        try:
            total = 0
            for p in parts:
                if p in _FR_NUM:
                    total += _FR_NUM[p]
                else:
                    raise ValueError(p)
            return total or None
        except ValueError:
            return None
    return None


def _parse_rows_data(value: Any, cols_num: int | None = None) -> list[list[str]]:
    """Normalise rows_data into a list of row lists of cell strings.

    Handles:
      - a nested list of rows: [["a1","b1"], ["a2","b2"]]
      - a flat list / string that should be reshaped into rows of `cols_num`
        columns when a column count is known: ["a1","a2","b1","b2"] with cols=2
        -> [["a1","a2"], ["b1","b2"]]
    """
    if not value:
        return []
    if isinstance(value, str):
        rows = [r.strip() for r in re.split(r"[;]|\n", value) if r.strip()]
        if not rows:
            rows = [value.strip()]
        return _parse_rows_data(rows, cols_num=cols_num)
    # Normalise the top-level to a list of items
    items: list[Any] = []
    for row in value:
        if isinstance(row, (list, tuple)):
            items.append(list(row))
        elif isinstance(row, str):
            cells = _as_list(row, split=True)
            items.append(cells if cells else [row])
        elif row is not None:
            items.append([str(row)])
    rows: list[list[str]] = []
    for item in items:
        rows.append(_as_list(item))
    # Reshape a flat single row of cells into cols_num-wide rows (e.g. the user
    # typed "1,2,3,4" for a 2-column table -> [["1","2"],["3","4"]]).
    if cols_num and cols_num > 1 and len(rows) == 1:
        flat = [c for r in rows for c in r if c]
        if len(flat) > cols_num and len(flat) % cols_num == 0:
            reshaped = [flat[i:i + cols_num] for i in range(0, len(flat), cols_num)]
            rows = [r for r in reshaped if any(r)]
    return rows


class OnlyOfficeExecutor(DomainExecutor):
    domains: tuple[str, ...] = ("office",)
    backend = "onlyoffice"

    def __init__(self) -> None:
        ds_port = os.environ.get("ONLYOFFICE_DS_PORT", "8088")
        app_host = os.environ.get("ONLYOFFICE_APP_HOST", "host.docker.internal")
        # Inside the container the Document Server is published on the HOST, so
        # 127.0.0.1 would point at this container. Prefer the internal URL.
        self.base_url = os.environ.get(
            "ONLYOFFICE_DS_INTERNAL_URL",
            os.environ.get("ONLYOFFICE_DS_URL", f"http://{app_host}:{ds_port}"),
        ).rstrip("/")
        self.secret = os.environ.get("ONLYOFFICE_JWT_SECRET", "")
        self._http = HttpClient(self.base_url)

    # ------------------------------------------------------------------
    @staticmethod
    def _b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    def _token(self, payload: dict[str, Any]) -> str:
        if not self.secret:
            raise ExecutorError("onlyoffice: ONLYOFFICE_JWT_SECRET not configured")
        header = self._b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        body = self._b64url(
            json.dumps(payload, separators=(",", ":")).encode()
        )
        signing = f"{header}.{body}".encode()
        sig = hmac.new(self.secret.encode(), signing, hashlib.sha256).digest()
        return f"{header}.{body}.{self._b64url(sig)}"

    def _document_url(self, params: dict[str, Any]) -> str | None:
        doc = params.get("document")
        url = params.get("url")
        if isinstance(doc, str) and doc.startswith(("http://", "https://")):
            return doc
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            return url
        return None

    # ------------------------------------------------------------------
    def execute(self, ctx: ExecutionContext) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for action, payload in ctx.actions.items():
            params = (payload or {}).get("fields", {})
            try:
                if action in CONVERT_ACTIONS:
                    data = self._convert(params, action)
                elif action in COMMAND_ACTIONS:
                    data = self._command(params, action)
                elif action in CREATE_ACTIONS:
                    data = self._create_action(params, action, ctx)
                elif action in EDIT_ACTIONS:
                    if self._edit_target_is_pdf(params, ctx):
                        results.append(deferred_action(
                            action, params,
                            "l'édition directe des PDF n'est pas encore supportée; convertissez le fichier en .docx",
                        ))
                        continue
                    data = self._edit_action(params, action, ctx)
                elif action.startswith(_DEFERRED_SUFFIXES) or action in _NEEDS_URL:
                    data = None
                else:
                    data = None
                if data is None:
                    results.append(deferred_action(action, params))
                    continue
                # Record the resolved params (e.g. the redaction output actually
                # injected) so the inspector reflects what reached the document;
                # otherwise fall back to the classifier's original fields
                # (image_url, target_doc, ...) so it reflects routing.
                resolved = data.pop("resolved_params", None) or params
                result_data = {"ok": True, "response": data}
                results.append(
                    action_result(
                        action, resolved,
                        tool_calls=[f"onlyoffice:{action}"],
                        data=result_data,
                    )
                )
            except ExecutorError as exc:
                results.append(
                    action_result(
                        action, params,
                        tool_calls=[f"onlyoffice:{action}"],
                        data={"ok": False, "error": str(exc)},
                    )
                )
        return executor_result(ctx.domain, results, attempts=ctx.retry_count + 1)

    # ------------------------------------------------------------------
    def _create_action(
        self,
        params: dict[str, Any],
        action: str,
        ctx: ExecutionContext | None = None,
    ) -> dict[str, Any]:
        """Create a brand-new Office document (docx/xlsx/pptx) via DocBuilder."""
        from server.webapp.docbuilder import create_document

        name = str(params.get("name") or params.get("filename") or params.get("document_name") or "").strip()
        content = str(params.get("content") or params.get("text") or params.get("content_text") or "").strip()
        folder = str(params.get("folder") or params.get("path") or "").strip().strip("/").strip("\\")
        fmt = str(params.get("format") or "").lower()

        seeded = False
        if not content and ctx is not None:
            # No prose in the fields: use what the earlier domains actually read.
            lines = _prior_read_lines(ctx)
            if lines:
                content = "\n".join(lines)
                seeded = True

        if "." not in name:
            if any(k in fmt for k in ["xls", "cell", "excel", "tableur"]):
                name = f"{name}.xlsx"
            elif any(k in fmt for k in ["ppt", "slide", "powerpoint", "presentation"]):
                name = f"{name}.pptx"
            else:
                name = f"{name}.docx"

        ext = name.rsplit(".", 1)[-1].lower() if "." in name else "docx"
        doc_type = "cell" if ext in ("xlsx", "xls", "csv") else ("slide" if ext in ("pptx", "ppt", "ppsx") else "word")

        result = create_document(doc_type, name, content_text=content, folder=folder or None)
        if not result["success"]:
            raise ExecutorError(f"onlyoffice: create failed: {result['message']}")

        return {
            "action": action,
            "file": result["file_path"],
            "folder": folder or "",
            "content_seeded": seeded,
            "response": f"Fichier créé: {result['file_path']}"
            + (" (contenu issu de vos disponibilités)" if seeded else ""),
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _edit_target_is_pdf(params: dict[str, Any], ctx: ExecutionContext | None) -> bool:
        """True when the targeted document is a PDF (needs docx conversion first)."""
        target = ""
        if ctx:
            for att in (ctx.attachments or []):
                if isinstance(att, dict) and att.get("_viito_file_path"):
                    target = att["_viito_file_path"]
                    break
        if not target:
            target = str(params.get("file") or params.get("target_doc") or "")
        return target.lower().rsplit(".", 1)[-1] == "pdf"

    # ------------------------------------------------------------------
    def _edit_action(self, params: dict[str, Any], action: str, ctx: ExecutionContext | None = None) -> dict[str, Any]:
        """Handle document editing actions via Document Builder."""
        from server.webapp.docbuilder import edit_document
        
        file_path = params.get("file") or params.get("target_doc") or ""
        
        # Always prefer file path from attachments (Viito session) over classified field
        saved_image_urls = []
        if ctx:
            for att in ctx.attachments:
                if isinstance(att, dict) and att.get("_viito_file_path"):
                    file_path = att["_viito_file_path"]
                if isinstance(att, dict) and att.get("_viito_image_url"):
                    saved_image_urls.append(att["_viito_image_url"])
        
        # Build request text from action-specific fields.
        # The docbuilder expects a natural-language instruction that matches
        # its keyword patterns (e.g. "titre" for heading, "image" for photo).
        insert = None
        if action in ("office.editor.edit_content", "office.editor.format_content"):
            built = _build_format_request(params)
            request_text = built or params.get("edit_request") or params.get("format_request") or params.get("request") or ""
        elif action == "office.editor.add_title":
            title = params.get("title_text") or params.get("title") or ""
            if title:
                request_text = f"ajoute un titre {title}"
            else:
                request_text = params.get("request") or "ajoute un titre"
        elif action == "office.editor.add_content":
            classified = str(params.get("text") or params.get("content_text") or "").strip()
            pos = str(params.get("position") or "").lower()
            at_beginning = any(k in pos for k in ["debut", "begin", "start"])
            intent = (ctx.intent if ctx else "") or ""
            prior = _prior_text_content(ctx)
            # The content that lands in the document is, in order of preference:
            #   1. the literal text the user supplied (quoted or explicit),
            #   2. the prose the general/redaction domain generated this turn,
            #   3. nothing -> fail honestly instead of writing the request's topic.
            if classified and not _is_generic_placeholder(classified) and not _is_topic_fragment(
                classified, intent
            ):
                text = classified
            elif prior:
                text = _clean_prose(prior)
            elif classified and not _is_generic_placeholder(classified):
                # Only the classifier's subject is available: writing it would put
                # a stub in the document, so let the validator re-route to general.
                text = ""
            else:
                text = ""
            if not text:
                raise ExecutorError(
                    "onlyoffice: no generated or explicit text was available for "
                    "add_content (no prose produced for this turn)"
                )
            # Record the text actually injected so the inspector shows the content
            # that landed in the document (redaction output or literal text), not
            # the classified topic.
            params = {**params, "text": text}
            insert = {"text": text, "at_beginning": at_beginning}
            request_text = params.get("request") or "ajoute du contenu"
        elif action == "office.editor.insert_image":
            img_url = params.get("image_url") or ""
            # Fall back to a saved attachment image URL if the classifier didn't resolve one
            if not img_url and saved_image_urls:
                img_url = saved_image_urls[0]
            request_text = f"insere une image {img_url}" if img_url else (params.get("request") or "insere une image")
        elif action == "office.editor.insert_table":
            spec = params.get("table_spec") or params.get("table_data") or ""
            if spec and not any(k in spec.lower() for k in ["tableau", "table", "grille", "ajoute", "insert"]):
                spec = f"ajoute un tableau de {spec}"
            request_text = spec or params.get("request") or "ajoute un tableau"
        elif action == "office.editor.modify_table":
            spec = params.get("table_spec") or params.get("table_data") or ""
            if spec and not any(k in spec.lower() for k in ["tableau", "table", "modifie", "modify", "grille"]):
                spec = f"modifie le tableau {spec}"
            request_text = spec or params.get("request") or "modifie le tableau"
        elif action == "office.editor.remove_table":
            request_text = params.get("request") or "supprime le tableau"
        elif action == "office.editor.remove_image":
            img = params.get("image") or ""
            request_text = f"supprime la photo du document" if not any(
                k in img.lower() for k in ["supprime", "delete", "remove"]
            ) else img
            request_text = request_text or params.get("request") or "supprime la photo du document"
        else:
            request_text = params.get("request") or ""
        
        if not file_path:
            raise ExecutorError("onlyoffice: file path required for edit action")
        
        # Build a structured table spec from classified fields (columns_titles / rows_data)
        # for table insert/modify so cells get populated (not just dimensions).
        table = None
        modify = None
        if action in ("office.editor.insert_table", "office.editor.modify_table"):
            headers = _as_list(params.get("columns_titles") or [], split=True)
            # User-requested additions to the EXISTING table (modify_table).
            user_cols = _to_int(params.get("cols_num")) or 0
            rows_num = _to_int(params.get("rows_num")) or 0
            raw = params.get("rows_data") or params.get("table_data") or ""
            raw_rows = _parse_rows_data(raw, cols_num=None)
            # Determine the column width used to reshape a flat cell list / string
            # into rows: prefer the header count, else derive from rows_num.
            reshape_cols = len(headers) if headers else None
            if not reshape_cols and rows_num and raw_rows:
                flat = [cell for row in raw_rows for cell in row]
                if flat:
                    reshape_cols = max(1, math.ceil(len(flat) / rows_num))
            rows_data = _parse_rows_data(raw, cols_num=reshape_cols) if reshape_cols else raw_rows
            if action == "office.editor.modify_table":
                # Keep the existing table; grow rows/cols only as needed to hold
                # the provided headers/data + any explicitly requested additions
                # (rows_num / user_cols), then fill the cells.
                modify = {
                    "headers": headers,
                    "rows_data": rows_data,
                    "rows_num": rows_num,
                    "cols_num": user_cols,
                }
            elif headers:
                table = {"headers": headers, "rows_data": rows_data}

        # Idempotency guard: a spreadsheet row-append must not duplicate a row
        # that is already present (retries / repeated requests re-fire the whole
        # pipeline, and re-appending would corrupt the sheet).
        is_cell = file_path.lower().rsplit(".", 1)[-1] in _CELL_EXTENSIONS
        if is_cell and action == "office.editor.modify_table" and modify and modify["rows_data"]:
            existing_rows = [r for r in modify["rows_data"] if _cell_contains_rows(file_path, [r])]
            if len(existing_rows) == len(modify["rows_data"]):
                already = " ; ".join(" | ".join(str(c) for c in r) for r in modify["rows_data"])
                return {
                    "action": action,
                    "file": file_path,
                    "modified": False,
                    "response": f"La ligne '{already}' est déjà présente dans le tableau.",
                }

        # Use Document Builder to edit the document
        result = edit_document(file_path, request_text, table=table, modify=modify, insert=insert)
        
        if not result["success"]:
            raise ExecutorError(f"onlyoffice: Document Builder failed: {result['message']}")

        resp = result.get("message", "Document modifié")
        if is_cell and action == "office.editor.modify_table" and modify and modify["rows_data"]:
            resp = _describe_cell_append(file_path, modify["rows_data"])
        
        return {
            "action": action,
            "file": file_path,
            "modified": result.get("modified", False),
            "response": resp,
            "resolved_params": params,
        }

    # ------------------------------------------------------------------
    def _convert(self, params: dict[str, Any], action: str) -> dict[str, Any]:
        url = self._document_url(params)
        source_path = ""
        if not url:
            # The classifier often resolves `document` to a local stored name
            # (e.g. "test.docx"). Resolve it to its real path under the storage
            # (it may live in a subfolder, e.g. "test/test.docx"), then serve it
            # through our download endpoint so the Document Server can fetch it.
            doc = str(params.get("document") or params.get("path") or "").strip()
            if doc and "." in doc:
                from server.webapp.docbuilder import _file_url, _file_storage
                resolved = _file_storage.resolve(doc) or doc
                url = _file_url(resolved)
                source_path = resolved
        if not url:
            raise ExecutorError("onlyoffice: documentserver needs a document URL to convert")
        target = (params.get("target_format") or params.get("format") or "pdf").strip().lstrip(".")
        payload = {
            "url": url,
            "filetype": params.get("filetype") or self._guess_filetype(url),
            "outputtype": target,
            "title": params.get("title"),
        }
        body = {k: v for k, v in payload.items() if v is not None}
        headers = {"Authorization": f"Bearer {self._token(body)}"}
        raw = self._http.post(
            "/ConvertService.ashx",
            json=body,
            headers=headers,
            timeout=120,
        )
        if isinstance(raw, dict):  # JSON result (modern DS honours Accept: json)
            if isinstance(raw.get("error"), int) and raw["error"]:
                raise ExecutorError(f"onlyoffice convert: error {raw.get('error')}")
            converted_url = raw.get("fileUrl") or ""
            file_type = raw.get("fileType") or target
            saved = self._save_converted(converted_url, params, file_type, source_path)
            # Surface a working link to the SAVED file, never the Document
            # Server's internal cache URL the classifier/formatter would otherwise echo.
            return {
                "converted_url": self._saved_url(saved) or converted_url,
                "file": saved,
                "type": file_type,
                "percent": raw.get("percent"),
                "end_convert": raw.get("endConvert") is True,
            }
        import xml.etree.ElementTree as ET

        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise ExecutorError(f"onlyoffice convert: unexpected response '{raw[:120]}'") from exc
        file_url = root.findtext("FileUrl") or ""
        file_type = root.findtext("FileType") or target
        saved = self._save_converted(file_url, params, file_type, source_path)
        return {
            "converted_url": self._saved_url(saved) or file_url,
            "file": saved,
            "type": file_type,
            "percent": root.findtext("Percent"),
            "end_convert": root.findtext("EndConvert") == "True",
        }

    @staticmethod
    def _saved_url(saved: str) -> str:
        """Public download URL for a saved file, or '' when nothing was stored."""
        if not saved:
            return ""
        from server.webapp.docbuilder import _file_url

        return _file_url(saved)

    @staticmethod
    def _save_converted(converted_url: str, params: dict[str, Any], file_type: str, source_path: str = "") -> str:
        """Download the DS-produced output and store it under memory/files/.

        Returns the saved relative path ('' when nothing could be saved).
        """
        if not converted_url:
            return ""
        from server.webapp.docbuilder import _file_storage

        title = str(params.get("title") or "").strip()
        if not title or "." not in title:
            base = converted_url.rsplit("/", 1)[-1].split("?", 1)[0]
            title = base if "." in base else f"{base or 'converted'}.{file_type.lstrip('.')}"
        import urllib.request

        try:
            with urllib.request.urlopen(converted_url, timeout=60) as resp:
                content = resp.read()
        except Exception as exc:
            raise ExecutorError(f"onlyoffice convert: failed to download result: {exc}") from exc
        _file_storage.save_file(title, content)
        return title

    def _command(self, params: dict[str, Any], action: str) -> dict[str, Any]:
        command = _COMMAND_KIND.get(action, "version")
        payload: dict[str, Any] = {"c": command}
        if command == "version":
            body = payload
        else:
            url = self._document_url(params)
            if not url:
                raise ExecutorError("onlyoffice: command needs a document URL (document)")
            payload["key"] = params.get("key") or hashlib.md5(url.encode()).hexdigest()
            if command == "drop":
                payload["user"] = params.get("user") or "viito-agent"
            body = payload
        headers = {"Authorization": f"Bearer {self._token(body)}"}
        raw = self._http.post(
            "/coauthoring/CommandService.ashx",
            json=body,
            headers=headers,
            timeout=60,
        )
        if isinstance(raw, int) or isinstance(raw, float):
            raise ExecutorError(f"onlyoffice command: internal error [{raw}]")
        if not isinstance(raw, dict):
            raise ExecutorError(f"onlyoffice command: unexpected response '{raw}'")
        if raw.get("error"):
            raise ExecutorError(f"onlyoffice command {command}: error {raw.get('error')}")
        return {"command": command, "result": raw.get("result"), "version": raw.get("version")}

    @staticmethod
    def _guess_filetype(url: str) -> str:
        dot = url.rsplit(".", 1)
        if len(dot) == 2 and len(dot[1]) <= 5:
            return dot[1]
        return "docx"


def _prior_read_lines(ctx: ExecutionContext) -> list[str]:
    """Render earlier domains' READ results (free/busy, records) as plain lines.

    "écris mes disponibilités dans un fichier" executes calendaring first, then
    office.editor.create with no content: the document came out empty because
    the data to write lives in the previous domain's result, not in the
    classifier fields.
    """
    lines: list[str] = []
    for rec in getattr(ctx, "executor_log", None) or []:
        if str(rec.get("domain") or "").lower() == "office":
            continue
        for res in rec.get("results") or []:
            data = res.get("data") or {}
            if data.get("deferred") or not data.get("ok", True):
                continue
            response = data.get("response")
            if not isinstance(response, (dict, list)):
                continue
            for line in _render_read_payload(response):
                if line and line not in lines:
                    lines.append(line)
    return lines


def _render_read_payload(payload: Any) -> list[str]:
    """Human-readable lines for the read payloads Viito returns."""
    rows: list[dict[str, Any]] = []
    if isinstance(payload, list):
        rows = [r for r in payload if isinstance(r, dict)]
    elif isinstance(payload, dict):
        for key in ("busy_events", "events", "records", "leads", "rows", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                rows = [r for r in value if isinstance(r, dict)]
                break
        else:
            if payload.get("name") or payload.get("title"):
                rows = [payload]
    lines: list[str] = []
    for row in rows:
        label = str(
            row.get("event") or row.get("name") or row.get("title") or row.get("subject") or ""
        ).strip()
        start = str(row.get("start") or row.get("date_start") or "").strip()
        stop = str(row.get("stop") or row.get("end") or row.get("date_end") or "").strip()
        when = _human_datetime(start)
        if stop:
            when = f"{when} - {_human_datetime(stop).split(' ')[-1]}" if when else _human_datetime(stop)
        if label and when:
            lines.append(f"- {label} : {when}")
        elif label:
            lines.append(f"- {label}")
        elif when:
            lines.append(f"- {when}")
    return lines


def _human_datetime(value: str) -> str:
    """'2026-09-29 13:00:00' -> 'mardi 29 septembre 2026 à 13:00'."""
    text = str(value or "").strip()
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})", text)
    if not match:
        return text
    year, month, day, hour, minute = (int(g) for g in match.groups())
    try:
        date = datetime.date(year, month, day)
        stamp = f"{_FR_DAYS[date.weekday()]} {date.day} {_FR_MONTHS[date.month - 1]} {date.year}"
        return f"{stamp} à {hour:02d}:{minute:02d}"
    except (ValueError, IndexError):
        return text


_FR_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
_FR_MONTHS = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]
