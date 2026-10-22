"""Gmail executor — serves the email_action domain via Gmail REST API.

Uses OAuth2 for authentication. Tokens are stored in a JSON file on disk
(``memory/gmail_tokens.json``). The executor handles token refresh
automatically when the access token expires.

Supported actions: send, list/read messages by label (inbox, sent, drafts,
trash), create/delete drafts, trash/untrash messages.
"""

from __future__ import annotations

import base64
import hashlib
import json
import unicodedata
from urllib.parse import quote
import os
import re
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
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

# Gmail API base
_GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
_GOOGLE_OAUTH = "https://oauth2.googleapis.com"
_GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"

# Label IDs for standard boxes
_LABEL_MAP = {
    "inbox": "INBOX",
    "sent": "SENT",
    "drafts": "DRAFT",
    "trash": "TRASH",
    "spam": "SPAM",
    "important": "IMPORTANT",
    "starred": "STARRED",
}

_SCOPES = "https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/gmail.send"

_TOKEN_DIR = Path(__file__).resolve().parents[3] / "memory"
_TOKEN_FILE = _TOKEN_DIR / "gmail_tokens.json"


#: Actions with a real external side effect: never re-run them for a turn that
#: already fired them (the pipeline replays the whole domain on retries).
_SIDE_EFFECT_ACTIONS = frozenset({
    "email.email_submission.set",
    "email.email.reply",
})
_SIDE_EFFECT_CACHE_MAX = 200
#: A side effect is only replayed for this long: long enough to cover the
#: validation retries of one turn (seconds), short enough that asking again
#: later always sends for real.
_SIDE_EFFECT_TTL_SECONDS = 120.0

_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".zip": "application/zip",
}


def _guess_content_type(filename: str) -> str:
    suffix = Path(str(filename or "")).suffix.casefold()
    return _CONTENT_TYPES.get(suffix, "application/octet-stream")


def _rfc2231_filename(filename: str) -> tuple[str, str, str]:
    """Filename as an RFC 2231 parameter.

    Writing the header by hand (``f'attachment; filename="{name}"'``) made
    Python RFC 2047-encode the WHOLE header as soon as the name held a
    non-ASCII character ("données" -> "=?utf-8?q?attachment=3B_filename=..."),
    which Gmail cannot parse: the part arrived with its bytes but with no
    filename at all. Passing the name as a parameter keeps the token intact and
    encodes only the value.
    """
    name = str(filename or "").strip() or "file"
    # A quote or backslash inside the name would break the quoted-string form.
    return ("utf-8", "", name.replace("\\", "_").replace('"', "_"))


def _ascii_fallback_name(filename: str) -> str:
    """ASCII-safe version of a file name for the plain `filename=` parameter."""
    folded = unicodedata.normalize("NFKD", str(filename or ""))
    ascii_name = folded.encode("ascii", "ignore").decode("ascii")
    ascii_name = ascii_name.replace("\\", "_").replace('"', "_").strip()
    return ascii_name or "file"


def _content_disposition(filename: str) -> str:
    """`Content-Disposition` carrying the name twice, safely.

    ``filename="<ascii>"`` for clients that only read the classic parameter and
    ``filename*=utf-8''...`` (RFC 2231) for the exact accented name.
    """
    ascii_name = _ascii_fallback_name(filename)
    exact = str(filename or "").strip() or "file"
    exact = exact.replace("\\", "_").replace('"', "_").replace("\r", "").replace("\n", "")
    quoted = quote(exact, safe="")
    return f'attachment; filename="{ascii_name}"; filename*=utf-8\'\'{quoted}'


