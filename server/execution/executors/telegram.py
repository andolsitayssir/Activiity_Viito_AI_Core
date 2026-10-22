"""Messaging domain executor backed by the user's OWN Telegram account.

Implements the ``messaging.*`` action set from ``tools/attr_defs/messaging.json``
through Telegram's MTProto (Telethon) using a personal session — NOT a bot, so
the account sees all its conversations, contacts and channels, and can send
to anyone without the target having to write first.

Auth is configured from the webapp config page (or env TELEGRAM_API_ID /
TELEGRAM_API_HASH / TELEGRAM_PHONE) with the code flow: api_id + api_hash
(from my.telegram.org) + phone, then the one-time code (and 2FA password if
the account has one). The authorized session is persisted in
``memory/telegram_session.session`` and survives server restarts.

Real capabilities served:
  * message.send -> sendMessage (private chats, groups, supergroups, channels),
  * conversation.messages/read -> getMessages(chat) + mark-as-read,
  * conversation.create -> start a private chat (by @username / phone / phone
    contact) and optionally send the first message,
  * message.update / message.delete -> editMessage / deleteMessages,
  * conversation.delete -> leave a channel/supergroup or erase a private chat
    history,
  * conversation.mute / unmute -> UpdateNotifySettings (mute_until),
  * conversation.participants -> invite members into a group/supergroup,
  * group.create / channel.create -> CreateChannelRequest (supergroup +
    members, or broadcast) with an optional first message,
  * get_dialogs aggregation that feeds the webapp Conversations/Channels tabs.

Telegram has no "conversation status/priority/assignee" inbox model, so those
Chatwoot-style actions are honestly deferred.

All MTProto calls are coroutine-based: they run on a single dedicated event
loop in a background thread (``_LoopBridge``), so sync callers (pipeline,
FastAPI routes) stay simple and the Telethon client keeps a stable loop.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import re
import threading
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from server.execution.executors.base import (
    DomainExecutor,
    ExecutorError,
    ExecutionContext,
    action_result,
    deferred_action,
    executor_result,
)

_MEMORY_DIR = Path(__file__).resolve().parents[3] / "memory"
_CREDS_FILE = _MEMORY_DIR / "telegram_credentials.json"
_SESSION_FILE = _MEMORY_DIR / "telegram_session.session"
_STORE_FILE = _MEMORY_DIR / "telegram_store.json"

# Actions mapped to real Telegram calls.
_SEND_MESSAGE = "messaging.message.send"
_CREATE_CONVERSATION = "messaging.conversation.create"
_DELETE_CONVERSATION = "messaging.conversation.delete"
_LIST_MESSAGES = "messaging.conversation.messages"
_READ_CONVERSATION = "messaging.conversation.read"
_MUTE_CONVERSATION = "messaging.conversation.mute"
_UNMUTE_CONVERSATION = "messaging.conversation.unmute"
_PARTICIPANTS = "messaging.conversation.participants"
_UPDATE_MESSAGE = "messaging.message.update"
_DELETE_MESSAGE = "messaging.message.delete"
_CREATE_GROUP = "messaging.group.create"
_CREATE_CHANNEL = "messaging.channel.create"

_MESSAGE_LIMIT = 50
_MUTE_FAR_FUTURE = datetime(2038, 1, 1)

_CRED_ENV = {
    "api_id": "TELEGRAM_API_ID",
    "api_hash": "TELEGRAM_API_HASH",
    "phone": "TELEGRAM_PHONE",
}


class TelegramNeedsPasswordError(ExecutorError):
    """The account has 2FA: the code is valid but a password is required."""


class _LoopBridge:
    """Runs a single event loop on a daemon thread forever.

    ``submit(coro)`` schedules the coroutine on that loop and blocks the
    calling thread until it finishes — so every Telethon operation stays on
    the same loop while sync code (pipeline, webapp) keeps a plain API.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def ensure_started(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                loop = asyncio.new_event_loop()
                thread = threading.Thread(
                    target=loop.run_forever, name="viito-telethon", daemon=True
                )
                thread.start()
                self._loop = loop
                self._thread = thread

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        self.ensure_started()
        return self._loop

    def submit(self, coro: Any, timeout: int = 120) -> Any:
        self.ensure_started()
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise ExecutorError(
                "Telegram : opération en timeout (réseau injoignable ?)"
            ) from None


def _entity_name(entity: Any) -> str:
    """Human title for a Telethon entity (chat/user/channel)."""
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    first = getattr(entity, "first_name", None)
    last = getattr(entity, "last_name", None)
    if first and last:
        return f"{first} {last}"
    if first:
        return str(first)
    username = getattr(entity, "username", None)
    return str(username) if username else "Dialogue"


def _fold(value: str) -> str:
    """Lowercase, accents stripped — for fuzzy name matching."""
    return (
        unicodedata.normalize("NFD", value or "")
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )


def _parse_dt(value: Any) -> datetime:
    """Best-effort datetime parsing for mute/snooze fields."""
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return _MUTE_FAR_FUTURE


# Same context separator as the webapp pipeline: appended document/file text is
# extraction noise, so target-from-intent parsing stops at the first marker.
_INTENT_CONTEXT_SEP = re.compile(r"\n\n---\n|\n\n\[Pi[eè]ce[s]? join(?:te[s]?|e)?:|\[Document|\[Fichier")

_TARGET_PATTERNS = (
    r"\bconversation\s+avec\s+([^\n,.;]{2,80})",
    r"\bchat\s+avec\s+([^\n,.;]{2,80})",
    r"\bauprès de\s+([@\w][^\n,.;]{2,80})",
    r"\bavec\s+([@\w][^\n,.;]{2,80})",
    r"\bvers\s+([@\w][^\n,.;]{2,80})",
    r"\bsur\s+([@\w][^\n,.;]{2,80})",
    r"@([\w]{3,32})",
)

_TARGET_STOPWORDS = {
    _fold(_s)
    for _s in (
        "lui", "elle", "leur", "eux", "moi", "toi", "ce", "cet", "cette", "ces",
        "la conversation", "cette conversation", "le chat", "ce chat", "telegram",
    )
}


def _target_from_query(query: str) -> str | None:
    """Best-effort extraction of the target person/chat name from a raw user
    query (used only when attribute extraction left the fields empty)."""
    text = str(query or "").strip()
    if not text:
        return None
    cut = _INTENT_CONTEXT_SEP.search(text)
    if cut:
        text = text[: cut.start()].strip()
    candidate: str | None = None
    for pattern in _TARGET_PATTERNS:
        match = re.search(pattern, text)
        if match:
            candidate = match.group(1).strip()
            break
    if not candidate:
        return None
    candidate = re.sub(r"^(le |la |les |l'|du |de la |des |au |aux )", "", candidate).strip()
    candidate = candidate.rstrip(" .,;:!?")
    folded = _fold(candidate)
    if folded in _TARGET_STOPWORDS or len(candidate) < 2:
        return None
    return candidate


class TelegramExecutor(DomainExecutor):
    domains: tuple[str, ...] = ("messaging",)
    backend = "telegram"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bridge = _LoopBridge()
        self._client: TelegramClient | None = None
        self._phone = ""
        self._pending_code = False
        self._code_hash: str | None = None
        self._store: dict[str, Any] | None = None
        # Test seam: when set, _run() executes coroutines synchronously and no
        # client/loop provisioning happens (see _make_telegram in the tests).
        self._testing = False

    # ------------------------------------------------------------------
    # Credentials (env first, then UI-persisted file).
    # ------------------------------------------------------------------
    def _load_credentials(self) -> dict[str, str]:
        data: dict[str, Any] = {}
        if _CREDS_FILE.exists():
            try:
                parsed = json.loads(_CREDS_FILE.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    data = parsed
            except (json.JSONDecodeError, OSError):
                data = {}
        return {
            key: (os.environ.get(env, "").strip() or str(data.get(key, "") or ""))
            for key, env in _CRED_ENV.items()
        }

    def save_config(self, api_id: str, api_hash: str, phone: str) -> None:
        """Persist api_id/api_hash/phone from the config page and re-provision."""
        _MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        _CREDS_FILE.write_text(
            json.dumps(
                {
                    "api_id": (api_id or "").strip(),
                    "api_hash": (api_hash or "").strip(),
                    "phone": (phone or "").strip(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        self._pending_code = False
        self._code_hash = None
        self._client = None
        self._provision_client()

    # ------------------------------------------------------------------
    # Telethon client provisioning.
    # ------------------------------------------------------------------
    def _provision_client(self) -> None:
        if self._testing:
            return
        if self._client is not None:
            return
        cfg = self._load_credentials()
        if not cfg["api_id"] or not cfg["api_hash"]:
            raise ExecutorError(
                "Telegram non configuré : api_id / api_hash manquants "
                "(my.telegram.org -> API development tools)."
            )
        if not cfg["phone"]:
            raise ExecutorError("Telegram non configuré : numéro de téléphone manquant.")
        self._phone = cfg["phone"]
        self._client = TelegramClient(
            str(_SESSION_FILE),
            int(cfg["api_id"]),
            cfg["api_hash"],
            loop=self._bridge.loop,
        )

    def _call(
        self,
        coro: Any,
        timeout: int = 120,
    ) -> Any:
        """Ensure the client is connected, then run ``coro`` (already created)
        on the Telethon loop thread."""
        if self._client is None:
            self._provision_client()
        if self._client is None:
            raise ExecutorError("Telegram non configuré.")
        return self._run(self._async_ensure(coro), timeout=timeout)

    def _run(self, coro: Any, timeout: int = 120) -> Any:
        if self._testing:
            return asyncio.run(coro)
        return self._bridge.submit(coro, timeout=timeout)

    # ------------------------------------------------------------------
    # Async internals (all operate on self._client).
    # ------------------------------------------------------------------
    async def _async_ensure(self, coro: Any) -> Any:
        client = self._client
        if client is not None and not client.is_connected():
            await client.connect()
        return await coro

    async def _async_is_authorized(self) -> bool:
        return bool(await self._client.is_user_authorized())

    async def _async_me_brief(self) -> dict[str, Any]:
        me = await self._client.get_me()
        return {
            "id": getattr(me, "id", None),
            "first_name": getattr(me, "first_name", None),
            "last_name": getattr(me, "last_name", None),
            "username": getattr(me, "username", None),
            "phone": getattr(me, "phone", None),
        }

    async def _async_send_code(self, phone: str) -> bool:
        sent = await self._client.send_code_request(phone)
        self._code_hash = getattr(sent, "phone_code_hash", None)
        self._pending_code = True
        return True

    async def _async_sign_in_code(self, code: str) -> Any:
        kwargs: dict[str, Any] = {
            "code": code,
            "phone_code_hash": self._code_hash,
        }
        if self._phone:
            kwargs["phone"] = self._phone
        return await self._client.sign_in(**kwargs)

    async def _async_sign_in_password(self, password: str) -> Any:
        return await self._client.sign_in(password=password)

    async def _async_logout(self) -> bool:
        return bool(await self._client.log_out())

    async def _async_get_dialogs(self) -> list[Any]:
        return await self._client.get_dialogs(limit=None)

    async def _async_get_entity(self, value: Any) -> Any:
        return await self._client.get_entity(value)

    async def _async_send_message(self, peer: Any, text: str) -> Any:
        return await self._client.send_message(peer, str(text))

    async def _async_get_messages(self, chat_id: int, limit: int = _MESSAGE_LIMIT) -> list[Any]:
        raw = await self._client.get_messages(chat_id, limit=limit)
        return list(raw or [])

    async def _async_contacts(self) -> list[Any]:
        return await self._client.get_contacts()

    async def _async_search_users(self, query: str, limit: int = 20) -> list[Any]:
        from telethon.tl.functions.contacts import SearchRequest

        result = await self._client(SearchRequest(q=str(query), limit=limit))
        return list(getattr(result, "users", None) or [])

    async def _async_add_contact(self, entity_id: int, first_name: str, last_name: str) -> Any:
        """Add a known Telegram user to the account's phonebook."""
        from telethon.tl.functions.contacts import AddContactRequest
        from telethon.tl.types import InputUser

        entity = await self._client.get_entity(entity_id)
        access_hash = getattr(entity, "access_hash", None)
        if access_hash is None:
            raise ExecutorError("messaging: contact non résolu.")
        first = first_name or (getattr(entity, "first_name", "") or "")
        last = last_name or (getattr(entity, "last_name", "") or "")
        return await self._client(
            AddContactRequest(
                id=InputUser(user_id=int(getattr(entity, "id", 0)), access_hash=access_hash),
                first_name=first,
                last_name=last,
                phone="",
                add_phone_privacy_exception=True,
            )
        )

    async def _async_edit_message(self, chat_id: int, msg_id: int, text: str) -> Any:
        return await self._client.edit_message(chat_id, msg_id, str(text))

    async def _async_delete_messages(self, chat_id: int, msg_ids: list[int]) -> Any:
        return await self._client.delete_messages(chat_id, msg_ids)

    async def _async_mark_read(self, chat_id: int) -> Any:
        return await self._client.send_read_acknowledge(
            await self._client.get_input_entity(chat_id)
        )

    async def _async_send_to_entity(self, entity: Any, text: str) -> Any:
        return await self._client.send_message(entity, str(text))

    async def _async_update_mute(self, chat_id: int, until: datetime | None) -> Any:
        from telethon.tl.functions.account import UpdateNotifySettingsRequest
        from telethon.tl.types import InputPeerNotifySettings

        peer = await self._client.get_input_entity(chat_id)
        settings = InputPeerNotifySettings(
            show_previews=True, silent=True, mute_until=until, sound=None
        )
        return await self._client(UpdateNotifySettingsRequest(peer=peer, settings=settings))

    async def _async_create_channel_entity(self, title: str, about: str, megagroup: bool) -> Any:
        from telethon.tl.functions.channels import CreateChannelRequest

        return await self._client(
            CreateChannelRequest(
                title=title, about=about, broadcast=not megagroup, megagroup=megagroup
            )
        )

    async def _async_invite_members(self, chat_id: int, member_ids: list[int]) -> Any:
        from telethon.tl.functions.channels import InviteToChannelRequest
        from telethon.tl.functions.messages import AddChatUserRequest
        from telethon.tl.types import Channel

        entity = await self._client.get_entity(chat_id)
        input_users = [await self._client.get_input_entity(mid) for mid in member_ids]
        if isinstance(entity, Channel):
            return await self._client(
                InviteToChannelRequest(
                    channel=await self._client.get_input_entity(entity), users=input_users
                )
            )
        return await self._client(AddChatUserRequest(chat_id=chat_id, user_id=input_users[0], fwd_limit=100))

    async def _async_leave_channel(self, peer: Any) -> Any:
        from telethon.tl.functions.channels import LeaveChannelRequest

        return await self._client(
            LeaveChannelRequest(channel=await self._client.get_input_entity(peer))
        )

    async def _async_erase_history(self, peer: Any) -> Any:
        from telethon.tl.functions.messages import DeleteHistoryRequest

        return await self._client(
            DeleteHistoryRequest(
                peer=await self._client.get_input_entity(peer), max_id=0, just_clear=False
            )
        )

    # ------------------------------------------------------------------
    # Login flow (config page).
    # ------------------------------------------------------------------
    def get_status(self) -> dict[str, Any]:
        """configured / connected / awaiting_code + the signed-in profile."""
        cfg = self._load_credentials()
        configured = False
        connected = False
        me: dict[str, Any] | None = None
        awaiting = False

        if not (cfg.get("api_id") and cfg.get("api_hash") and cfg.get("phone")):
            return {
                "connected": False,
                "configured": False,
                "awaiting_code": False,
                "me": None,
                "phone": cfg.get("phone", ""),
            }
        configured = True

        if self._client is None and not self._testing:
            try:
                self._provision_client()
            except ExecutorError:
                pass
        if self._client is not None:
            try:
                if self._run(self._async_is_authorized(), timeout=30):
                    connected = True
                    try:
                        me = self._run(self._async_me_brief(), timeout=30)
                    except Exception:
                        me = None
            except Exception:
                connected = False
            awaiting = bool(self._pending_code) and not connected

        return {
            "connected": connected,
            "configured": configured,
            "awaiting_code": awaiting,
            "me": me,
            "phone": cfg.get("phone", ""),
        }

    def send_code(self) -> None:
        """Request the one-time login code (SMS/app)."""
        if self._client is None:
            self._provision_client()
        cfg = self._load_credentials()
        if not cfg["phone"]:
            raise ExecutorError("Numéro de téléphone requis.")
        self._phone = cfg["phone"]
        try:
            self._call(self._async_send_code(cfg["phone"]), timeout=90)
        except ExecutorError as exc:
            self._pending_code = False
            raise exc

    def login(self, code: str, password: str | None = None) -> dict[str, Any]:
        """Complete the login. Raises TelegramNeedsPasswordError when the
        account has 2FA (call again with ``password``)."""
        if self._client is None:
            self._provision_client()
        if not self._pending_code:
            raise ExecutorError("Aucun code demandé : lancez « Envoyer le code » d'abord.")
        try:
            if password:
                self._run(self._async_sign_in_password(password), timeout=90)
            else:
                self._run(self._async_sign_in_code(code), timeout=90)
        except SessionPasswordNeededError:
            # Code accepted, cloud password required: keep pending so the next
            # call signs in with the password.
            raise TelegramNeedsPasswordError(
                "Ce compte Telegram a activé la vérification en deux étapes (2FA). "
                "Saisissez le mot de passe du compte."
            ) from None
        except ExecutorError:
            self._pending_code = False
            self._code_hash = None
            raise
        self._pending_code = False
        self._code_hash = None
        return self.get_status()

    def logout(self) -> dict[str, Any]:
        if self._client is not None:
            try:
                self._run(self._async_logout(), timeout=60)
            except Exception:
                pass
        self._pending_code = False
        self._code_hash = None
        self._client = None
        try:
            _SESSION_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        return {"connected": False}

    # ------------------------------------------------------------------
    # Local store (dialogs cache), shared with the webapp.
    # ------------------------------------------------------------------
    def _default_store(self) -> dict[str, Any]:
        return {"chats": {}, "channels": {}}

    def _load_store(self) -> dict[str, Any]:
        if self._store is None:
            try:
                if _STORE_FILE.exists():
                    data = json.loads(_STORE_FILE.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and "chats" in data:
                        self._store = data
            except (json.JSONDecodeError, OSError):
                pass
            if self._store is None:
                self._store = self._default_store()
                self._save_store(self._store)
        return self._store

    def _save_store(self, store: dict[str, Any]) -> None:
        self._store = store
        try:
            _STORE_FILE.write_text(
                json.dumps(store, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Normalization (Telethon objects -> plain dicts for the webapp/UI).
    # ------------------------------------------------------------------
    @staticmethod
    def _dialog_type(dialog: Any) -> str:
        if getattr(dialog, "is_channel", False):
            return "channel"
        if getattr(dialog, "is_group", False):
            return "group"
        if getattr(dialog, "is_private", False):
            return "private"
        return "chat"

    def _dialog_to_chat(self, dialog: Any) -> dict[str, Any]:
        entity = getattr(dialog, "entity", None)
        last_msg = getattr(getattr(dialog, "message", None), "message", None)
        date = getattr(dialog, "date", None)
        private = getattr(dialog, "is_private", False) or self._dialog_type(dialog) == "private"
        phone = getattr(entity, "phone", None) if private else None
        return {
            "chat_id": str(getattr(dialog, "id", 0)),
            "title": getattr(dialog, "name", None) or _entity_name(entity),
            "type": self._dialog_type(dialog),
            "username": getattr(entity, "username", None),
            "phone": str(phone) if phone else None,
            "last_message": ((last_msg or "") or "")[:300],
            "last_date": getattr(date, "timestamp", lambda: None)(),
            "messages": 0,
        }

    def _dialog_for(self, chat_id: int) -> dict[str, Any] | None:
        store = self._load_store()
        key = str(chat_id)
        for bucket in (store.get("chats", {}), store.get("channels", {})):
            if key in bucket:
                return bucket[key]
        return None

    def _message_to_dict(self, message: Any, dialog: dict[str, Any] | None) -> dict[str, Any]:
        text = getattr(message, "message", None) or ""
        if getattr(message, "media", None) and not text:
            if getattr(message, "photo", None):
                text = "(photo)"
            elif getattr(message, "voice", None):
                text = "(message vocal)"
            elif getattr(message, "sticker", None):
                text = "(sticker)"
            elif getattr(message, "document", None):
                text = "(document)"
            else:
                text = "(média)"
        out = bool(getattr(message, "out", False))
        if dialog and dialog.get("type") == "private":
            from_name = "Moi" if out else (dialog.get("title") or "Contact")
        else:
            from_name = "Moi" if out else "Membre"
        date = getattr(message, "date", None)
        return {
            "message_id": getattr(message, "id", None),
            "date": getattr(date, "timestamp", lambda: None)(),
            "direction": "out" if out else "in",
            "from_id": getattr(message, "sender_id", None),
            "from_name": from_name,
            "text": text,
        }

    # ------------------------------------------------------------------
    # Ingest: pull all dialogs (get_dialogs), cache contacts + channels.
    # ------------------------------------------------------------------
    def refresh(self) -> int:
        """Pull the account's dialogs. Returns the number of dialogs cached.
        Raises ExecutorError when not connected."""
        dialogs = self._call(self._async_get_dialogs())
        items = [self._dialog_to_chat(d) for d in dialogs or []]
        for chat in items:
            if chat.get("type") == "private" and not chat.get("phone"):
                chat["phone"] = self._odoo_phone(chat.get("title") or "")
        with self._lock:
            store = self._load_store()
            for chat in items:
                target = (
                    store["channels"] if chat["type"] == "channel" else store["chats"]
                )
                target[chat["chat_id"]] = chat
            self._save_store(store)
        return len(items)

    # ------------------------------------------------------------------
    # Target resolution (numeric id, @username, phone, contact name, title).
    # ------------------------------------------------------------------
    def _resolve_user(self, value: Any) -> dict[str, str]:
        """Resolve a PERSON to their Telegram user id: phone number, @username,
        or a full/first name searched among the account's phone contacts, the
        global public search, then the Odoo contact book (phone lookup).
        Never guesses: prefers an exact/unique match, else asks for more."""
        text = str(value or "").strip()
        if not text:
            raise ExecutorError("messaging: cible manquante")

        digits = re.sub(r"[\s\-().]", "", text)
        if re.fullmatch(r"\+?\d{5,16}", digits):
            phone = digits if digits.startswith("+") else "+" + digits
            try:
                entity = self._call(self._async_get_entity(phone))
            except Exception:
                raise ExecutorError(
                    f"messaging: aucun utilisateur Telegram avec le numéro {phone}."
                ) from None
            return {
                "id": str(getattr(entity, "id", 0)),
                "label": _entity_name(entity),
                "username": (getattr(entity, "username", None) or ""),
                "entity": entity,
            }

        if text.startswith("@"):
            try:
                entity = self._call(self._async_get_entity(text))
            except Exception:
                raise ExecutorError(f"messaging: aucun utilisateur « {text} » trouvé.") from None
            return {
                "id": str(getattr(entity, "id", 0)),
                "label": _entity_name(entity),
                "username": (getattr(entity, "username", None) or ""),
                "entity": entity,
            }

        try:
            contacts = self._call(self._async_contacts()) or []
        except Exception:
            contacts = []
        needle = _fold(text)
        matches: list[Any] = []
        for contact in contacts:
            full = _fold(
                f"{getattr(contact, 'first_name', '') or ''} "
                f"{getattr(contact, 'last_name', '') or ''} "
                f"{getattr(contact, 'username', '') or ''}"
            )
            if needle and needle in full:
                matches.append(contact)
        if len(matches) == 1:
            contact = matches[0]
            return {
                "id": str(getattr(contact, "id", 0)),
                "label": _entity_name(contact),
                "username": (getattr(contact, "username", None) or ""),
                "entity": contact,
                "via": "contacts",
            }
        if len(matches) > 1:
            resolved = self._odoo_resolved(self._odoo_phone(text))
            if resolved:
                return resolved
            labels = ", ".join(_entity_name(m) for m in matches[:5])
            raise ExecutorError(
                f"messaging: {len(matches)} contacts correspondent à « {text} » ({labels}). "
                "Précisez l'@username ou le numéro."
            )

        # Global public search fallback (any Telegram user, not just the
        # account's phone contacts) so a known name still resolves.
        found: list[Any] = []
        try:
            search_results = self._call(self._async_search_users(text)) or []
        except Exception:
            search_results = []
        for user in search_results:
            full = _fold(
                f"{getattr(user, 'first_name', '') or ''} "
                f"{getattr(user, 'last_name', '') or ''} "
                f"{getattr(user, 'username', '') or ''}"
            )
            if needle and needle in full:
                found.append(user)
        if len(found) == 1:
            user = found[0]
            return {
                "id": str(getattr(user, "id", 0)),
                "label": _entity_name(user),
                "username": (getattr(user, "username", None) or ""),
                "entity": user,
                "via": "search",
            }
        if len(found) > 1:
            # The Odoo contact book disambiguates the many public profiles:
            # the partner's stored phone points at the right Telegram user.
            resolved = self._odoo_resolved(self._odoo_phone(text))
            if resolved:
                return resolved
            labels = ", ".join(_entity_name(u) for u in found[:5])
            raise ExecutorError(
                f"messaging: {len(found)} profils publics « {text} » ({labels}). "
                "Précisez l'@username ou le numéro."
            )

        # Last resort: the Odoo contact book (res.partner) may carry the phone.
        phone = self._odoo_phone(text)
        if phone:
            resolved = self._odoo_resolved(phone)
            if resolved:
                return resolved
            raise ExecutorError(
                f"messaging: « {text} » trouvé dans vos contacts Odoo "
                f"(tél. {phone}), mais ce numéro n'a aucun compte Telegram actif."
            )
        raise ExecutorError(
            f"messaging: personne « {text} » introuvable dans vos contacts. "
            "Donnez un @username ou un numéro de téléphone."
        )

    def _odoo(self) -> Any:
        """The registered Odoo executor (contacts domain), or None."""
        try:
            from server.execution.executors.registry import get_registry
            from server.execution.executors.odoo import OdooExecutor
        except Exception:
            return None
        candidate = get_registry().executor_for("contacts")
        return candidate if isinstance(candidate, OdooExecutor) else None

    def _odoo_phone(self, text: str) -> str | None:
        """Phone of the Odoo partner whose name matches ``text``, if Odoo is
        reachable and the contact exists (tolerant: None on any failure)."""
        odoo = self._odoo()
        if odoo is None:
            return None
        try:
            return odoo.read_partner_phone(text)
        except Exception:
            return None

    def _odoo_resolved(self, phone: str | None) -> dict[str, Any] | None:
        """Resolve an Odoo partner phone to a Telegram user, tagging the result
        so the conversation can add the contact afterwards."""
        if not phone:
            return None
        resolved = self._resolve_phone(phone)
        if not resolved:
            return None
        resolved["via"] = "odoo"
        resolved["phone"] = str(phone)
        return resolved

    def _resolve_phone(self, phone: str) -> dict[str, str] | None:
        """Resolve a phone number to a Telegram user, trying common formats:
        as-is, with a leading ``+``, and the Tunisi +216 prefix for an
        8-digit national number."""
        digits = re.sub(r"[\s\-().]+", "", str(phone)).lstrip("+")
        if not digits or not re.fullmatch(r"\d{5,16}", digits):
            return None
        candidates = ["+" + digits]
        if len(digits) == 8 and not digits.startswith("216"):
            candidates.append("+216" + digits)
        for candidate in candidates:
            try:
                entity = self._call(self._async_get_entity(candidate))
            except Exception:
                continue
            return {
                "id": str(getattr(entity, "id", 0)),
                "label": _entity_name(entity),
                "username": (getattr(entity, "username", None) or ""),
                "entity": entity,
            }
        return None

    def _resolve_entity(self, chat: dict[str, Any]) -> Any:
        """A full Telegram entity (with access_hash) for a resolved chat dict.

        A bare numeric id is often only known as a ``PeerUser`` without an
        access_hash (min entity from a name/phone search), so re-resolving by
        phone or @username first guarantees an InputPeer can be built."""
        entity = chat.get("entity")
        if entity is not None:
            return entity
        phone = chat.get("phone")
        if phone:
            try:
                return self._call(self._async_get_entity(phone))
            except Exception:
                pass
        username = chat.get("username")
        if username:
            try:
                return self._call(self._async_get_entity("@" + username))
            except Exception:
                pass
        return self._call(self._async_get_entity(int(chat["id"])))

    @staticmethod
    def _as_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(v) for v in value if str(v).strip()]
        return [p.strip() for p in re.split(r"[,\n;]+", str(value)) if p.strip()]

    def _resolve_message_ref(self, chat_id: str, ref: Any) -> int:
        """Return a numeric message id from a ref: an explicit id, « dernier
        message », or a textual fragment matched against the chat history."""
        text = str(ref or "").strip()
        if not text:
            raise ExecutorError("messaging.message: un identifiant de message est requis")
        if re.fullmatch(r"-?\d+", text):
            return int(text)
        low = text.lower()
        if "dernier" in low or "derniere" in low or "last" in low:
            outgoing = [
                m["message_id"]
                for m in self.list_messages(chat_id)["messages"]
                if m.get("direction") == "out"
            ]
            if outgoing:
                return outgoing[-1]
            raise ExecutorError("messaging.message: aucun message envoyé récent trouvé.")
        for message in reversed(self.list_messages(chat_id)["messages"]):
            if text.lower() in (message.get("text") or "").lower():
                return message["message_id"]
        raise ExecutorError(f"messaging.message: aucun message contenant « {text} ».")

    def _resolve_chat(self, value: Any) -> dict[str, str]:
        """Map a target reference to a chat id + label. Best-effort, never
        guessed: titles must match a cached dialog, usernames/phones resolve
        live, and a bare person name searches the account's phone contacts."""
        if value is None:
            raise ExecutorError("messaging: cible de chat manquante")
        text = str(value).strip()

        if re.fullmatch(r"-?\d+", text):
            return {"id": text, "label": f"chat {text}"}

        store = self._load_store()
        pool = {**store.get("chats", {}), **store.get("channels", {})}

        if text.startswith("@"):
            username = text[1:].lower()
            for chat in pool.values():
                if str(chat.get("username") or "").lower() == username:
                    return {"id": str(chat["chat_id"]), "label": chat.get("title") or text}
            try:
                entity = self._call(self._async_get_entity(text))
            except Exception:
                raise ExecutorError(f"messaging: aucune conversation « {text} » trouvée.") from None
            return {
                "id": str(getattr(entity, "id", text)),
                "label": _entity_name(entity),
            }

        for chat in pool.values():
            hay = f"{chat.get('title', '')} {chat.get('username', '')}".lower()
            if text.lower() in hay or hay in text.lower():
                return {"id": str(chat["chat_id"]), "label": chat.get("title") or text}

        # A person name (not in the dialog cache): search the phone contacts so
        # a private chat can be started with someone we never exchanged with.
        try:
            return self._resolve_user(text)
        except ExecutorError:
            pass

        raise ExecutorError(
            f"messaging: aucune conversation connue pour « {text} ». "
            "Vérifiez le titre ou rafraîchissez les conversations Telegram. "
            "Si c'est une personne, précisez son @username ou son numéro."
        )

    # ------------------------------------------------------------------
    # Actions.
    # ------------------------------------------------------------------
    def _send_message(self, chat: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        content = params.get("message_text") or params.get("message")
        if not content:
            raise ExecutorError("messaging.message.send: missing 'message_text'")
        sent = self.send_chat_message(chat["id"], str(content), chat.get("entity"))
        return {"chat_id": sent["chat_id"], "message_id": sent.get("message_id"), "sent": sent}

    def _log_local_chat(self, chat_id: str, title: str, chat_type: str, phone: str | None = None) -> None:
        store = self._load_store()
        bucket = store["channels"] if chat_type == "channel" else store["chats"]
        bucket[chat_id] = {
            "chat_id": chat_id,
            "title": title,
            "type": chat_type,
            "username": None,
            "phone": phone,
            "last_message": "",
            "last_date": None,
            "messages": 0,
        }
        self._save_store(store)

    def _create_conversation(self, params: dict[str, Any]) -> dict[str, Any]:
        """Start a private chat with a person (contact / @username / phone) and
        optionally send the first message; a participant list creates a group."""
        participants = self._as_list(params.get("participants"))
        first_message = params.get("message_text") or params.get("message")
        title = str(params.get("title") or params.get("subject") or "").strip()
        if participants:
            return self._create_group(title or "Groupe Telegram", participants, first_message)
        target = (
            params.get("conversation")
            or params.get("contact")
            or params.get("customer")
            or title
            or params.get("assignee")
        )
        if not target:
            raise ExecutorError(
                "messaging.conversation.create: la cible (contact, titre, @username ou numéro) est requise."
            )
        user = self._resolve_user(target)
        chat_id = user["id"]
        added = False
        if user.get("via") == "odoo":
            # The person was found through the Odoo phonebook: add them to the
            # Telegram contacts while we start the conversation.
            label = user.get("label") or ""
            first, _, last = label.partition(" ")
            try:
                self._call(self._async_add_contact(int(chat_id), first, last or ""))
                added = True
            except Exception:
                added = False
        if first_message:
            self.send_chat_message(chat_id, str(first_message), user.get("entity"))
        if chat_id not in self._load_store()["chats"]:
            self._log_local_chat(chat_id, user["label"], "private", user.get("phone"))
        return {
            "chat_id": chat_id,
            "title": user["label"],
            "type": "private",
            "message_sent": bool(first_message),
            "added_to_contacts": added,
        }

    def _try_resolve_user(self, value: Any) -> dict[str, str] | None:
        """Like ``_resolve_user`` but returns None instead of raising, so callers
        can degrade gracefully and give their own guidance."""
        try:
            return self._resolve_user(value)
        except ExecutorError:
            return None

    def _create_group(
        self, title: str, participants: list[str], first_message: Any = None
    ) -> dict[str, Any]:
        members = [int(self._resolve_user(p)["id"]) for p in participants]
        created = self._call(
            self._async_create_channel_entity(title, "", megagroup=True)
        )
        chat_id = str(getattr(created, "id", 0))
        if members:
            self._call(self._async_invite_members(int(chat_id), members))
        if first_message:
            self._send_entity_message(created, str(first_message))
        self._log_local_chat(chat_id, title, "group")
        return {"chat_id": chat_id, "title": title, "type": "group", "members": members}

    def _create_group_action(self, params: dict[str, Any]) -> dict[str, Any]:
        participants = self._as_list(params.get("participants"))
        if not participants:
            raise ExecutorError(
                "messaging.group.create: indiquez au moins un membre (@username ou numéro)."
            )
        title = str(params.get("title") or params.get("subject") or "").strip() or "Groupe Telegram"
        return self._create_group(title, participants, params.get("message_text") or params.get("message"))

    def _create_channel_action(self, params: dict[str, Any]) -> dict[str, Any]:
        title = str(params.get("title") or params.get("subject") or "").strip() or "Canal Telegram"
        about = str(params.get("about") or params.get("description") or "").strip()
        first_message = params.get("message_text") or params.get("message")
        created = self._call(
            self._async_create_channel_entity(title, about, megagroup=False)
        )
        chat_id = str(getattr(created, "id", 0))
        if first_message:
            self._send_entity_message(created, str(first_message))
        self._log_local_chat(chat_id, title, "channel")
        return {"chat_id": chat_id, "title": title, "type": "channel", "message_sent": bool(first_message)}

    def _send_entity_message(self, entity: Any, text: str) -> dict[str, Any]:
        message = self._call(self._async_send_to_entity(entity, text))
        result = self._message_to_dict(message, None)
        result["chat_id"] = str(getattr(entity, "id", 0))
        return result

    def _update_message(self, params: dict[str, Any]) -> dict[str, Any]:
        target = params.get("conversation") or params.get("chat_id")
        new_text = params.get("message_text")
        if not target:
            raise ExecutorError("messaging.message.update: le champ conversation est requis.")
        if not new_text:
            raise ExecutorError("messaging.message.update: le nouveau texte (message_text) est requis.")
        chat = self._resolve_chat(target)
        msg_id = self._resolve_message_ref(chat["id"], params.get("message"))
        self._call(self._async_edit_message(int(chat["id"]), msg_id, str(new_text)))
        return {"chat_id": chat["id"], "message_id": msg_id, "text": new_text}

    def _delete_message(self, params: dict[str, Any]) -> dict[str, Any]:
        target = params.get("conversation") or params.get("chat_id")
        ref = params.get("message") or params.get("message_id")
        if not target:
            raise ExecutorError("messaging.message.delete: le champ conversation est requis.")
        if ref is None:
            raise ExecutorError("messaging.message.delete: un identifiant de message est requis.")
        chat = self._resolve_chat(target)
        msg_id = self._resolve_message_ref(chat["id"], ref)
        self._call(self._async_delete_messages(int(chat["id"]), [msg_id]))
        return {"chat_id": chat["id"], "deleted_ids": [msg_id]}

    def _delete_conversation(self, params: dict[str, Any]) -> dict[str, Any]:
        target = params.get("conversation") or params.get("chat_id")
        if not target:
            raise ExecutorError("messaging.conversation.delete: le champ conversation est requis.")
        chat = self._resolve_chat(target)
        chat_id = int(chat["id"])
        entity = self._resolve_entity(chat)
        if getattr(entity, "broadcast", None) is not None:
            self._call(self._async_leave_channel(entity))
            action = "left"
        else:
            self._call(self._async_erase_history(entity))
            action = "history_deleted"
        store = self._load_store()
        for bucket in (store.get("chats", {}), store.get("channels", {})):
            bucket.pop(str(chat_id), None)
        self._save_store(store)
        return {"chat_id": str(chat_id), "action": action}

    def _set_mute(self, params: dict[str, Any], muted: bool) -> dict[str, Any]:
        target = params.get("conversation") or params.get("chat_id")
        if not target:
            raise ExecutorError("messaging.conversation.mute: le champ conversation est requis.")
        chat = self._resolve_chat(target)
        until = None
        if muted:
            raw = params.get("snooze_until") or params.get("until")
            until = _parse_dt(raw) if raw else _MUTE_FAR_FUTURE
        self._call(self._async_update_mute(int(chat["id"]), until))
        return {"chat_id": chat["id"], "muted": muted, "until": until.isoformat() if until else None}

    def _manage_participants(self, params: dict[str, Any]) -> dict[str, Any]:
        target = params.get("conversation")
        if not target:
            raise ExecutorError("messaging.conversation.participants: le champ conversation est requis.")
        members = self._as_list(params.get("participants") or params.get("members") or params.get("users"))
        if not members:
            raise ExecutorError(
                "messaging.conversation.participants: précisez les participants à ajouter."
            )
        chat = self._resolve_chat(target)
        user_ids = [int(self._resolve_user(m)["id"]) for m in members]
        self._call(self._async_invite_members(int(chat["id"]), user_ids))
        return {"chat_id": chat["id"], "added": user_ids}

    def _read_conversation(self, params: dict[str, Any]) -> dict[str, Any]:
        target = params.get("conversation")
        if not target:
            raise ExecutorError("messaging.conversation.read: le champ conversation est requis.")
        chat = self._resolve_chat(target)
        try:
            self._call(self._async_mark_read(int(chat["id"])))
            marked_read = True
        except Exception:
            marked_read = False
        data = self.list_messages(chat["id"])
        data["marked_read"] = marked_read
        return data

    # ------------------------------------------------------------------
    # Webapp-facing helpers (shared by /api/telegram/*).
    # ------------------------------------------------------------------
    def list_conversations(self) -> list[dict[str, Any]]:
        store = self._load_store()
        out = list(store.get("chats", {}).values())
        out.sort(key=lambda c: c.get("last_date") or 0, reverse=True)
        return out

    def list_channels(self) -> list[dict[str, Any]]:
        store = self._load_store()
        out = list(store.get("channels", {}).values())
        out.sort(key=lambda c: c.get("last_date") or 0, reverse=True)
        return out

    def list_messages(self, chat_id: str) -> dict[str, Any]:
        if self._client is None and not self._testing:
            return {"chat_id": str(chat_id), "count": 0, "messages": []}
        try:
            raw = self._call(self._async_get_messages(int(chat_id)))
        except (ExecutorError, ValueError):
            return {"chat_id": str(chat_id), "count": 0, "messages": []}
        dialog = self._dialog_for(int(chat_id))
        messages = [self._message_to_dict(m, dialog) for m in (raw or [])]
        messages.sort(key=lambda m: m.get("date") or 0)
        return {
            "chat_id": str(chat_id),
            "count": len(messages),
            "messages": messages,
        }

    def send_chat_message(self, chat_id: str, text: str, peer: Any | None = None) -> dict[str, Any]:
        if not text.strip():
            raise ExecutorError("message vide")
        message = self._call(self._async_send_message(peer if peer is not None else int(chat_id), text))
        result = self._message_to_dict(message, self._dialog_for(int(chat_id)))
        result["chat_id"] = str(chat_id)
        return result

    # ------------------------------------------------------------------
    # Entry point (pipeline).
    # ------------------------------------------------------------------
    def execute(self, ctx: ExecutionContext) -> dict[str, Any]:
        results: list[dict[str, Any]] = []

        # Fallback when attribute extraction returned empty fields: recover a
        # target from the raw user query so conversations can still be started.
        intent_target: str | None = None
        if not any((payload or {}).get("fields") for payload in ctx.actions.values()):
            intent_target = _target_from_query(ctx.intent or "")
        _TARGET_NEEDING_ACTIONS = {
            _SEND_MESSAGE,
            _CREATE_CONVERSATION,
            _READ_CONVERSATION,
            _LIST_MESSAGES,
            _UPDATE_MESSAGE,
            _DELETE_MESSAGE,
            _DELETE_CONVERSATION,
            _MUTE_CONVERSATION,
            _UNMUTE_CONVERSATION,
            _PARTICIPANTS,
        }

        for action, payload in ctx.actions.items():
            params = (payload or {}).get("fields", {}) or {}
            if (
                intent_target
                and action in _TARGET_NEEDING_ACTIONS
                and not any(params.get(k) for k in ("conversation", "contact", "customer", "chat_id", "title"))
            ):
                params["conversation"] = intent_target
            try:
                if action == _SEND_MESSAGE:
                    target = (
                        params.get("conversation")
                        or params.get("contact")
                        or params.get("customer")
                        or params.get("chat_id")
                    )
                    if not target:
                        raise ExecutorError("messaging.message.send: cible (conversation) manquante")
                    chat = self._resolve_chat(target)
                    data = self._send_message(chat, params)
                elif action == _READ_CONVERSATION:
                    data = self._read_conversation(params)
                elif action == _LIST_MESSAGES:
                    target = params.get("conversation") or params.get("chat_id")
                    if not target:
                        raise ExecutorError("messaging.conversation.messages: conversation requise")
                    chat = self._resolve_chat(target)
                    data = self.list_messages(chat["id"])
                elif action == _CREATE_CONVERSATION:
                    data = self._create_conversation(params)
                elif action == _UPDATE_MESSAGE:
                    data = self._update_message(params)
                elif action == _DELETE_MESSAGE:
                    data = self._delete_message(params)
                elif action == _DELETE_CONVERSATION:
                    data = self._delete_conversation(params)
                elif action == _MUTE_CONVERSATION:
                    data = self._set_mute(params, muted=True)
                elif action == _UNMUTE_CONVERSATION:
                    data = self._set_mute(params, muted=False)
                elif action == _PARTICIPANTS:
                    data = self._manage_participants(params)
                elif action == _CREATE_GROUP:
                    data = self._create_group_action(params)
                elif action == _CREATE_CHANNEL:
                    data = self._create_channel_action(params)
                else:
                    results.append(deferred_action(action, params))
                    continue

                results.append(
                    action_result(
                        action,
                        params,
                        tool_calls=[f"telegram:{action}"],
                        data={"ok": True, "response": data},
                    )
                )
            except ExecutorError as exc:
                results.append(
                    action_result(
                        action,
                        params,
                        tool_calls=[f"telegram:{action}"],
                        data={"ok": False, "error": str(exc)},
                    )
                )
            except Exception as exc:  # noqa: BLE001 - raw backend errors must not crash the turn
                results.append(
                    action_result(
                        action,
                        params,
                        tool_calls=[f"telegram:{action}"],
                        data={"ok": False, "error": f"telegram: {exc}"},
                    )
                )

        return executor_result(ctx.domain, results, attempts=ctx.retry_count + 1)