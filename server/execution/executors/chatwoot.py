"""Messaging domain executor backed by the Chatwoot REST API (localhost:3000).

Implements the ``messaging.*`` action set from ``tools/attr_defs/messaging.json``
against Chatwoot's /api/v1/accounts/{id}/... endpoints. Auth is the account
access token from the environment (CHATWOOT_ACCESS_TOKEN) — never hardcoded,
never read from the secrets file at call time.

Live-verified actions send/read/toggle real objects; the rest are deferred.
"""

from __future__ import annotations

import os
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

# Actions implemented against the live Chatwoot instance.
_CREATE_CONVERSATION = "messaging.conversation.create"
_READ_CONVERSATION = "messaging.conversation.read"
_UPDATE_STATUS = "messaging.conversation.toggle_status"
_UPDATE_PRIORITY = "messaging.conversation.toggle_priority"
_ASSIGN_CONVERSATION = "messaging.conversation.assign"
_SEND_MESSAGE = "messaging.message.send"
_LIST_MESSAGES = "messaging.conversation.messages"
_CREATE_CONTACT = "messaging.contact.create"


class ChatwootExecutor(DomainExecutor):
    domains: tuple[str, ...] = ("messaging",)
    backend = "chatwoot"

    def __init__(self) -> None:
        base_url = os.environ.get("CHATWOOT_URL", "http://localhost:3000")
        token = os.environ.get("CHATWOOT_ACCESS_TOKEN", "")
        self.account_id = os.environ.get("CHATWOOT_ACCOUNT_ID", "2")
        self._http = HttpClient(
            base_url, headers={"api_access_token": token} if token else {}
        )

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------
    def _path(self, suffix: str = "") -> str:
        return f"/api/v1/accounts/{self.account_id}{suffix}"

    def _get(self, suffix: str = "", **kw: Any) -> Any:
        return self._http.get(self._path(suffix), **kw)

    def _post(self, suffix: str, **kw: Any) -> Any:
        return self._http.post(self._path(suffix), **kw)

    # ------------------------------------------------------------------
    # Resolvers (id or fuzzy name -> backend id)
    # ------------------------------------------------------------------
    def _resolve_ids(
        self, suffix: str, value: Any, id_field: str
    ) -> list[str]:
        """Resolve a target reference to backend ids via the search endpoint.

        ``value`` may be an int/str id ("12", 12) or a display name.
        """
        if value is None:
            return []
        text = str(value).strip()
        if text.isdigit():
            return [text]
        try:
            payload = self._get(suffix, params={"q": text})
        except ExecutorError:
            return []
        items = payload.get("payload") or payload if isinstance(payload, dict) else payload
        ids = []
        if isinstance(items, list):
            for item in items:
                name = "".join(
                    str(item.get(k) or "")
                    for k in ("name", "conversation_name", "title", "subject")
                )
                if not name or text.lower() in name.lower() or name.lower() in text.lower():
                    ids.append(str(item.get(id_field)))
        return [i for i in ids if i]

    def _resolve_conversation(self, value: Any) -> str | None:
        ids = self._resolve_ids("/conversations/search", value, "id")
        return ids[0] if ids else None

    def _resolve_contact(self, value: Any) -> str | None:
        ids = self._resolve_ids("/contacts/search", value, "id")
        return ids[0] if ids else None

    def _resolve_inbox(self, value: Any) -> str | None:
        try:
            inboxes = self._get("/inboxes")
        except ExecutorError:
            return None
        payload = inboxes.get("payload") if isinstance(inboxes, dict) else inboxes
        if not isinstance(payload, list):
            return None
        text = str(value or "").strip()
        for inbox in payload:
            if text and text.lower() in str(inbox.get("name", "")).lower():
                return str(inbox.get("id"))
        return str(payload[0].get("id")) if payload else None  # default inbox

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _create_contact(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name") or params.get("customer")
        email = params.get("email")
        if not name:
            raise ExecutorError("messaging.contact.create: missing 'name'")
        payload: dict[str, Any] = {"name": str(name)}
        if email:
            payload["email"] = str(email)
        response = self._post("/contacts", json=payload)
        contact = (response or {}).get("payload", {}).get("contact", {})
        return {"id": contact.get("id"), "contact": contact}

    def _create_conversation(self, params: dict[str, Any]) -> dict[str, Any]:
        contact_id = self._resolve_contact(params.get("customer"))
        if not contact_id:
            contact = self._create_contact(
                {"name": params.get("customer") or "New contact"}
            )
            contact_id = str(contact.get("id"))
        inbox_id = self._resolve_inbox(params.get("inbox"))
        payload: dict[str, Any] = {
            "contact_id": int(contact_id),
            "message": {"content": params.get("message") or ""},
        }
        if inbox_id:
            payload["inbox_id"] = int(inbox_id)
        return self._post("/conversations", json=payload)

    def _toggle_status(self, conversation_id: str, params: dict[str, Any]) -> dict[str, Any]:
        status = (params.get("status") or "open").lower().strip()
        if status not in ("open", "pending", "resolved", "snoozed"):
            status = "open"
        payload: dict[str, Any] = {"status": status}
        if params.get("snooze_until") and status == "snoozed":
            payload["snoozed_until"] = str(params["snooze_until"])
        return self._post(
            f"/conversations/{conversation_id}/toggle_status", json=payload
        )

    def _toggle_priority(self, conversation_id: str, params: dict[str, Any]) -> dict[str, Any]:
        priority = (params.get("priority") or "none").lower().strip()
        payload = {"priority": priority}
        return self._post(
            f"/conversations/{conversation_id}/toggle_priority", json=payload
        )

    def _assign(self, conversation_id: str, params: dict[str, Any]) -> dict[str, Any]:
        assignee = params.get("assignee")
        if assignee is None:
            raise ExecutorError("messaging.conversation.assign: missing 'assignee'")
        assignee_id: str | int = assignee
        if not isinstance(assignee, int) and str(assignee).strip().isdigit():
            assignee_id = int(assignee)
        else:
            agents = self._get("/agents")
            payload = agents.get("payload") if isinstance(agents, dict) else agents
            if isinstance(payload, list):
                for agent in payload:
                    if str(assignee).lower() in str(agent.get("name", "")).lower():
                        assignee_id = int(agent.get("id"))
                        break
        return self._post(
            f"/conversations/{conversation_id}/assignments",
            json={"assignee_id": assignee_id},
        )

    def _send_message(self, conversation_id: str, params: dict[str, Any]) -> dict[str, Any]:
        content = params.get("message_text") or params.get("message")
        if not content:
            raise ExecutorError("messaging.message.send: missing 'message_text'")
        payload: dict[str, Any] = {"content": str(content)}
        if params.get("message_type"):
            payload["message_type"] = str(params["message_type"])
        return self._post(
            f"/conversations/{conversation_id}/messages", json=payload
        )

    def _list_messages(self, conversation_id: str) -> dict[str, Any]:
        return self._get(f"/conversations/{conversation_id}/messages")

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    def execute(self, ctx: ExecutionContext) -> dict[str, Any]:
        results: list[dict[str, Any]] = []

        for action, payload in ctx.actions.items():
            params = (payload or {}).get("fields", {})
            try:
                if action == _CREATE_CONVERSATION:
                    data = self._create_conversation(params)
                elif action == _READ_CONVERSATION:
                    cid = self._resolve_conversation(params.get("conversation"))
                    data = (
                        self._get(f"/conversations/{cid}") if cid
                        else {"error": "conversation not found"}
                    )
                elif action == _UPDATE_STATUS:
                    cid = self._resolve_conversation(params.get("conversation"))
                    data = self._toggle_status(cid, params) if cid else {"error": "conversation not found"}
                elif action == _UPDATE_PRIORITY:
                    cid = self._resolve_conversation(params.get("conversation"))
                    data = self._toggle_priority(cid, params) if cid else {"error": "conversation not found"}
                elif action == _ASSIGN_CONVERSATION:
                    cid = self._resolve_conversation(params.get("conversation"))
                    data = self._assign(cid, params) if cid else {"error": "conversation not found"}
                elif action == _SEND_MESSAGE:
                    cid = self._resolve_conversation(params.get("conversation"))
                    data = self._send_message(cid, params) if cid else {"error": "conversation not found"}
                elif action == _LIST_MESSAGES:
                    cid = self._resolve_conversation(params.get("conversation"))
                    data = self._list_messages(cid) if cid else {"error": "conversation not found"}
                elif action == _CREATE_CONTACT:
                    data = self._create_contact(params)
                else:
                    results.append(deferred_action(action, params))
                    continue

                results.append(
                    action_result(
                        action,
                        params,
                        tool_calls=[f"chatwoot:{action}"],
                        data={"ok": True, "response": data},
                    )
                )
            except ExecutorError as exc:
                results.append(
                    action_result(
                        action,
                        params,
                        tool_calls=[f"chatwoot:{action}"],
                        data={"ok": False, "error": str(exc)},
                    )
                )

        return executor_result(
            ctx.domain, results, attempts=ctx.retry_count + 1
        )