class GmailExecutor(DomainExecutor):
    domains: tuple[str, ...] = ("email_action",)
    backend = "gmail"

    def __init__(self) -> None:
        self._client_id = os.environ.get("GMAIL_CLIENT_ID", "")
        self._client_secret = os.environ.get("GMAIL_CLIENT_SECRET", "")
        self._redirect_uri = os.environ.get(
            "GMAIL_REDIRECT_URI", "http://localhost:8000/api/gmail/callback"
        )
        self._tokens: dict[str, Any] = {}
        self._http: HttpClient | None = None
        self._load_tokens()
        self._load_stored_credentials()

    # ------------------------------------------------------------------
    # OAuth2 credential management (stored on disk, set from UI)
    # ------------------------------------------------------------------
    _CREDS_FILE = _TOKEN_DIR / "gmail_credentials.json"

    def _load_stored_credentials(self) -> None:
        if self._CREDS_FILE.exists():
            try:
                creds = json.loads(self._CREDS_FILE.read_text(encoding="utf-8"))
                if not self._client_id and creds.get("client_id"):
                    self._client_id = creds["client_id"]
                if not self._client_secret and creds.get("client_secret"):
                    self._client_secret = creds["client_secret"]
            except (json.JSONDecodeError, OSError):
                pass

    def set_credentials(self, client_id: str, client_secret: str) -> None:
        """Store OAuth credentials from UI input to disk."""
        self._client_id = client_id
        self._client_secret = client_secret
        _TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        self._CREDS_FILE.write_text(
            json.dumps({"client_id": client_id, "client_secret": client_secret}, indent=2),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # OAuth2 token management
    # ------------------------------------------------------------------
    def _load_tokens(self) -> None:
        if _TOKEN_FILE.exists():
            try:
                self._tokens = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._tokens = {}

    def _save_tokens(self) -> None:
        _TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        _TOKEN_FILE.write_text(json.dumps(self._tokens, indent=2), encoding="utf-8")

    def _is_connected(self) -> bool:
        return bool(self._tokens.get("access_token"))

    def _get_authenticated_http(self) -> HttpClient:
        if not self._tokens.get("access_token"):
            raise ExecutorError("Gmail non connecté. Veuillez d'abord activer l'API Gmail.")
        if time.time() >= self._tokens.get("expires_at", 0):
            self._refresh_token()
        http = HttpClient("https://gmail.googleapis.com")
        http._session.headers["Authorization"] = f"Bearer {self._tokens['access_token']}"
        return http

    def _refresh_token(self) -> None:
        refresh = self._tokens.get("refresh_token")
        if not refresh:
            raise ExecutorError("Token Gmail expiré et pas de refresh token. Reconnectez-vous.")
        http = HttpClient(_GOOGLE_OAUTH)
        data = http.post(
            "/token",
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self._tokens["access_token"] = data["access_token"]
        self._tokens["expires_at"] = time.time() + data.get("expires_in", 3600) - 60
        self._save_tokens()

    # ------------------------------------------------------------------
    # OAuth2 flow helpers (called from API endpoints)
    # ------------------------------------------------------------------
    def get_auth_url(self) -> str:
        """Build the Google OAuth2 consent screen URL."""
        if not self._client_id:
            raise ExecutorError("GMAIL_CLIENT_ID non configuré.")
        from urllib.parse import urlencode
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "scope": _SCOPES,
            "response_type": "code",
            "access_type": "offline",
            "prompt": "consent",
        }
        return f"{_GOOGLE_AUTH}?{urlencode(params)}"

    def exchange_code(self, code: str) -> dict[str, Any]:
        """Exchange an authorization code for tokens."""
        if not self._client_id or not self._client_secret:
            raise ExecutorError("GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET non configurés.")
        http = HttpClient(_GOOGLE_OAUTH)
        data = http.post(
            "/token",
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": self._redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self._tokens = {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", ""),
            "expires_at": time.time() + data.get("expires_in", 3600) - 60,
            "token_type": data.get("token_type", "Bearer"),
        }
        self._save_tokens()
        email = self._get_email_address()
        return {"connected": True, "email": email}

    def disconnect(self) -> dict[str, Any]:
        """Revoke tokens and clear stored credentials."""
        self._tokens = {}
        if _TOKEN_FILE.exists():
            _TOKEN_FILE.unlink()
        return {"connected": False}

    def get_status(self) -> dict[str, Any]:
        """Return connection status without throwing."""
        if not self._is_connected():
            return {"connected": False, "email": None}
        try:
            email = self._get_email_address()
            return {"connected": True, "email": email}
        except ExecutorError:
            return {"connected": False, "email": None}

    def _get_email_address(self) -> str:
        http = self._get_authenticated_http()
        data = http.get("/gmail/v1/users/me/profile")
        return data.get("emailAddress", "")

    # ------------------------------------------------------------------
    # Message helpers
    # ------------------------------------------------------------------
    def _list_messages(
        self, label: str = "INBOX", query: str = "", max_results: int = 20
    ) -> list[dict[str, Any]]:
        http = self._get_authenticated_http()
        params: dict[str, Any] = {"maxResults": max_results}
        label_id = _LABEL_MAP.get(label.lower(), label)
        if label_id:
            params["labelIds"] = label_id
        if query:
            params["q"] = query
        data = http.get("/gmail/v1/users/me/messages", params=params)
        messages = data.get("messages", [])
        if not messages:
            return []
        ids = [m["id"] for m in messages[:max_results]]
        result = []
        for mid in ids:
            msg = http.get(
                f"/gmail/v1/users/me/messages/{mid}",
                params={"format": "metadata"},
            )
            result.append(self._parse_message(msg))
        result.sort(key=lambda m: m.get("timestamp") or 0, reverse=True)
        return result

    def _get_message(self, msg_id: str) -> dict[str, Any]:
        http = self._get_authenticated_http()
        msg = http.get(
            f"/gmail/v1/users/me/messages/{msg_id}",
            params={"format": "full"},
        )
        parsed = self._parse_message(msg)
        parsed["body"] = self._extract_body(msg.get("payload", {}))
        parsed["body_html"] = self._extract_body_html(msg.get("payload", {}))
        parsed["attachments"] = self._extract_attachments(msg.get("payload", {}))
        parsed["snippet"] = msg.get("snippet", "")
        return parsed

    def _extract_attachments(self, payload: dict[str, Any]) -> list[dict[str, str]]:
        attachments: list[dict[str, str]] = []
        for part in payload.get("parts", []):
            filename = part.get("filename", "")
            if filename:
                attachments.append({
                    "filename": filename,
                    "mimeType": part.get("mimeType", ""),
                    "size": str(part.get("body", {}).get("size", 0)),
                    "attachmentId": part.get("body", {}).get("attachmentId", ""),
                })
            # Recurse into nested parts (e.g. multipart/mixed -> multipart/alternative)
            attachments.extend(self._extract_attachments(part))
        return attachments

    def _download_attachment(self, msg_id: str, att_id: str) -> dict[str, Any]:
        """Download an attachment by message ID and attachment ID."""
        http = self._get_authenticated_http()
        resp = http.get(
            f"/gmail/v1/users/me/messages/{msg_id}/attachments/{att_id}",
        )
        data = resp.get("data", "")
        if not data:
            return {"error": "No data returned for attachment"}
        decoded = base64.urlsafe_b64decode(data)
        return {"data_b64": base64.b64encode(decoded).decode("ascii")}

    def _parse_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        headers = {}
        for h in msg.get("payload", {}).get("headers", []):
            headers[h["name"].lower()] = h["value"]
        labels = msg.get("labelIds", [])
        raw_from = headers.get("from", "")
        name, email_addr = parseaddr(raw_from)
        timestamp = 0
        internal = msg.get("internalDate", "")
        if internal:
            try:
                timestamp = int(internal)
            except (TypeError, ValueError):
                timestamp = 0
        if not timestamp:
            date_header = headers.get("date", "")
            if date_header:
                try:
                    timestamp = int(parsedate_to_datetime(date_header).timestamp() * 1000)
                except (TypeError, ValueError, OverflowError):
                    timestamp = 0
        return {
            "id": msg.get("id", ""),
            "thread_id": msg.get("threadId", ""),
            "subject": headers.get("subject", ""),
            "from": raw_from,
            "from_name": name or email_addr or "Inconnu",
            "from_email": email_addr or "",
            "to": headers.get("to", ""),
            "date": headers.get("date", ""),
            "timestamp": timestamp,
            "snippet": msg.get("snippet", ""),
            "unread": "UNREAD" in labels,
            "labels": labels,
        }

    def _extract_body(self, payload: dict[str, Any]) -> str:
        mime = payload.get("mimeType", "")
        if mime == "text/plain":
            data = payload.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        for part in payload.get("parts", []):
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        for part in payload.get("parts", []):
            body = self._extract_body(part)
            if body:
                return body
        return payload.get("snippet", "")

    def _extract_body_html(self, payload: dict[str, Any]) -> str:
        mime = payload.get("mimeType", "")
        if mime == "text/html":
            data = payload.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        for part in payload.get("parts", []):
            if part.get("mimeType") == "text/html":
                data = part.get("body", {}).get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        for part in payload.get("parts", []):
            body = self._extract_body_html(part)
            if body:
                return body
        return ""

    def _send_message(
        self,
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        bcc: str = "",
        attachments: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        http = self._get_authenticated_http()
        if attachments:
            msg = MIMEMultipart()
            msg.attach(MIMEText(body, "plain", "utf-8"))
            for att in attachments:
                filename = att.get("filename", "file")
                content_b64 = att.get("content", "")
                mime_type = att.get("content_type", "application/octet-stream")
                main_type, sub_type = mime_type.split("/", 1) if "/" in mime_type else ("application", "octet-stream")
                part = MIMEBase(main_type, sub_type)
                part.set_payload(base64.urlsafe_b64decode(content_b64))
                encoders.encode_base64(part)
                part["Content-Disposition"] = _content_disposition(filename)
                msg.attach(part)
        else:
            msg = MIMEText(body, "plain", "utf-8")
        msg["to"] = to
        msg["subject"] = subject
        if cc:
            msg["cc"] = cc
        if bcc:
            msg["bcc"] = bcc
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        data = http.post(
            "/gmail/v1/users/me/messages/send",
            json={"raw": raw},
        )
        return {"id": data.get("id", ""), "thread_id": data.get("threadId", "")}

    def _reply_message(
        self,
        msg_id: str,
        body: str,
        to: str = "",
        cc: str = "",
        attachments: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Reply to an existing message, keeping it in the same thread."""
        http = self._get_authenticated_http()
        original = http.get(
            f"/gmail/v1/users/me/messages/{msg_id}",
            params={"format": "metadata"},
        )
        headers = {}
        for h in original.get("payload", {}).get("headers", []):
            headers[h["name"].lower()] = h["value"]

        thread_id = original.get("threadId", "")
        original_from = headers.get("from", "")
        original_subject = headers.get("subject", "")
        message_id_header = headers.get("message-id", "")

        # If to is empty or is just a name (no @), use original sender
        if not to or "@" not in to:
            to = original_from

        # Add Re: prefix if not already present
        subject = original_subject
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

        # Build MIME message
        if attachments:
            msg = MIMEMultipart()
            msg.attach(MIMEText(body, "plain", "utf-8"))
            for att in attachments:
                filename = att.get("filename", "file")
                content_b64 = att.get("content", "")
                mime_type = att.get("content_type", "application/octet-stream")
                main_type, sub_type = mime_type.split("/", 1) if "/" in mime_type else ("application", "octet-stream")
                part = MIMEBase(main_type, sub_type)
                part.set_payload(base64.urlsafe_b64decode(content_b64))
                encoders.encode_base64(part)
                part["Content-Disposition"] = _content_disposition(filename)
                msg.attach(part)
        else:
            msg = MIMEText(body, "plain", "utf-8")

        msg["to"] = to
        msg["subject"] = subject
        if cc:
            msg["cc"] = cc

        # Threading headers
        if message_id_header:
            msg["In-Reply-To"] = message_id_header
            msg["References"] = message_id_header

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        payload = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id

        data = http.post(
            "/gmail/v1/users/me/messages/send",
            json=payload,
        )
        return {"id": data.get("id", ""), "thread_id": data.get("threadId", "")}

    def _get_thread(self, thread_id: str) -> dict[str, Any]:
        """Fetch all messages in a thread."""
        http = self._get_authenticated_http()
        thread = http.get(
            f"/gmail/v1/users/me/threads/{thread_id}",
            params={"format": "full"},
        )
        messages = []
        for msg in thread.get("messages", []):
            parsed = self._parse_message(msg)
            parsed["body"] = self._extract_body(msg.get("payload", {}))
            parsed["body_html"] = self._extract_body_html(msg.get("payload", {}))
            parsed["attachments"] = self._extract_attachments(msg.get("payload", {}))
            parsed["snippet"] = msg.get("snippet", "")
            messages.append(parsed)
        messages.sort(key=lambda m: m.get("timestamp") or 0)
        return {
            "id": thread.get("id", ""),
            "messages": messages,
            "snippet": thread.get("snippet", ""),
        }

    def _create_draft(self, to: str, subject: str, body: str) -> dict[str, Any]:
        http = self._get_authenticated_http()
        msg = MIMEText(body, "plain", "utf-8")
        msg["to"] = to
        msg["subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        data = http.post(
            "/gmail/v1/users/me/drafts",
            json={"message": {"raw": raw}},
        )
        return {"id": data.get("id", ""), "message_id": (data.get("message") or {}).get("id", "")}

    def _delete_draft(self, draft_id: str) -> dict[str, Any]:
        http = self._get_authenticated_http()
        http.delete(f"/gmail/v1/users/me/drafts/{draft_id}")
        return {"deleted": True}

    def _trash_message(self, msg_id: str) -> dict[str, Any]:
        http = self._get_authenticated_http()
        data = http.post(f"/gmail/v1/users/me/messages/{msg_id}/trash")
        return {"id": data.get("id", ""), "labels": data.get("labelIds", [])}

    def _untrash_message(self, msg_id: str) -> dict[str, Any]:
        http = self._get_authenticated_http()
        data = http.post(f"/gmail/v1/users/me/messages/{msg_id}/untrash")
        return {"id": data.get("id", ""), "labels": data.get("labelIds", [])}

    def _list_drafts(self, max_results: int = 20) -> list[dict[str, Any]]:
        http = self._get_authenticated_http()
        data = http.get("/gmail/v1/users/me/drafts", params={"maxResults": max_results})
        drafts = data.get("drafts", [])
        if not drafts:
            return []
        result = []
        for d in drafts:
            msg_id = (d.get("message") or {}).get("id")
            if msg_id:
                msg = http.get(
                    f"/gmail/v1/users/me/messages/{msg_id}",
                    params={"format": "metadata", "metadataHeaders": "From,To,Subject,Date"},
                )
                parsed = self._parse_message(msg)
                parsed["draft_id"] = d.get("id", "")
                result.append(parsed)
            else:
                result.append({"id": "", "draft_id": d.get("id", ""), "subject": "", "from": "", "snippet": ""})
        result.sort(key=lambda m: m.get("timestamp") or 0, reverse=True)
        return result

    def _list_labels(self) -> list[dict[str, Any]]:
        http = self._get_authenticated_http()
        data = http.get("/gmail/v1/users/me/labels")
        return [{"id": l.get("id", ""), "name": l.get("name", ""), "type": l.get("type", "")} for l in data.get("labels", [])]

    # ------------------------------------------------------------------
    # DomainExecutor interface
    # ------------------------------------------------------------------
    def _clean_recipients(self, raw: Any) -> str:
        """Keep only fragments that are real email addresses.

        The attribute classifier sometimes emits whole sentence clauses as
        'recipients' when the query only refers to 'l'adresse email de la
        société' without stating it; those would produce 'Invalid To header'."""
        items = raw if isinstance(raw, list) else [raw]
        out: list[str] = []
        for item in items:
            if not isinstance(item, str):
                continue
            for part in re.split(r"[;,\s]+", item.strip()):
                part = part.strip()
                if "@" in part and part not in out:
                    out.append(part)
        return ", ".join(out)

    def _find_turn_invoice(self, ctx: ExecutionContext) -> dict[str, Any] | None:
        """Reuse an invoice posted earlier in the same turn (accounting domain)
        so its PDF can be attached to this email send."""
        if not ctx or not ctx.executor_log:
            return None
        for entry in reversed(ctx.executor_log):
            for action in entry.get("results") or []:
                data = action.get("data") or {}
                name = action.get("action") or ""
                if name.endswith(".action_invoice_download_pdf") and data.get("ok"):
                    invoices = (data.get("response") or {}).get("invoices") or []
                    if invoices:
                        return invoices[0]
        for entry in reversed(ctx.executor_log):
            for action in entry.get("results") or []:
                data = action.get("data") or {}
                name = action.get("action") or ""
                if name.endswith(".action_get") and data.get("ok"):
                    response = data.get("response") or {}
                    move = response.get("move")
                    if move:
                        return move
        for entry in reversed(ctx.executor_log):
            for action in entry.get("results") or []:
                data = action.get("data") or {}
                if action.get("action") == "account.move.create" and data.get("ok"):
                    params = action.get("params") or {}
                    response = data.get("response") or {}
                    if isinstance(response, dict) and response.get("id"):
                        return {
                            "id": response["id"],
                            "ref": params.get("invoice_number"),
                            "name": None,
                            "state": "draft",
                            "partner_id": params.get("customer"),
                        }
        return None

    def _odoo(self) -> Any | None:
        """Lazy, guarded handle to the Odoo executor for recipient + invoice
        data (imports deferred to avoid an import cycle with the registry)."""
        try:
            from server.execution.executors.registry import get_registry
            from server.execution.executors.odoo import OdooExecutor
        except Exception:
            return None
        candidate = get_registry().executor_for("contacts")
        return candidate if isinstance(candidate, OdooExecutor) else None

    def _resolve_recipient(self, params: dict[str, Any], invoice_ctx: dict[str, Any] | None) -> str:
        """Find the destination address when the query only refers to the
        customer ('à l'adresse email de la société'): read it from the Odoo
        partner of the invoice/company involved in the turn."""
        odoo = self._odoo()
        partner_ref: Any = None
        if invoice_ctx:
            pid = invoice_ctx.get("partner_id")
            if isinstance(pid, (list, tuple)) and pid:
                partner_ref = pid[0]
            elif pid:
                partner_ref = pid
        if not partner_ref:
            partner_ref = params.get("company") or params.get("customer") or ""
        if not partner_ref:
            raise ExecutorError(
                "email.email_submission.set : aucune adresse email destinataire trouvée dans "
                "la demande (l'adresse de la société n'a pas été donnée)."
            )
        if odoo is None:
            raise ExecutorError(
                "email.email_submission.set : aucune adresse email destinataire et backend Odoo "
                "indisponible pour la résoudre."
            )
        email = odoo.read_partner_email(partner_ref)
        if not email:
            raise ExecutorError(
                f"email.email_submission.set : le contact/la société {partner_ref!r} n'a pas "
                "d'adresse email renseignée dans Odoo."
            )
        return email

    def _load_invoice_pdf(self, invoice_ctx: dict[str, Any]) -> str:
        """Render the invoice PDF (via Odoo data) and return it urlsafe-b64."""
        odoo = self._odoo()
        if odoo is None:
            return ""
        ref = invoice_ctx.get("ref") or invoice_ctx.get("id") or invoice_ctx.get("name")
        if not ref:
            return ""
        try:
            packet = odoo.read_invoice_packet(ref)
            if not packet:
                return ""
            from server.execution.executors.invoice_pdf import render_invoice_pdf
            raw = render_invoice_pdf(packet)
            if not raw:
                return ""
            return base64.urlsafe_b64encode(raw).decode("ascii")
        except Exception:
            return ""

    def execute(self, ctx: ExecutionContext) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        runtime: dict[str, Any] = {"attached": [], "missing": []}
        for action, payload in ctx.actions.items():
            params = (payload or {}).get("fields", {})
            # Side effects (a real send) must not fire twice when the turn is
            # retried: the pipeline re-runs the whole domain on validation
            # failures, which used to e-mail the recipient N times.
            guard_key = self._idempotency_key(action, params, ctx)
            if guard_key:
                cached, _ = self._side_effect_at(guard_key)
                if cached is not None:
                    self._remember_attachment(self._turn_key(ctx), cached)
                    results.append(
                        action_result(
                            action,
                            params,
                            tool_calls=[f"gmail:{action}"],
                            data={
                                "ok": True,
                                "response": cached,
                                "duplicate_suppressed": True,
                            },
                        )
                    )
                    continue
            try:
                data = self._handle_action(action, params, ctx, runtime)
                if data is None:
                    results.append(deferred_action(action, params))
                    continue
                if guard_key and action in _SIDE_EFFECT_ACTIONS:
                    self._remember_side_effect(guard_key, data)
                    self._remember_attachment(self._turn_key(ctx), data)
                results.append(
                    action_result(
                        action,
                        params,
                        tool_calls=[f"gmail:{action}"],
                        data={"ok": True, "response": data},
                    )
                )
            except ExecutorError as exc:
                results.append(
                    action_result(
                        action,
                        params,
                        tool_calls=[f"gmail:{action}"],
                        data={"ok": False, "error": str(exc)},
                    )
                )
        return executor_result(ctx.domain, results, attempts=ctx.retry_count + 1)

    # ------------------------------------------------------------------
    # Idempotency guard for non-idempotent actions (sending an email).
    # ------------------------------------------------------------------
    @property
    def _attachments_sent(self) -> dict[str, list[str]]:
        """turn key -> file names actually attached to the message sent.

        Created lazily: tests build the executor with ``__new__``.
        """
        store = self.__dict__.get("_attachment_log")
        if store is None:
            store = {}
            self.__dict__["_attachment_log"] = store
        return store

    def _remember_attachment(self, turn_key: str, data: Any) -> None:
        """Record what a send carried, so `email.blob.upload` can report it.

        The action order is submission.set then blob.upload, so the upload
        action must be able to state which files travelled with the mail.
        Reading ctx.executor_log did NOT work: the current domain's own results
        are not in the log while it is executing, so the upload action reported
        "attached: []" and the validator concluded the file had not been sent.
        """
        if not isinstance(data, dict):
            return
        names = [str(n) for n in (data.get("attachment_names") or []) if n]
        if not names:
            return
        bucket = self._attachments_sent.setdefault(turn_key, [])
        for name in names:
            if name not in bucket:
                bucket.append(name)

    @staticmethod
    def _turn_key(ctx: ExecutionContext) -> str:
        return ctx.turn_id or f"{ctx.conversation_id}|{ctx.intent}"

    def _attachments_for_turn(self, ctx: ExecutionContext | None) -> list[str]:
        if ctx is None:
            return []
        return list(self._attachments_sent.get(self._turn_key(ctx)) or [])

    @property
    def _side_effects(self) -> dict[str, Any]:
        """Turn-scoped memo of completed side effects (replayed on retry).

        Created lazily: tests build the executor with ``__new__``.
        """
        store = self.__dict__.get("_side_effect_cache")
        if store is None:
            store = {}
            self.__dict__["_side_effect_cache"] = store
        return store

    def _side_effect_at(self, guard_key: str) -> tuple[Any, float]:
        store = self._side_effects
        if guard_key not in store:
            return None, 0.0
        sent_at = self._side_effect_times.get(guard_key, 0.0)
        if time.time() - sent_at > _SIDE_EFFECT_TTL_SECONDS:
            # A new turn asking for the same mail must really send it again.
            store.pop(guard_key, None)
            self._side_effect_times.pop(guard_key, None)
            return None, 0.0
        return store[guard_key], sent_at

    @property
    def _side_effect_times(self) -> dict[str, float]:
        times = self.__dict__.get("_side_effect_times_cache")
        if times is None:
            times = {}
            self.__dict__["_side_effect_times_cache"] = times
        return times

    def _idempotency_key(
        self, action: str, params: dict[str, Any], ctx: ExecutionContext
    ) -> str:
        if action not in _SIDE_EFFECT_ACTIONS:
            return ""
        # A turn id is generated per user message (graph.run_turn). The
        # conversation fallback is only a safety net: it must never make a NEW
        # request look like a retry of an old one, hence the TTL on lookups.
        turn = ctx.turn_id or f"{ctx.conversation_id}|{ctx.intent}"
        digest = hashlib.sha256(
            json.dumps(
                {
                    "a": action,
                    "to": params.get("recipients") or params.get("to") or "",
                    "cc": params.get("cc") or "",
                    "subject": params.get("subject") or "",
                    "body": params.get("content") or params.get("body_text") or "",
                    "files": self._referenced_filenames(params),
                },
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()[:16]
        return f"{ctx.domain}:{action}:{turn}:{digest}"

    def _remember_side_effect(self, key: str, data: Any) -> None:
        self._side_effects[key] = data
        self._side_effect_times[key] = time.time()
        if len(self._side_effects) > _SIDE_EFFECT_CACHE_MAX:
            for stale in list(self._side_effects)[: len(self._side_effects) - _SIDE_EFFECT_CACHE_MAX]:
                self._side_effects.pop(stale, None)
                self._side_effect_times.pop(stale, None)

    @staticmethod
    def _referenced_filenames(params: dict[str, Any]) -> list[str]:
        """File NAMES mentioned by the classifier, whatever shape it used.

        Accepts `attachment_names` / `file_names` / `attachments` as a string
        ("a.pdf, b.pdf"), a list of strings, or a list of attachment dicts.
        Anything else silently produced an empty list, which made the pipeline
        believe no attachment had been requested.
        """
        names: list[str] = []
        raw: Any = None
        for key in ("attachment_names", "file_names", "attachments", "attachment", "files"):
            value = params.get(key)
            if value not in (None, "", [], {}):
                raw = value
                break
        if isinstance(raw, str):
            raw = re.split(r"[,;]", raw)
        if isinstance(raw, dict):
            raw = [raw]
        for item in raw or []:
            if isinstance(item, dict):
                name = str(item.get("filename") or item.get("name") or "").strip()
            else:
                name = str(item).strip()
            if name:
                names.append(name)
        return sorted(set(names))

    def _normalize_attachments(
        self, params: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Split `params['attachments']` into real payloads and bare file names.

        The classifier is inconsistent: it may send attachment dicts (with
        base64 content), a bare filename string, or a list of names. Treating
        the string as a sequence produced one bogus MIME part per CHARACTER,
        and dicts without content produced empty parts that Gmail drops, so the
        mail went out with no usable attachment.
        """
        real: list[dict[str, Any]] = []
        names: list[str] = []
        raw: Any = params.get("attachments")
        items: list[Any]
        if raw in (None, "", [], {}):
            items = []
        elif isinstance(raw, str):
            items = re.split(r"[,;]", raw)
        elif isinstance(raw, dict):
            items = [raw]
        elif isinstance(raw, (list, tuple)):
            items = list(raw)
        else:
            items = [raw]
        for item in items:
            if isinstance(item, dict):
                content = item.get("content") or item.get("content_b64") or ""
                if content:
                    real.append(
                        {
                            "filename": item.get("filename") or item.get("name") or "file",
                            "content_type": item.get("content_type")
                            or _guess_content_type(str(item.get("filename") or "")),
                            "content": content,
                        }
                    )
                else:
                    # No bytes: it is a reference to a stored file, resolve by name.
                    name = str(item.get("filename") or item.get("name") or "").strip()
                    if name:
                        names.append(name)
            else:
                name = str(item).strip()
                if name:
                    names.append(name)
        return real, names

    def _files_named_in_intent(self, intent: str) -> list[str]:
        """Document names mentioned in the request ("... avec rapport.pdf").

        Only known document extensions count, so email addresses and domains
        (user@gmail.com) are never mistaken for files.
        """
        if not intent:
            return []
        doc_exts = tuple(_CONTENT_TYPES) + (".odt", ".rtf", ".md", ".json", ".log")
        found: list[str] = []
        pattern = r"[\"'\u00ab\u00bb]?([^\s\"'\u00ab\u00bb@]{2,80}\.[A-Za-z0-9]{1,8})[\"'\u00ab\u00bb]?"
        for match in re.finditer(pattern, intent, flags=re.IGNORECASE):
            candidate = match.group(1).strip(" .,;:!?()[]")
            if not candidate or "@" in candidate:
                continue
            if candidate.casefold().endswith(doc_exts):
                found.append(candidate)
        return sorted(set(found))

    def _resolve_named_attachments(
        self,
        params: dict[str, Any],
        ctx: ExecutionContext | None,
        extra_names: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Turn `attachment_names` (classifier output) into real files.

        The classifier only knows file *names*; the bytes live in the turn
        uploads (ctx.attachments) or in the Viito file storage. Returns
        ``(attachments, missing_names)`` so an unresolvable file is reported
        instead of silently sending a mail without it.
        """
        wanted = list(self._referenced_filenames(params))
        for extra in extra_names or []:
            if extra not in wanted:
                wanted.append(extra)
        # The classifier sometimes drops the file entirely and keeps it only in
        # the request text ("... avec le rapport final en piece jointe").
        for extra in self._files_named_in_intent((ctx.intent if ctx else "") or ""):
            if extra not in wanted:
                wanted.append(extra)
        if not wanted:
            return [], []
        resolved: list[dict[str, Any]] = []
        missing: list[str] = []
        for name in wanted:
            found = self._lookup_attachment(name, ctx)
            if found:
                resolved.append(found)
            else:
                missing.append(name)
        return resolved, missing

    def _lookup_attachment(
        self, name: str, ctx: ExecutionContext | None
    ) -> dict[str, Any] | None:
        target = self._norm_name(name)
        # 1. files uploaded with the current turn
        for att in (ctx.attachments if ctx else []) or []:
            filename = str(att.get("filename") or "")
            if not self._norm_name(filename):
                continue
            if self._name_matches(target, self._norm_name(filename)):
                content = att.get("content") or att.get("content_b64") or ""
                if content:
                    return {
                        "filename": filename or name,
                        "content_type": att.get("content_type")
                        or _guess_content_type(filename),
                        "content": content,
                    }
        # 2. files already in the Viito storage (uploads, generated documents)
        try:
            from server.execution.executors.file_storage import FileStorage

            storage = FileStorage()
            path = self._find_in_storage(storage, target)
            if path:
                raw = storage.read_file(path) or b""
                if raw:
                    return {
                        "filename": path.split("/")[-1],
                        "content_type": _guess_content_type(path),
                        "content": base64.urlsafe_b64encode(raw).decode("ascii"),
                    }
        except Exception:
            return None
        return None

    def _find_in_storage(self, storage: Any, target: str) -> str:
        exact = storage.resolve(target) if hasattr(storage, "resolve") else None
        if exact:
            return exact
        stack = [""]
        seen = 0
        while stack and seen < 200:
            seen += 1
            current = stack.pop(0)
            try:
                entries = storage.list_dir(current)
            except Exception:
                entries = []
            for entry in entries:
                path = entry.get("path") or ""
                name = entry.get("name") or path.split("/")[-1]
                if entry.get("is_dir") or entry.get("type") == "dir":
                    stack.append(path)
                    continue
                if self._name_matches(target, self._norm_name(name)):
                    return path
        return ""

    @staticmethod
    def _norm_name(value: str) -> str:
        return re.sub(r"[\s_]+", " ", str(value or "")).strip().casefold()

    @staticmethod
    def _name_matches(target: str, candidate: str) -> bool:
        if not target or not candidate:
            return False
        return (
            target == candidate
            or candidate.startswith(target)
            or target.startswith(candidate)
            or target in candidate
        )

    def _handle_action(
        self,
        action: str,
        params: dict[str, Any],
        ctx: ExecutionContext | None = None,
        runtime: dict[str, Any] | None = None,
    ) -> Any | None:
        if action == "email.email_submission.set":
            to = self._clean_recipients(params.get("recipients"))
            attachments, named_only = self._normalize_attachments(params)
            invoice_ctx = self._find_turn_invoice(ctx) if ctx else None
            if not to:
                to = self._resolve_recipient(params, invoice_ctx)
            if invoice_ctx and not any(
                (a.get("filename") or "").lower().endswith(".pdf") for a in attachments
            ):
                pdf_b64 = self._load_invoice_pdf(invoice_ctx)
                if pdf_b64:
                    name = (
                        invoice_ctx.get("name")
                        or invoice_ctx.get("ref")
                        or invoice_ctx.get("id")
                        or "invoice"
                    )
                    attachments.append({
                        "filename": f"{name}.pdf",
                        "content_type": "application/pdf",
                        "content": pdf_b64,
                    })
            if not attachments and ctx and ctx.attachments:
                attachments = [
                    {
                        "filename": a.get("filename", "file"),
                        "content_type": a.get("content_type", "application/octet-stream"),
                        "content": a.get("content") or a.get("content_b64", ""),
                    }
                    for a in ctx.attachments
                ]
            # The classifier names files in `attachment_names` (no bytes): pull
            # them from the turn uploads so "envoyer ... avec <fichier>" really
            # ships the file instead of a text-only mail.
            named, missing = self._resolve_named_attachments(
                params, ctx, extra_names=named_only
            )
            if named:
                have = {(a.get("filename") or "").casefold() for a in attachments}
                attachments.extend(
                    a for a in named if (a.get("filename") or "").casefold() not in have
                )
            sent_names = [a.get("filename") for a in attachments if a.get("filename")]
            response = self._send_message(
                to=to,
                subject=params.get("subject", ""),
                body=params.get("content") or params.get("body_text") or "",
                cc=params.get("cc", ""),
                attachments=attachments if attachments else None,
            )
            if isinstance(response, dict) and (sent_names or missing):
                response = dict(response)
                if runtime is not None:
                    runtime["attached"] = sent_names
                    runtime["missing"] = missing
                if sent_names:
                    response["attachment_names"] = sent_names
                if missing:
                    # Be explicit instead of claiming a file went out.
                    response["missing_attachments"] = missing
            return response
        if action in ("email.blob.upload", "email.email.set"):
            # Gmail has no separate blob endpoint: attachments travel inline with
            # the send/reply action. Reporting a deferred action here made the
            # pipeline retry the whole domain (re-sending the mail) and then
            # report a failure for an email that had been delivered.
            names = self._referenced_filenames(params)
            live = runtime or {}
            attached = list(live.get("attached") or [])
            missing = list(live.get("missing") or [])
            if not attached and not missing:
                # Later attempt (router retry): read the send back from the log,
                # otherwise the ack looks like the file was never attached.
                attached = self._attachments_for_turn(ctx)
                missing = [n for n in names if n not in attached]
            return {
                "ok": True,
                "handled_by": "email.email_submission.set",
                "note": "Gmail sends attachments inline with the message; no separate upload needed",
                "requested": names,
                "attached": attached,
                "missing": missing,
            }
        if action == "email.email.query":
            query = params.get("search_query") or params.get("query") or ""
            return {"emails": self._list_messages("inbox", query=query)}
        if action == "email.email.get":
            msg_id = params.get("email") or params.get("id") or ""
            if msg_id and not str(msg_id).startswith("http"):
                return self._get_message(str(msg_id))
            return None
        if action == "email.email.search_snippet":
            query = params.get("search_query") or params.get("query") or ""
            return {"emails": self._list_messages("inbox", query=query)}
        if action == "email.mailbox.query":
            return {"mailboxes": self._list_labels()}
        if action == "email.mailbox.get":
            name = params.get("mailbox") or "inbox"
            msgs = self._list_messages(name)
            return {"mailbox": name, "messages": msgs, "count": len(msgs)}
        if action == "email.mailbox.create":
            return None  # Gmail doesn't support custom labels via this interface simply
        if action in ("email.blob.get", "email.blob.lookup"):
            query = params.get("email_subject") or ""
            return {"emails": self._list_messages("inbox", query=query)}
        if action == "email.email.set":
            return None
        if action == "email.email.reply":
            msg_id = params.get("email") or params.get("id") or params.get("message_id") or ""
            # If no message ID, try to find the email by sender name or subject
            if not msg_id:
                search_query = params.get("email") or params.get("sender") or params.get("recipients") or ""
                if isinstance(search_query, list):
                    search_query = search_query[0] if search_query else ""
                if search_query:
                    msgs = self._list_messages("inbox", query=f"from:{search_query}", max_results=1)
                    if msgs:
                        msg_id = msgs[0].get("id", "")
            if not msg_id:
                # Try searching in sent as well
                search_query = params.get("recipients") or ""
                if isinstance(search_query, list):
                    search_query = search_query[0] if search_query else ""
                if search_query:
                    msgs = self._list_messages("sent", query=f"to:{search_query}", max_results=1)
                    if msgs:
                        msg_id = msgs[0].get("id", "")
            if not msg_id:
                return None
            to = params.get("recipients") or ""
            if isinstance(to, list):
                to = ", ".join(to)
            attachments = params.get("attachments") or []
            if not attachments and ctx and ctx.attachments:
                attachments = [
                    {
                        "filename": a.get("filename", "file"),
                        "content_type": a.get("content_type", "application/octet-stream"),
                        "content": a.get("content") or a.get("content_b64", ""),
                    }
                    for a in ctx.attachments
                ]
            return self._reply_message(
                msg_id=str(msg_id),
                body=params.get("content") or params.get("body_text") or "",
                to=to,
                cc=params.get("cc", ""),
                attachments=attachments if attachments else None,
            )
        return None
