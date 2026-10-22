"""Stalwart executor (JMAP) — serves the email_action domain.

Talks to the Stalwart JMAP endpoint (RFC 8620/8621) over Basic auth. Supports
send (Email/set draft + EmailSubmission/set), email search/read and mailbox
management. Unsupported JMAP methods are deferred honestly.

Live-verified against the running Stalwart container: Email/set created a
draft, EmailSubmission/set accepted the submission (id returned, sendAt set),
Email/query + Email/get and Mailbox/get round-tripped.
"""

from __future__ import annotations

import base64
import os
from typing import Any
from urllib.parse import urlparse

from server.execution.executors.base import (
    DomainExecutor,
    ExecutorError,
    ExecutionContext,
    action_result,
    deferred_action,
    executor_result,
)
from server.execution.executors.http import HttpClient

SEND_ACTIONS = {"email.email_submission.set"}
QUERY_ACTIONS = {"email.email.query", "email.email.search_snippet", "email.email.get"}
MAILBOX_ACTIONS = {
    "email.mailbox.query",
    "email.mailbox.create",
    "email.mailbox.get",
    "email.mailbox.set",
    "email.mailbox.rename",
}
_DEFERRED_PREFIXES = ("email.blob.", "email.thread.")


class StalwartExecutor(DomainExecutor):
    domains: tuple[str, ...] = ("email_action",)
    backend = "stalwart"

    def __init__(self) -> None:
        base = os.environ.get("STALWART_URL", "http://localhost:8089")
        self.admin_email = os.environ.get("STALWART_ADMIN_EMAIL", "admin@localhost")
        self.admin_password = os.environ.get("STALWART_ADMIN_PASSWORD", "")
        self.default_sender = os.environ.get("STALWART_SENDER", "") or self.admin_email
        self._http = HttpClient(base)
        self._session: Any = None
        self._account_id: str | None = None
        self._api_path = "/jmap/"
        self._identity_id: str | None = None
        self._mailbox_ids: dict[str, str] = {}

    # ------------------------------------------------------------------
    def _auth(self) -> dict[str, str]:
        import base64

        token = base64.b64encode(f"{self.admin_email}:{self.admin_password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def _session_info(self) -> tuple[str, str]:
        if self._account_id:
            return self._account_id, self._api_path
        session = self._http.get(
            "/jmap/session", headers={"Accept": "application/json", **self._auth()}
        )
        account_id = (session.get("primaryAccounts") or {}).get(
            "urn:ietf:params:jmap:mail"
        )
        if not account_id or not account_id[0]:
            raise ExecutorError("stalwart: no mail account in JMAP session")
        self._session = session
        self._account_id = account_id[0]
        api_url = str(session.get("apiUrl", "/jmap/"))
        self._api_path = urlparse(api_url).path or "/jmap/"  # advertise https, serve http
        return self._account_id, self._api_path

    def _jmap(self, calls: list[list[Any]], using: list[str]) -> list[Any]:
        account_id, api_path = self._session_info()
        body = {
            "using": using,
            "methodCalls": calls,
        }
        data = self._http.post(api_path, json=body, headers=self._auth())
        responses = data.get("methodResponses", [])
        for name, item, _call_id in responses:
            if name == "error":
                raise ExecutorError(f"stalwart jmap error: {item.get('description')}")
        return responses

    def _run(self, name: str, args: dict[str, Any], using: list[str]) -> dict[str, Any]:
        responses = self._jmap([[name, args, "c0"]], using)
        _rname, response, _cid = responses[0]
        not_created = response.get("notCreated") or response.get("notUpdated")
        if not_created:
            key = next(iter(not_created))
            err = not_created[key]
            raise ExecutorError(f"stalwart {name}: {err.get('description')}")
        return response

    def _mailbox_id(self, role: str) -> str:
        self._session_info()
        if role not in self._mailbox_ids:
            resp = self._run(
                "Mailbox/get",
                {"accountId": self._account_id, "ids": None, "properties": ["id", "name", "role"]},
                ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"],
            )
            for box in resp.get("list", []):
                role_key = box.get("role") or box["name"].lower()
                self._mailbox_ids.setdefault(role_key, box["id"])
        return self._mailbox_ids[role]

    def _identity(self, sender: str | None = None) -> str:
        self._session_info()
        resp = self._run(
            "Identity/get",
            {"accountId": self._account_id},
            ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail", "urn:ietf:params:jmap:submission"],
        )
        identities = resp.get("list", [])
        if not identities:
            raise ExecutorError("stalwart: no email identity configured")
        if sender:
            for ident in identities:
                if (ident.get("email") or "").lower() == sender.lower():
                    self._identity_id = ident["id"]
                    return self._identity_id
        if self._identity_id:
            return self._identity_id
        self._identity_id = identities[0]["id"]
        return self._identity_id

    # ------------------------------------------------------------------
    def execute(self, ctx: ExecutionContext) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for action, payload in ctx.actions.items():
            params = (payload or {}).get("fields", {})
            try:
                if action in SEND_ACTIONS:
                    data = self._send_email(params)
                elif action in QUERY_ACTIONS:
                    data = self._query_emails(params, action)
                elif action in MAILBOX_ACTIONS:
                    data = self._mailbox(params, action)
                elif action.startswith(_DEFERRED_PREFIXES):
                    data = None
                else:
                    data = None
                if data is None:
                    results.append(deferred_action(action, params))
                    continue
                results.append(
                    action_result(
                        action, params,
                        tool_calls=[f"stalwart:{action}"],
                        data={"ok": True, "response": data},
                    )
                )
            except ExecutorError as exc:
                results.append(
                    action_result(
                        action, params,
                        tool_calls=[f"stalwart:{action}"],
                        data={"ok": False, "error": str(exc)},
                    )
                )
        return executor_result(ctx.domain, results, attempts=ctx.retry_count + 1)

    # ------------------------------------------------------------------
    def _as_list(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            parts = [p.strip() for p in value.split(",")]
            return [p for p in parts if p]
        if isinstance(value, list):
            return [str(v) for v in value]
        return [str(value)]

    def _upload_blob(self, content: bytes, content_type: str) -> str:
        """Upload a binary blob via JMAP Blob/upload and return its blobId."""
        account_id, api_path = self._session_info()
        resp = self._http.post(
            f"{api_path}upload/{account_id}",
            headers={"Content-Type": content_type, **self._auth()},
            data=content,
        )
        if not isinstance(resp, dict):
            raise ExecutorError("stalwart: blob upload returned unexpected response")
        blob_id = resp.get("blobId")
        if not blob_id:
            raise ExecutorError("stalwart: blob upload failed")
        return blob_id

    def _send_email(self, params: dict[str, Any]) -> dict[str, Any]:
        using = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail", "urn:ietf:params:jmap:submission"]
        self._session_info()
        recipients = self._as_list(params.get("recipients"))
        if not recipients:
            raise ExecutorError("stalwart: no recipients for email submission")
        sender = params.get("sender_identity") or self.default_sender
        subject = params.get("subject") or ""
        content = params.get("content") or params.get("body_text") or ""
        drafts = self._mailbox_id("drafts")
        account = self._account_id

        attachments = []
        for att in params.get("attachments") or []:
            name = att.get("filename") or att.get("name") or att.get("attachment_name") or "attachment.bin"
            mime_type = att.get("content_type") or att.get("type") or "application/octet-stream"
            content_b64 = att.get("content") or att.get("content_b64") or ""
            if not content_b64:
                continue
            try:
                raw = base64.urlsafe_b64decode(str(content_b64))
            except Exception:
                continue
            blob_id = self._upload_blob(raw, mime_type)
            attachments.append({
                "blobId": blob_id,
                "type": mime_type,
                "name": name.replace('"', ""),
                "size": len(raw),
            })

        draft_body: dict[str, Any] = {
            "accountId": account,
            "create": {"draft": {
                "mailboxIds": {drafts: True},
                "from": [{"email": sender}],
                "to": [{"email": r} for r in recipients],
                "cc": [{"email": c} for c in self._as_list(params.get("cc"))],
                "subject": subject,
                "keywords": {"$draft": True},
                "textBody": [{"partId": "b", "type": "text/plain"}],
                "bodyValues": {"b": {"value": content, "charset": "utf-8"}},
            }},
        }
        if attachments:
            draft_body["create"]["draft"]["attachments"] = attachments
        replies = self._jmap([["Email/set", draft_body, "c1"]], using)
        created = replies[0][1] if replies else {}
        email_id = (created.get("created") or {}).get("draft", {}).get("id")
        if not email_id:
            err = (created.get("notCreated") or {}).get("draft", {})
            raise ExecutorError(f"stalwart: draft create failed: {err.get('description')}")
        sent = self._mailbox_id("sent")
        sub = self._run(
            "EmailSubmission/set",
            {
                "accountId": account,
                "create": {"send": {
                    "emailId": email_id,
                    "identityId": self._identity(sender),
                    "envelope": {"mailFrom": {"email": sender}},
                }},
                "onSuccessUpdateEmail": {"send": {"mailboxIds/%s" % sent: True, "keywords/$draft": None}},
            },
            using,
        )
        created_sub = (sub.get("created") or {}).get("send", {})
        return {
            "email_id": email_id,
            "submission_id": created_sub.get("id"),
            "send_at": created_sub.get("sendAt"),
        }

    def _query_emails(self, params: dict[str, Any], action: str) -> Any:
        using = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"]
        self._session_info()
        query = params.get("search_query") or params.get("query") or params.get("subject") or ""
        filter_body: dict[str, Any] = {"text": query} if query else {}
        resp = self._run(
            "Email/query",
            {"accountId": self._account_id, "filter": filter_body, "limit": 10},
            using,
        )
        ids = resp.get("ids", [])
        if action == "email.email.query":
            return {"ids": ids, "total": len(ids)}
        get = self._run(
            "Email/get",
            {"accountId": self._account_id, "ids": ids, "properties": ["id", "subject", "from", "to", "sentAt", "mailboxIds", "keywords", "preview"]},
            using,
        )
        return {"emails": get.get("list", [])}

    def _mailbox(self, params: dict[str, Any], action: str) -> Any:
        using = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"]
        self._session_info()
        if action == "email.mailbox.query":
            resp = self._run(
                "Mailbox/get",
                {"accountId": self._account_id, "ids": None, "properties": ["id", "name", "role", "totalEmails"]},
                using,
            )
            return {"mailboxes": resp.get("list", [])}
        if action == "email.mailbox.get":
            name = params.get("mailbox") or params.get("box_query") or "Inbox"
            resp = self._run(
                "Mailbox/get",
                {"accountId": self._account_id, "ids": None, "properties": ["id", "name", "role", "totalEmails", "totalUnread"]},
                using,
            )
            match = [b for b in resp.get("list", []) if b["name"].lower() == name.lower()]
            return {"mailbox": match[0] if match else None}
        if action == "email.mailbox.create":
            name = params.get("mailbox") or params.get("name") or params.get("box_query")
            if not name:
                raise ExecutorError("stalwart: mailbox name required for create")
            resp = self._run(
                "Mailbox/set",
                {"accountId": self._account_id, "create": {"m": {"name": name}}},
                using,
            )
            created = (resp.get("created") or {}).get("m", {})
            return {"id": created.get("id")}
        # set / rename on an existing mailbox
        name = params.get("mailbox")
        if not name:
            raise ExecutorError("stalwart: mailbox required")
        existing = self._mailbox(params, "email.mailbox.get")
        if not existing.get("mailbox"):
            raise ExecutorError(f"stalwart: mailbox '{name}' not found")
        mid = existing["mailbox"]["id"]
        if action == "email.mailbox.rename":
            new_name = params.get("target_mailbox") or params.get("name")
            if not new_name:
                raise ExecutorError("stalwart: target_mailbox required for rename")
            vals = {"name": new_name}
        else:
            vals = {k: v for k, v in params.items() if k in ("isSubscribed",)}
        resp = self._run(
            "Mailbox/set",
            {"accountId": self._account_id, "update": {mid: vals}},
            using,
        )
        return {"id": mid, "updated": True}