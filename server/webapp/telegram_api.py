"""Viito HTTP API — Telegram messaging backend (personal account).

Thin HTTP wrappers over the TelegramExecutor: the config page drives the
Telethon login flow (api_id/api_hash/phone -> one-time code -> optional 2FA
password), then the Conversations/Channels tabs pull the account's dialogs.

Endpoints
---------
GET  /telegram/status          -> { connected, configured, awaiting_code, me, phone }
POST /telegram/config          -> save { api_id, api_hash, phone }
POST /telegram/send-code       -> request the one-time code (SMS/app)
POST /telegram/login           -> { code, password? } — returns the new status
POST /telegram/logout          -> disconnect + wipe session
POST /telegram/refresh         -> pull get_dialogs, cache, return count
GET  /telegram/conversations   -> cached chats (private/group/supergroup)
GET  /telegram/conversations/{chat_id} -> { chat_id, messages }
POST /telegram/conversations/{chat_id} -> send a message, body { text }
GET  /telegram/channels        -> cached broadcast channels
POST /telegram/channels/{channel_id}   -> publish, body { text }
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server.execution.executors.telegram import TelegramNeedsPasswordError

router = APIRouter(tags=["telegram"])


def _get_telegram_executor():
    from server.execution.executors.registry import get_registry
    from server.execution.executors.telegram import TelegramExecutor

    executor = get_registry().executor_for("messaging")
    if isinstance(executor, TelegramExecutor):
        return executor
    raise HTTPException(status_code=503, detail="Telegram backend not available")


def _connected(telegram):
    status = telegram.get_status()
    if not status.get("connected"):
        raise HTTPException(
            status_code=400,
            detail="Compte Telegram non connecté. Terminez la connexion dans la page Configuration.",
        )
    return status


@router.get("/telegram/status")
def telegram_status():
    try:
        telegram = _get_telegram_executor()
    except HTTPException:
        return {"connected": False, "configured": False, "awaiting_code": False, "me": None, "phone": ""}
    return telegram.get_status()


class TelegramConfigRequest(BaseModel):
    api_id: str
    api_hash: str
    phone: str


@router.post("/telegram/config")
def telegram_save_config(payload: TelegramConfigRequest):
    telegram = _get_telegram_executor()
    if not payload.api_id.strip() or not payload.api_hash.strip() or not payload.phone.strip():
        raise HTTPException(status_code=400, detail="api_id, api_hash et phone sont requis.")
    telegram.save_config(payload.api_id, payload.api_hash, payload.phone)
    return telegram.get_status()


@router.post("/telegram/send-code")
def telegram_send_code():
    telegram = _get_telegram_executor()
    try:
        telegram.send_code()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"sent": True}


class TelegramLoginRequest(BaseModel):
    code: str
    password: str | None = None


@router.post("/telegram/login")
def telegram_login(payload: TelegramLoginRequest):
    telegram = _get_telegram_executor()
    if not payload.code.strip():
        raise HTTPException(status_code=400, detail="code requis")
    try:
        return telegram.login(payload.code.strip(), password=(payload.password or "").strip() or None)
    except TelegramNeedsPasswordError as exc:
        raise HTTPException(
            status_code=400,
            detail={"password_needed": True, "message": str(exc)},
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "Code invalide") from exc


@router.post("/telegram/logout")
def telegram_logout():
    telegram = _get_telegram_executor()
    return telegram.logout()


@router.post("/telegram/refresh")
def telegram_refresh():
    telegram = _get_telegram_executor()
    _connected(telegram)
    return {"new_dialogs": telegram.refresh()}


@router.get("/telegram/conversations")
def telegram_conversations():
    telegram = _get_telegram_executor()
    _connected(telegram)
    return {"conversations": telegram.list_conversations()}


@router.get("/telegram/conversations/{chat_id}")
def telegram_conversation_messages(chat_id: str):
    telegram = _get_telegram_executor()
    _connected(telegram)
    return telegram.list_messages(chat_id)


class TelegramSendRequest(BaseModel):
    text: str


@router.post("/telegram/conversations/{chat_id}")
def telegram_conversation_send(chat_id: str, payload: TelegramSendRequest):
    telegram = _get_telegram_executor()
    _connected(telegram)
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="text requis")
    return telegram.send_chat_message(chat_id, payload.text)


@router.get("/telegram/channels")
def telegram_channels():
    telegram = _get_telegram_executor()
    _connected(telegram)
    return {"channels": telegram.list_channels()}


@router.post("/telegram/channels/{channel_id}")
def telegram_channel_send(channel_id: str, payload: TelegramSendRequest):
    telegram = _get_telegram_executor()
    _connected(telegram)
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="text requis")
    return telegram.send_chat_message(channel_id, payload.text)