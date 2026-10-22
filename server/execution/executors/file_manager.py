"""File management executor — local filesystem backend.

Replaces the Nextcloud executor. Supports all file_management actions
using a local storage directory (memory/files/).
"""

from __future__ import annotations

import base64
from typing import Any

from server.execution.executors.base import (
    DomainExecutor,
    ExecutorError,
    ExecutionContext,
    action_result,
    executor_result,
)
from server.execution.executors.file_storage import FileStorage


class FileExecutor(DomainExecutor):
    domains = ("file_management",)
    backend = "local_files"

    def __init__(self) -> None:
        self._storage = FileStorage()

    def execute(self, ctx: ExecutionContext) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for action, payload in ctx.actions.items():
            params = (payload or {}).get("fields", {})
            try:
                data = self._handle_action(action, params, ctx)
                if data is None:
                    from server.execution.executors.base import deferred_action
                    results.append(deferred_action(action, params, f"action '{action}' not supported"))
                    continue
                results.append(
                    action_result(
                        action,
                        params,
                        tool_calls=[f"files:{action}"],
                        data={"ok": True, "response": data},
                    )
                )
            except ExecutorError as exc:
                results.append(
                    action_result(
                        action,
                        params,
                        tool_calls=[f"files:{action}"],
                        data={"ok": False, "error": str(exc)},
                    )
                )
        return executor_result(ctx.domain, results, attempts=ctx.retry_count + 1)

    def _handle_action(self, action: str, params: dict[str, Any], ctx: ExecutionContext | None = None) -> Any | None:
        if action == "file.put":
            return self._upload(params, ctx)
        if action == "file.get":
            return self._download(params)
        if action == "file.delete":
            return self._delete(params)
        if action == "file.mkcol":
            return self._mkdir(params)
        if action == "file.copy":
            return self._copy(params)
        if action == "file.move":
            return self._move(params)
        if action == "file.propfind":
            return self._list(params)
        if action == "file.trashbin.list":
            return {"trash": self._storage.list_trash()}
        if action == "file.trashbin.restore":
            return self._restore_trash(params)
        if action == "file.trashbin.purge":
            count = self._storage.purge_trash()
            return {"purged": count}
        if action == "file.share.create":
            return self._share_create(params)
        if action == "file.share.delete":
            return self._share_delete(params)
        if action == "file.version.list":
            return self._list_versions(params)
        return None

    def _upload(self, params: dict[str, Any], ctx: ExecutionContext | None) -> dict[str, Any]:
        filename = params.get("file_name") or params.get("name") or "upload"
        path = params.get("path") or ""
        content_b64 = params.get("content") or params.get("content_b64") or ""

        # Check context attachments
        if not content_b64 and ctx and ctx.attachments:
            for att in ctx.attachments:
                if att.get("filename") == filename or not content_b64:
                    content_b64 = att.get("content") or att.get("content_b64", "")
                    if not filename or filename == "upload":
                        filename = att.get("filename", filename)
                    break

        if not content_b64:
            # No bytes in the fields: the document may already have been created
            # by another domain this turn (office.editor.create) or exist in
            # storage. Failing here was fatal: 3 structural retries killed the
            # whole turn and the domains still queued (e.g. the email) never ran.
            existing = self._existing_file(ctx, filename)
            if existing:
                return {
                    "ok": True,
                    "file_name": existing,
                    "path": existing,
                    "already_exists": True,
                    "note": "file already created earlier in this turn; nothing to write",
                }
            raise ExecutorError("file.put: no content provided")

        try:
            raw = base64.b64decode(content_b64)
        except Exception:
            raw = content_b64.encode("utf-8") if isinstance(content_b64, str) else b""

        full_path = f"{path}/{filename}" if path else filename
        return self._storage.save_file(full_path, raw)

    def _existing_file(self, ctx: ExecutionContext | None, filename: str) -> str:
        """Path of `filename` if this turn (or storage) already produced it."""
        target = str(filename or "").strip().casefold()
        if not target:
            return ""
        # 1. a document another domain created earlier in this turn
        for entry in (getattr(ctx, "executor_log", None) or []):
            for record in entry.get("results") or []:
                response = (record.get("data") or {}).get("response")
                if not isinstance(response, dict):
                    continue
                for key in ("file", "file_path", "path"):
                    value = str(response.get(key) or "").strip()
                    if value and value.split("/")[-1].casefold() == target:
                        return value
        # 2. already in storage
        name = target
        for entry in self._storage.list_dir("") if hasattr(self._storage, "list_dir") else []:
            path = str(entry.get("path") or "")
            if entry.get("is_dir") or entry.get("type") == "dir":
                continue
            if path.split("/")[-1].casefold() == name:
                return path
        return ""

    def _download(self, params: dict[str, Any]) -> dict[str, Any]:
        path = params.get("path") or params.get("file_name") or ""
        if not path:
            raise ExecutorError("file.get: path required")
        content = self._storage.read_file(path)
        if content is None:
            raise ExecutorError(f"file.get: '{path}' not found")
        info = self._storage.get_info(path) or {}
        return {
            "path": path,
            "filename": info.get("name", path.rsplit("/", 1)[-1]),
            "size": info.get("size", len(content)),
            "mime": info.get("mime", ""),
            "content_b64": base64.b64encode(content).decode("ascii"),
        }

    def _delete(self, params: dict[str, Any]) -> dict[str, Any]:
        path = params.get("path") or params.get("file_name") or ""
        if not path:
            raise ExecutorError("file.delete: path required")
        ok = self._storage.delete(path, trash=True)
        if not ok:
            raise ExecutorError(f"file.delete: '{path}' not found")
        return {"deleted": path, "trashed": True}

    def _mkdir(self, params: dict[str, Any]) -> dict[str, Any]:
        folder = params.get("folder") or params.get("path") or ""
        if not folder:
            raise ExecutorError("file.mkcol: folder name required")
        return self._storage.mkdir(folder)

    def _copy(self, params: dict[str, Any]) -> dict[str, Any]:
        src = params.get("source_path") or params.get("path") or ""
        dst = params.get("target_path") or params.get("destination") or ""
        if not src or not dst:
            raise ExecutorError("file.copy: source and destination required")
        result = self._storage.copy(src, dst)
        if result is None:
            raise ExecutorError(f"file.copy: source '{src}' not found")
        return result

    def _move(self, params: dict[str, Any]) -> dict[str, Any]:
        src = params.get("source_path") or params.get("path") or ""
        dst = params.get("target_path") or params.get("destination") or ""
        if not src or not dst:
            raise ExecutorError("file.move: source and destination required")
        result = self._storage.move(src, dst)
        if result is None:
            raise ExecutorError(f"file.move: source '{src}' not found")
        return result

    def _list(self, params: dict[str, Any]) -> dict[str, Any]:
        path = params.get("path") or ""
        return {"items": self._storage.list_dir(path), "path": path}

    def _restore_trash(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("file_name") or params.get("path") or ""
        if not name:
            raise ExecutorError("file.trashbin.restore: file_name required")
        ok = self._storage.restore_from_trash(name)
        if not ok:
            raise ExecutorError(f"file.trashbin.restore: '{name}' not found in trash")
        return {"restored": name}

    def _share_create(self, params: dict[str, Any]) -> dict[str, Any]:
        path = params.get("path") or ""
        recipient = params.get("recipient") or params.get("recipient_email") or ""
        if not path or not recipient:
            raise ExecutorError("file.share.create: path and recipient required")
        return self._storage.create_share(path, recipient)

    def _share_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        share_id = params.get("share_id") or ""
        if not share_id:
            raise ExecutorError("file.share.delete: share_id required")
        ok = self._storage.delete_share(share_id)
        if not ok:
            raise ExecutorError(f"file.share.delete: share '{share_id}' not found")
        return {"deleted": share_id}

    def _list_versions(self, params: dict[str, Any]) -> dict[str, Any]:
        path = params.get("path") or ""
        return {"versions": [], "note": "versioning not implemented for local storage"}
