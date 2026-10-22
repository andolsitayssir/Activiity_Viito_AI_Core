"""ONLYOFFICE Document Server integration.

Embeds the ONLYOFFICE editor (Word/Excel/PDF/Presentation) for files stored in
the local file space (memory/files/), and receives the saved documents back.

The ONLYOFFICE Document Server runs as a Docker container on the host. The
browser talks to it on ``http://127.0.0.1:8088``; the container reaches back to
this FastAPI app on the host via ``host.docker.internal``.

Endpoints
---------
GET  /onlyoffice/docserver      -> { url, configured } (Document Server URL)
POST /onlyoffice/config         -> OnlyOffice editor config + JWT
POST /onlyoffice/callback       -> Document Server save callback (POSTs the file)
GET  /onlyoffice/content        -> raw text/bytes of a file (for Viito)
POST /onlyoffice/content        -> write raw text/bytes to a file (Viito edit)
POST /onlyoffice/viito          -> Viito modifies file (single turn)
POST /onlyoffice/viito/session  -> Viito multi-turn session
GET  /onlyoffice/viito/session  -> list Viito sessions
GET  /onlyoffice/viito/session/{id} -> get session history
POST /onlyoffice/viito/session/{id}/message -> send message to session
POST /onlyoffice/viito/session/{id}/attach -> attach file to session
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import jwt
import requests

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from server.config import load_environment, model_defaults
from server.execution.executors.file_storage import FileStorage

load_environment()

router = APIRouter(tags=["onlyoffice"])

_file_storage = FileStorage()

# Extensions whose text content we can extract for the AI pipeline context.
_TEXT_EXTENSIONS = {
    "docx": "docx", "doc": "docx",
    "xlsx": "xlsx", "xls": "xlsx", "ods": "xlsx", "csv": "xlsx",
    "pptx": "pptx", "ppt": "pptx", "odp": "pptx",
    "pdf": "pdf",
    "txt": "txt", "md": "txt", "rtf": "txt", "html": "txt", "htm": "txt",
    "json": "txt", "xml": "txt", "log": "txt",
}


def _extract_docx_text(content: bytes) -> str:
    from docx import Document
    from io import BytesIO
    doc = Document(BytesIO(content))
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    body = "\n".join(paragraphs)
    table_rows = []
    for table in doc.tables[:20]:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                table_rows.append(" | ".join(cells))
    if table_rows:
        body += ("\n--- Tableaux ---\n" if body else "") + "\n".join(table_rows)
    return body


def _extract_xlsx_text(content: bytes) -> str:
    import openpyxl
    from io import BytesIO
    wb = openpyxl.load_workbook(BytesIO(content), data_only=True, read_only=True)
    parts: list[str] = []
    for ws in wb.worksheets[:5]:
        rows: list[str] = []
        for row in ws.iter_rows(values_only=True):
            cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
            if cells:
                rows.append(" | ".join(cells))
        if rows:
            parts.append(f'Feuille "{ws.title}":\n' + "\n".join(rows))
    wb.close()
    return "\n\n".join(parts)


def _extract_pptx_text(content: bytes) -> str:
    from pptx import Presentation
    from io import BytesIO
    prs = Presentation(BytesIO(content))
    parts: list[str] = []
    for i, slide in enumerate(prs.slides, 1):
        texts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                t = "\n".join(p.text for p in shape.text_frame.paragraphs if p.text.strip())
                if t.strip():
                    texts.append(t.strip())
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells]
                    if any(cells):
                        texts.append(" | ".join(cells))
                break
        if texts:
            parts.append(f"Slide {i}:\n" + "\n".join(texts))
    return "\n\n".join(parts)


def _extract_pdf_text(content: bytes) -> str:
    from pypdf import PdfReader
    from io import BytesIO
    reader = PdfReader(BytesIO(content))
    pages: list[str] = []
    for i, page in enumerate(reader.pages[:30], 1):
        t = (page.extract_text() or "").strip()
        if t:
            pages.append(f"Page {i}:\n{t}")
    return "\n\n".join(pages)


def _extract_file_text(content: bytes, ext: str) -> str | None:
    """Extract readable text for the Viito pipeline context; None = binary."""
    kind = _TEXT_EXTENSIONS.get(ext and ext.lower())
    if kind == "docx":
        return _extract_docx_text(content)
    if kind == "xlsx":
        return _extract_xlsx_text(content)
    if kind == "pptx":
        return _extract_pptx_text(content)
    if kind == "pdf":
        return _extract_pdf_text(content)
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


# ── configuration ────────────────────────────────────────────────────────
DOC_SERVER_PORT = os.environ.get("ONLYOFFICE_DS_PORT", "8088")
DOC_SERVER_URL = os.environ.get("ONLYOFFICE_DS_URL", f"http://127.0.0.1:{DOC_SERVER_PORT}")
# URL used inside the Docker container to reach this app (to fetch/save files).
APP_INTERNAL_HOST = os.environ.get("ONLYOFFICE_APP_HOST", "host.docker.internal")
APP_INTERNAL_PORT = os.environ.get("ONLYOFFICE_APP_PORT", "8000")
# DocServer address reachable from inside the container (published on the HOST).
# DOC_SERVER_URL above stays browser-facing (127.0.0.1:8088) for the editor.
DOCSERVER_HOST_URL = os.environ.get(
    "ONLYOFFICE_DS_INTERNAL_URL", f"http://{APP_INTERNAL_HOST}:{DOC_SERVER_PORT}"
).rstrip("/")
# Secret shared with the Document Server. No default: it must come from the
# environment (see .env.example) so the value never lands in the repository.
JWT_SECRET = os.environ.get("ONLYOFFICE_JWT_SECRET", "")

FILE_TYPES = {
    "word": {"exts": ("docx", "doc", "odt", "rtf", "txt"), "documentType": "word"},
    "cell": {"exts": ("xlsx", "xls", "ods", "csv"), "documentType": "cell"},
    "slide": {"exts": ("pptx", "ppt", "odp"), "documentType": "slide"},
    "pdf": {"exts": ("pdf",), "documentType": "word"},
}

_file_storage = FileStorage()

# key -> {path, ext} so the save callback knows where to write the file.
_SESSIONS_PATH = Path(__file__).resolve().parent / "onlyoffice_sessions.json"
_viito_sessions_path = Path(__file__).resolve().parent / "onlyoffice_viito_sessions.json"
_oo_lock = threading.Lock()


def _load_sessions() -> dict:
    try:
        if _SESSIONS_PATH.exists():
            data = json.loads(_SESSIONS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_sessions(sessions: dict) -> None:
    try:
        _SESSIONS_PATH.write_text(json.dumps(sessions, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _remember(key: str, path: str) -> None:
    with _oo_lock:
        s = _load_sessions()
        s[key] = {"path": path, "at": time.time()}
        _save_sessions(s)


def _lookup(key: str) -> dict | None:
    with _oo_lock:
        return _load_sessions().get(key)


def _ext_for(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ext


def _category_for_path(path: str) -> str | None:
    ext = _ext_for(path)
    for cat, info in FILE_TYPES.items():
        if ext in info["exts"]:
            return cat
    return None


def _sign(payload: dict) -> str:
    if not JWT_SECRET:
        raise RuntimeError(
            "ONLYOFFICE_JWT_SECRET not configured (set it in .env, see .env.example)"
        )
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256", headers={"typ": "JWT"})


def _verify(token: str) -> dict | None:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except Exception:
        return None


def _file_url(path: str) -> str:
    """URL the Document Server uses to fetch the raw file bytes."""
    from urllib.parse import quote
    return f"http://{APP_INTERNAL_HOST}:{APP_INTERNAL_PORT}/onlyoffice/download/{quote(path)}"


def _callback_url() -> str:
    return f"http://{APP_INTERNAL_HOST}:{APP_INTERNAL_PORT}/onlyoffice/callback"


# ── open the file bytes to the Document Server ─────────────────────────
@router.get("/onlyoffice/download/{path:path}")
def onlyoffice_download(path: str):
    content = _file_storage.read_file(path)
    if content is None:
        raise HTTPException(status_code=404, detail="File not found")
    info = _file_storage.get_info(path) or {}
    return Response(
        content=content,
        media_type=info.get("mime", "application/octet-stream"),
        headers={"Content-Disposition": f'inline; filename="{path.rsplit("/", 1)[-1]}"'},
    )


@router.get("/onlyoffice/docserver")
def onlyoffice_docserver():
    alive = False
    try:
        import urllib.request
        with urllib.request.urlopen(f"{DOCSERVER_HOST_URL}/healthcheck", timeout=3) as r:
            body = r.read().decode().strip()
            alive = body == "true"
    except Exception:
        alive = False
    return {"configured": bool(os.environ.get("ONLYOFFICE_DS_URL")) or alive, "url": DOC_SERVER_URL, "alive": alive}


# ── editor config ───────────────────────────────────────────────────────
class OnlyOfficeConfigRequest(BaseModel):
    path: str
    type: str | None = None  # optional forced category (word/cell/slide/pdf)


@router.post("/onlyoffice/config")
def onlyoffice_config(body: OnlyOfficeConfigRequest):
    path = body.path.strip("/").strip("\\")
    info = _file_storage.get_info(path)
    if info is None:
        raise HTTPException(status_code=404, detail="File not found")

    ext = _ext_for(path)
    category = body.type if body.type in FILE_TYPES else _category_for_path(path)
    if category is None:
        raise HTTPException(status_code=400, detail=f"Type de fichier non supporté par OnlyOffice: .{ext}")

    file_key = str(uuid.uuid4()) + f"-{int(time.time() * 1000)}"
    document_type = FILE_TYPES[category]["documentType"]

    config: dict = {
        "document": {
            "fileType": ext,
            "key": file_key,
            "title": info["name"],
            "url": _file_url(path),
        },
        "documentType": document_type,
        "editorConfig": {
            "callbackUrl": _callback_url(),
            "mode": "edit",
            "lang": "fr",
            "user": {"id": "viito-user", "name": "Viito"},
            "customization": {
                "autosave": True,
                "compactToolbar": False,
                "forcesave": True,
                "goback": {"blank": True},
            },
        },
    }

    token = _sign(config)
    _remember(file_key, path)
    return JSONResponse({"config": config, "token": token, "docserver": DOC_SERVER_URL})


# ── save callback ───────────────────────────────────────────────────────
class SaveBody(BaseModel):
    status: int | None = None
    url: str | None = None
    key: str | None = None
    filetype: str | None = None


@router.post("/onlyoffice/callback")
async def onlyoffice_callback(request: Request, body: SaveBody | None = None):
    # OnlyOffice can send the callback as either form data or JSON.
    form = None
    try:
        form = await request.form()
    except Exception:
        form = None

    status = body.status if body is not None else None
    download_url = body.url if body is not None else None
    key = body.key if body is not None else None
    if form is not None:
        if status is None:
            raw_status = form.get("status")
            try:
                status = int(raw_status) if raw_status is not None else None
            except (TypeError, ValueError):
                status = None
        if key is None:
            key = form.get("key")
        if download_url is None:
            download_url = form.get("url")

    if status == 2 and download_url:  # 2 = document ready for saving
        saved_path = None
        if key:
            session = _lookup(str(key))
            if session:
                saved_path = session.get("path")
        if not saved_path:
            return {"error": 1}
        try:
            import urllib.request
            with urllib.request.urlopen(download_url, timeout=30) as r:
                content = r.read()
        except Exception:
            return {"error": 1}
        _file_storage.save_file(saved_path, content)
        return {"error": 0}
    return {"error": 0}


# ── Viito content read/write ────────────────────────────────────────────
@router.get("/onlyoffice/content")
def onlyoffice_get_content(path: str):
    """Return raw text of a file (used by Viito to read a document)."""
    content = _file_storage.read_file(path)
    if content is None:
        raise HTTPException(status_code=404, detail="File not found")
    info = _file_storage.get_info(path) or {}
    ext = _ext_for(path)
    try:
        text = content.decode("utf-8")
        is_binary = False
    except UnicodeDecodeError:
        text = base64.b64encode(content).decode("ascii")
        is_binary = True
    return {
        "path": path,
        "name": info.get("name", path.rsplit("/", 1)[-1]),
        "ext": ext,
        "text": text,
        "encoded": is_binary,
    }


class ContentPost(BaseModel):
    path: str
    content: str
    encoded: bool = False


@router.post("/onlyoffice/content")
def onlyoffice_set_content(body: ContentPost):
    if body.encoded:
        try:
            raw = base64.b64decode(body.content)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid base64 content")
    else:
        raw = body.content.encode("utf-8")
    info = _file_storage.save_file(body.path.strip("/").strip("\\"), raw)
    return {"ok": True, "info": info}


# ── Viito modifies the open file ───────────────────────────────────────
class ViitoEditRequest(BaseModel):
    path: str
    request: str


@router.post("/onlyoffice/viito")
def onlyoffice_viito_edit(body: ViitoEditRequest):
    """Let Viito read a file and return modified text content.

    Works best on text-based files (txt/md/csv/html/json). Binary office
    formats (docx/xlsx/pptx/pdf) are reported as unsupported for direct text
    editing — the browser can fall back to a message.
    """
    path = body.path.strip("/").strip("\\")
    content = _file_storage.read_file(path)
    if content is None:
        raise HTTPException(status_code=404, detail="File not found")
    info = _file_storage.get_info(path) or {}
    ext = _ext_for(path)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = None

    if text is None:
        return JSONResponse(
            {
                "ok": False,
                "reason": "binary",
                "message": f"Le fichier .{ext} est un document binaire; Viito ne peut pas encore éditer son texte directement. Sauvegardez-le en .txt pour les modifications par Viito.",
            },
            status_code=200,
        )

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="OPENROUTER_API_KEY absent")

    defaults = model_defaults()
    model = str(defaults.get("FORMATTER_MODEL") or "qwen/qwen3-8b")
    system_prompt = (
        "Tu es un assistant d'édition de documents intégré à Activiity. "
        "L'utilisateur te demande de modifier un fichier texte. À partir de la demande et "
        "du contenu actuel du fichier, produis le NOUVEAU contenu complet du fichier, "
        "modifié selon la demande. Respecte le format d'origine (mêmes fins de ligne). "
        "Renvoie UNIQUEMENT le nouveau contenu du fichier, sans commentaire ni balise, "
        "sans préfixe ni suffixe."
    )
    user_content = (
        "Demande de modification :\n" + body.request + "\n\n"
        "Contenu actuel du fichier :\n" + text
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.3,
        "max_tokens": 4000,
    }
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Title": "activity-ai-engine",
            },
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        new_text = str(resp.json()["choices"][0]["message"]["content"]).strip()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Erreur Viito: {exc}")

    # Write back in place so the editor can refresh with the new content.
    _file_storage.save_file(path, new_text.encode("utf-8"))
    return {"ok": True, "path": path, "content": new_text, "ext": ext}


# ── Viito multi-turn sessions ────────────────────────────────────────
def _load_viito_sessions() -> dict:
    try:
        if _viito_sessions_path.exists():
            data = json.loads(_viito_sessions_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_viito_sessions(sessions: dict) -> None:
    try:
        _viito_sessions_path.write_text(json.dumps(sessions, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _get_viito_session(session_id: str) -> dict | None:
    with _oo_lock:
        return _load_viito_sessions().get(session_id)


def _save_viito_session(session: dict) -> None:
    with _oo_lock:
        sessions = _load_viito_sessions()
        sessions[session["id"]] = session
        _save_viito_sessions(sessions)


class ViitoSessionCreate(BaseModel):
    file_path: str


class ViitoSessionMessage(BaseModel):
    content: str
    attachments: list[dict[str, Any]] | None = None


class ViitoSessionAttach(BaseModel):
    filename: str
    content_type: str = "application/octet-stream"
    content_b64: str


@router.get("/onlyoffice/viito/sessions")
def list_viito_sessions():
    sessions = _load_viito_sessions()
    result = []
    for s in sessions.values():
        result.append({
            "id": s["id"],
            "file_path": s.get("file_path", ""),
            "created_at": s.get("created_at", ""),
            "updated_at": s.get("updated_at", ""),
            "message_count": len(s.get("messages", [])),
        })
    result.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return result


@router.post("/onlyoffice/viito/session")
def create_viito_session(body: ViitoSessionCreate):
    file_path = body.file_path.strip("/").strip("\\")
    content = _file_storage.read_file(file_path)
    if content is None:
        raise HTTPException(status_code=404, detail="File not found")
    info = _file_storage.get_info(file_path) or {}
    ext = _ext_for(file_path)
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
    session_id = str(uuid.uuid4())
    session = {
        "id": session_id,
        "file_path": file_path,
        "file_name": info.get("name", file_path.rsplit("/", 1)[-1]),
        "file_ext": ext,
        "created_at": now_iso,
        "updated_at": now_iso,
        "messages": [],
        "attachments": [],
    }
    _save_viito_session(session)
    return {"id": session_id, "file_path": file_path, "created_at": now_iso}


@router.get("/onlyoffice/viito/session/{session_id}")
def get_viito_session(session_id: str):
    session = _get_viito_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.delete("/onlyoffice/viito/session/{session_id}")
def delete_viito_session(session_id: str):
    with _oo_lock:
        sessions = _load_viito_sessions()
        if session_id in sessions:
            del sessions[session_id]
            _save_viito_sessions(sessions)
    return {"ok": True}


@router.post("/onlyoffice/viito/session/{session_id}/message")
def send_viito_session_message(session_id: str, body: ViitoSessionMessage):
    print(f"[VIITO] Endpoint called: session_id={session_id}, content={body.content[:50] if body.content else 'None'}", flush=True)
    session = _get_viito_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    file_path = session.get("file_path", "")
    content = _file_storage.read_file(file_path)
    if content is None:
        raise HTTPException(status_code=404, detail="File not found")

    info = _file_storage.get_info(file_path) or {}
    ext = session.get("file_ext", "")
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
    user_content = body.content

    # Detect + persist image attachments so the AI pipeline + DocBuilder can
    # fetch them via /onlyoffice/download/images/<name>. The insertion itself is
    # NOT short-circuited here — it flows through the full pipeline (classifier
    # -> dispatcher -> executor -> validator -> formatter).
    image_attachments = [a for a in (body.attachments or []) if (a.get("content_type", "").startswith("image/") or a.get("filename", "").lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".bmp", ".webp")))]
    print(f"[VIITO] Attachments received: {len(body.attachments or [])}, image_attachments: {len(image_attachments)}", flush=True)
    for att in (body.attachments or []):
        has_b64 = bool(att.get("content_b64"))
        print(f"[VIITO]   attachment: {att.get('filename')}, content_type: {att.get('content_type')}, has_b64: {has_b64}", flush=True)

    saved_image_urls = []
    for att in image_attachments:
        filename = att.get("filename", "image.jpg")
        img_path = f"images/{filename}"
        content_b64 = att.get("content_b64", "")
        print(f"[VIITO] Processing image: {filename}, content_b64 length: {len(content_b64)}", flush=True)
        if not content_b64:
            print(f"[VIITO] SKIP: no content_b64 for {filename}", flush=True)
            continue
        try:
            img_data = base64.b64decode(content_b64)
            print(f"[VIITO] Decoded image: {filename}, size: {len(img_data)} bytes", flush=True)
            _file_storage.save_file(img_path, img_data)
            from urllib.parse import quote
            saved_image_urls.append(f"http://{APP_INTERNAL_HOST}:{APP_INTERNAL_PORT}/onlyoffice/download/images/{quote(filename)}")
        except Exception as exc:
            print(f"[VIITO] ERROR decoding image {filename}: {exc}", flush=True)

    # For all other requests, run through the full AI pipeline
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="OPENROUTER_API_KEY absent")

    # Decode file content as readable text for the AI pipeline context.
    # xlsx/pptx/pdf are extracted natively so Viito can reason over them
    # exactly like it already does for docx.
    original_bytes = content
    text = None
    try:
        text = _extract_file_text(content, ext)
    except Exception as exc:
        print(f"[VIITO] ERROR extracting text (.{ext}): {exc}", flush=True)
        text = None

    if text is None:
        return JSONResponse({
            "ok": False,
            "reason": "binary",
            "message": f"Le fichier .{ext} est binaire; Viito ne peut pas éditer son texte directement.",
        }, status_code=200)
    print(f"[VIITO] Extracted {len(text)} chars of text from .{ext}", flush=True)

    # Build the query with file context
    file_context = f"\n\n---\nFichier actuel ({info.get('name', file_path.rsplit('/', 1)[-1])}):\n{text[:8000]}"
    full_query = user_content + file_context

    # Add attachment info + file path as metadata for the executor
    attachments = list(body.attachments or []) if body.attachments else []
    attachments.append({"_viito_file_path": file_path, "_viito_file_name": info.get("name", file_path.rsplit("/", 1)[-1])})
    if body.attachments:
        for att in body.attachments:
            full_query += f"\n\n[Pièce jointe: {att.get('filename', 'fichier')}]"
    # Expose persisted image URLs to the executor + attribute classifier so
    # insert_image can resolve image_url without short-circuiting the pipeline.
    for img_url in saved_image_urls:
        attachments.append({"_viito_image_url": img_url})
        full_query += f"\n\n[Image disponible: {img_url}]"

    # Run through the full pipeline (router -> dispatcher -> executor -> validator -> formatter)
    print(f"[VIITO] Starting full pipeline for query: {user_content[:100]}", flush=True)
    try:
        from server.webapp.api import _run_pipeline, _final_to_reply, _build_history
        print("[VIITO] Imported _run_pipeline successfully", flush=True)
        state = _run_pipeline(
            full_query,
            thread_id=session_id,
            user_id="webapp-onlyoffice",
            attachments=attachments,
            history=_build_history(session.get("messages") or [], user_content),
        )
        print(f"[VIITO] Pipeline returned. Keys: {list(state.keys()) if isinstance(state, dict) else type(state)}", flush=True)
        reply = _final_to_reply(state)
        print(f"[VIITO] Final reply: {reply.get('content', '')[:100]}", flush=True)
        assistant_text = reply.get("content", "")
        meta = reply.get("meta", {})
    except Exception as exc:
        print(f"[VIITO] Pipeline FAILED: {exc}", flush=True)
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=502, detail=f"Erreur pipeline Viito: {exc}")

    # Check if the pipeline modified the file via the office executor
    modified = False
    new_text = text
    
    # The executor may have modified the file through the Viito backend.
    # Compare raw bytes (text decode would break on xlsx/pptx/pdf).
    if meta.get("actions"):
        content = _file_storage.read_file(file_path)
        if content is not None:
            modified = content != original_bytes
            try:
                new_text = content.decode("utf-8")
            except UnicodeDecodeError:
                new_text = None

    session["messages"].append({"role": "user", "content": user_content, "timestamp": now_iso})
    session["messages"].append({"role": "assistant", "content": assistant_text, "timestamp": now_iso, "modified": modified, "meta": meta})
    session["updated_at"] = now_iso
    _save_viito_session(session)
    return {"ok": True, "modified": modified, "content": assistant_text, "file_path": file_path, "file_content": new_text if modified else None, "meta": meta}


@router.post("/onlyoffice/viito/session/{session_id}/attach")
def attach_file_to_viito_session(session_id: str, body: ViitoSessionAttach):
    session = _get_viito_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
    session["attachments"].append({
        "filename": body.filename,
        "content_type": body.content_type,
        "content_b64": body.content_b64,
        "timestamp": now_iso,
    })
    session["updated_at"] = now_iso
    _save_viito_session(session)

    return {"ok": True, "count": len(session["attachments"])}


# ── DocBuilder script hosting ──────────────────────────────────────────────
# The DocBuilder Web API requires a URL to a .js script. We host generated
# scripts here so OnlyOffice can download them.
_docbuilder_scripts: dict[str, dict] = {}


def register_docbuilder_script(script_id: str, js_code: str, content_type: str = "application/javascript") -> str:
    """Register a generated .js script and return the URL to fetch it."""
    _docbuilder_scripts[script_id] = {"js": js_code, "ct": content_type}
    base = f"http://{APP_INTERNAL_HOST}:{APP_INTERNAL_PORT}"
    return f"{base}/docbuilder/script/{script_id}"


class DocBuilderScriptRegister(BaseModel):
    script_id: str
    js_code: str
    content_type: str = "application/javascript"


@router.post("/docbuilder/script")
def register_docbuilder_script_endpoint(body: DocBuilderScriptRegister):
    """Register a .js script so OnlyOffice can download it via GET."""
    _docbuilder_scripts[body.script_id] = {"js": body.js_code, "ct": body.content_type}
    return {"ok": True, "url": f"http://{APP_INTERNAL_HOST}:{APP_INTERNAL_PORT}/docbuilder/script/{body.script_id}"}


@router.get("/docbuilder/script/{script_id}")
def serve_docbuilder_script(script_id: str):
    """Serve a generated .js script for the DocBuilder Web API."""
    entry = _docbuilder_scripts.get(script_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Script not found")
    # Auto-cleanup after serving
    _docbuilder_scripts.pop(script_id, None)
    return Response(content=entry["js"], media_type=entry["ct"])


# ── WOPI endpoints ────────────────────────────────────────────────────────
# OnlyOffice uses WOPI to read/write files. These endpoints implement the
# WOPI REST interface that OnlyOffice calls from inside Docker.

@router.get("/wopi/files/{file_id}")
async def wopi_check_file_info(file_id: str, request: Request):
    """WOPI CheckFileInfo — returns file metadata for OnlyOffice."""
    # Resolve file_id to actual file path
    # file_id is the relative path under memory/files/ (e.g. "Document test.docx")
    file_path = file_id.replace("\\", "/")
    info = _file_storage.get_info(file_path)
    if info is None:
        raise HTTPException(status_code=404, detail="File not found")

    ext = info.get("name", "").rsplit(".", 1)[-1].lower() if "." in info.get("name", "") else ""
    file_type = "word"
    for ftype, meta in FILE_TYPES.items():
        if ext in meta["exts"]:
            file_type = ftype
            break

    # WOPI CheckFileInfo response
    base_url = f"http://{APP_INTERNAL_HOST}:{APP_INTERNAL_PORT}"
    response = {
        "BaseFileName": info["name"],
        "OwnerId": "viito-user",
        "Size": info["size"],
        "Version": str(int(info["modified"])),
        "SupportsUpdate": True,
        "SupportsLocks": True,
        "SupportsGetLock": True,
        "SupportsDeleteFile": False,
        "SupportsRename": False,
        "SupportsReadSecure": False,
        "SupportsCoauth": True,
        "SupportsCoauthLock": False,
        "SupportsContainers": False,
        "SupportsApplicationLicenseCheck": False,
        "UserCanWrite": True,
        "UserCanRename": False,
        "UserCanDelete": False,
        "UserCanWriteRelative": True,
        "ReadOnly": False,
        "IsReadOnly": False,
        "SupportsFileExtension": True,
        "FileExtension": f".{ext}" if ext else "",
        "FileType": file_type,
        "FileName": info["name"],
        "BreadcrumbBrandName": "Activiity",
        "BreadcrumbBrandUrl": base_url,
        "BreadcrumbFolderName": "Espace local",
        "BreadcrumbFolderUrl": base_url,
        "CloseUrl": f"{base_url}/onlyoffice/close?file={file_path}",
        "FileSharingUrl": "",
        "FileVersionPostUrl": "",
        "FileVersionUrl": "",
        "HostEditUrl": "",
        "HostEmbeddedEditUrl": "",
        "HostViewUrl": "",
        "Lock": {
            "LockType": "None",
        },
        "OwnerId": "viito-user",
        "PostMessageOrigin": base_url,
        "SHA256": "",
        "UniqueId": file_id,
        "UserCanAttend": False,
        "UserCanExport": True,
        "UserCanPrint": True,
        "UserCanExportAs": True,
        "UserCanPrintAs": True,
    }

    # Generate JWT token if enabled
    try:
        import jwt
        token_payload = {
            "wopi": {
                "iss": "activiity",
                "aud": "onlyoffice",
                "iat": int(time.time()),
                "exp": int(time.time()) + 3600,
                "file_id": file_id,
            }
        }
        token = jwt.encode(token_payload, JWT_SECRET, algorithm="HS256")
        response["token"] = token
    except ImportError:
        pass  # JWT not available, skip token

    return JSONResponse(content=response)


@router.get("/wopi/files/{file_id}/contents")
async def wopi_get_file(file_id: str, request: Request):
    """WOPI GetFile — returns raw file content for OnlyOffice."""
    file_path = file_id.replace("\\", "/")
    content = _file_storage.read_file(file_path)
    if content is None:
        raise HTTPException(status_code=404, detail="File not found")

    # Get content type
    info = _file_storage.get_info(file_path)
    ext = info.get("name", "").rsplit(".", 1)[-1].lower() if "." in info.get("name", "") else ""
    content_type = "application/octet-stream"
    ct_map = {
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "doc": "application/msword",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xls": "application/vnd.ms-excel",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "ppt": "application/vnd.ms-powerpoint",
        "pdf": "application/pdf",
        "txt": "text/plain",
        "csv": "text/csv",
    }
    content_type = ct_map.get(ext, "application/octet-stream")

    return Response(
        content=content,
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{info.get("name", file_id)}"',
        },
    )


@router.put("/wopi/files/{file_id}/contents")
async def wopi_put_file(file_id: str, request: Request):
    """WOPI PutFile — receives updated file content from OnlyOffice."""
    file_path = file_id.replace("\\", "/")

    # Read the request body (raw file content)
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty body")

    # Save the file
    _file_storage.save_file(file_path, body)

    print(f"[WOPI] File saved: {file_path} ({len(body)} bytes)", flush=True)

    return JSONResponse(content={"COULDNOTSETLOCKS": False})


@router.post("/wopi/files/{file_id}")
async def wopi_file_operation(file_id: str, request: Request):
    """WOPI file operations (lock, unlock, rename, etc.)."""
    file_path = file_id.replace("\\", "/")
    body = await request.json()
    operation = body.get("Operation")

    if operation == "LOCK":
        # Lock the file
        return JSONResponse(content={"Lock": "viito-lock"})
    elif operation == "UNLOCK":
        # Unlock the file
        return JSONResponse(content={})
    elif operation == "GET_LOCK":
        # Return current lock status
        return JSONResponse(content={"Lock": None, "LockType": "None"})
    else:
        return JSONResponse(content={})


@router.get("/onlyoffice/close")
async def onlyoffice_close(file: str = ""):
    """Called when OnlyOffice editor is closed."""
    return JSONResponse(content={"ok": True, "message": "Editor closed"})
