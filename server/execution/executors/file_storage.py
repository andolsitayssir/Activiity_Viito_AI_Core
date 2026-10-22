"""Local file storage — replaces Nextcloud for file_management domain.

Stores files in ``memory/files/`` with a simple JSON metadata index.
Supports: upload, download, list, mkdir, delete, move, copy, rename, trash.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any


_BASE = Path(__file__).resolve().parents[3] / "memory" / "files"
_TRASH = Path(__file__).resolve().parents[3] / "memory" / "trash"
_INDEX = Path(__file__).resolve().parents[3] / "memory" / "files_index.json"


def _ensure_dirs() -> None:
    _BASE.mkdir(parents=True, exist_ok=True)
    _TRASH.mkdir(parents=True, exist_ok=True)


def _load_index() -> dict[str, Any]:
    if _INDEX.exists():
        return json.loads(_INDEX.read_text(encoding="utf-8"))
    return {"files": {}, "shares": {}}


def _save_index(idx: dict[str, Any]) -> None:
    _INDEX.write_text(json.dumps(idx, indent=2, ensure_ascii=False), encoding="utf-8")


def _rel(path: str) -> str:
    return path.strip("/").strip("\\")


def _full(relpath: str) -> Path:
    return _BASE / _rel(relpath)


class FileStorage:
    """Thin wrapper over local filesystem + JSON index."""

    def __init__(self) -> None:
        _ensure_dirs()

    # ── listing ──────────────────────────────────────────────────────
    def list_dir(self, path: str = "") -> list[dict[str, Any]]:
        """List contents of a directory."""
        target = _full(path)
        if not target.exists():
            return []
        items = []
        for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            rel = str(entry.relative_to(_BASE)).replace("\\", "/")
            stat = entry.stat()
            is_dir = entry.is_dir()
            items.append({
                "id": rel if not is_dir else f"dir:{rel}",
                "name": entry.name,
                "path": rel,
                "type": "folder" if is_dir else "file",
                "size": len(list(entry.iterdir())) if is_dir else stat.st_size,
                "modified": stat.st_mtime,
                "mime": self._guess_mime(entry.name) if not is_dir else "",
            })
        return items

    def get_info(self, path: str) -> dict[str, Any] | None:
        target = _full(path)
        if not target.exists():
            return None
        rel = str(target.relative_to(_BASE)).replace("\\", "/")
        stat = target.stat()
        is_dir = target.is_dir()
        return {
            "id": rel if not is_dir else f"dir:{rel}",
            "name": target.name,
            "path": rel,
            "type": "folder" if is_dir else "file",
            "size": len(list(target.iterdir())) if is_dir else stat.st_size,
            "modified": stat.st_mtime,
            "mime": self._guess_mime(target.name) if not is_dir else "",
        }

    # ── upload ───────────────────────────────────────────────────────
    def save_file(self, relpath: str, content: bytes) -> dict[str, Any]:
        """Save uploaded content to a path, creating parent dirs."""
        target = _full(relpath)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        rel = str(target.relative_to(_BASE)).replace("\\", "/")
        stat = target.stat()
        return {
            "id": rel,
            "name": target.name,
            "path": rel,
            "type": "file",
            "size": stat.st_size,
            "modified": stat.st_mtime,
            "mime": self._guess_mime(target.name),
        }

    # ── download ─────────────────────────────────────────────────────
    def read_file(self, path: str) -> bytes | None:
        target = _full(path)
        if not target.exists() or not target.is_file():
            return None
        return target.read_bytes()

    # ── mkdir ────────────────────────────────────────────────────────
    def mkdir(self, path: str) -> dict[str, Any]:
        target = _full(path)
        target.mkdir(parents=True, exist_ok=True)
        rel = str(target.relative_to(_BASE)).replace("\\", "/")
        return {"id": f"dir:{rel}", "name": target.name, "path": rel, "type": "folder"}

    # ── delete ───────────────────────────────────────────────────────
    def delete(self, path: str, trash: bool = True) -> bool:
        target = _full(path)
        if not target.exists():
            return False
        if trash:
            trash_id = f"{int(time.time())}_{uuid.uuid4().hex[:8]}"
            trash_dest = _TRASH / trash_id
            shutil.move(str(target), str(trash_dest))
        else:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        return True

    # ── move ─────────────────────────────────────────────────────────
    def resolve(self, relpath: str) -> str | None:
        """Resolve a possibly-partial path (e.g. just a filename) to a real
        relative path under the storage root. Returns the resolved rel path
        or None if nothing matches. Tries exact path first, then a filename
        search across the whole tree."""
        relpath = _rel(relpath)
        if not relpath:
            return None
        exact = _full(relpath)
        if exact.exists():
            return str(exact.relative_to(_BASE)).replace("\\", "/")
        base_name = relpath.split("/")[-1]
        best: Path | None = None
        for candidate in _BASE.rglob(base_name):
            if candidate.name == base_name:
                if best is None or len(candidate.parts) < len(best.parts):
                    best = candidate
        if best is not None:
            return str(best.relative_to(_BASE)).replace("\\", "/")
        return None

    def move(self, src: str, dst: str) -> dict[str, Any] | None:
        resolved_src = self.resolve(src)
        if resolved_src is None:
            return None
        src_p = _full(resolved_src)
        dst_p = _full(dst)
        # If destination is a directory, append the source filename
        if dst_p.exists() and dst_p.is_dir():
            dst_p = dst_p / src_p.name
            dst = f"{dst.rstrip('/')}/{src_p.name}"
        elif not dst_p.exists() and dst.endswith('/'):
            # Destination ends with /, treat as folder
            dst_p.parent.mkdir(parents=True, exist_ok=True)
            dst_p = dst_p / src_p.name
            dst = f"{dst.rstrip('/')}/{src_p.name}"
        dst_p.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_p), str(dst_p))
        return self.get_info(dst)

    # ── copy ─────────────────────────────────────────────────────────
    def copy(self, src: str, dst: str) -> dict[str, Any] | None:
        resolved_src = self.resolve(src)
        if resolved_src is None:
            return None
        src_p = _full(resolved_src)
        dst_p = _full(dst)
        # If destination is a directory, append the source filename
        if dst_p.exists() and dst_p.is_dir():
            dst_p = dst_p / src_p.name
            dst = f"{dst.rstrip('/')}/{src_p.name}"
        elif not dst_p.exists() and dst.endswith('/'):
            dst_p.parent.mkdir(parents=True, exist_ok=True)
            dst_p = dst_p / src_p.name
            dst = f"{dst.rstrip('/')}/{src_p.name}"
        dst_p.parent.mkdir(parents=True, exist_ok=True)
        if src_p.is_dir():
            shutil.copytree(str(src_p), str(dst_p))
        else:
            shutil.copy2(str(src_p), str(dst_p))
        return self.get_info(dst)

    # ── rename ───────────────────────────────────────────────────────
    def rename(self, path: str, new_name: str) -> dict[str, Any] | None:
        target = _full(path)
        if not target.exists():
            return None
        new_target = target.parent / new_name
        target.rename(new_target)
        return self.get_info(str(new_target.relative_to(_BASE)).replace("\\", "/"))

    # ── trash ────────────────────────────────────────────────────────
    def list_trash(self) -> list[dict[str, Any]]:
        items = []
        for entry in sorted(_TRASH.iterdir(), key=lambda e: e.name, reverse=True):
            stat = entry.stat()
            items.append({
                "name": entry.name,
                "path": entry.name,
                "type": "folder" if entry.is_dir() else "file",
                "size": 0 if entry.is_dir() else stat.st_size,
                "modified": stat.st_mtime,
                "original_name": entry.name.split("_", 1)[-1] if "_" in entry.name else entry.name,
            })
        return items

    def restore_from_trash(self, trash_name: str) -> bool:
        src = _TRASH / trash_name
        if not src.exists():
            return False
        original_name = trash_name.split("_", 1)[-1] if "_" in trash_name else trash_name
        dest = _BASE / original_name
        shutil.move(str(src), str(dest))
        return True

    def purge_trash(self) -> int:
        count = 0
        for entry in _TRASH.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            count += 1
        return count

    # ── shares (simple) ──────────────────────────────────────────────
    def create_share(self, path: str, recipient: str, permission: str = "read") -> dict[str, Any]:
        idx = _load_index()
        share_id = uuid.uuid4().hex[:12]
        idx["shares"][share_id] = {
            "path": path,
            "recipient": recipient,
            "permission": permission,
            "created": time.time(),
        }
        _save_index(idx)
        return {"share_id": share_id, "path": path, "recipient": recipient, "permission": permission}

    def list_shares(self) -> list[dict[str, Any]]:
        idx = _load_index()
        return [{"share_id": k, **v} for k, v in idx.get("shares", {}).items()]

    def delete_share(self, share_id: str) -> bool:
        idx = _load_index()
        if share_id in idx.get("shares", {}):
            del idx["shares"][share_id]
            _save_index(idx)
            return True
        return False

    # ── helpers ──────────────────────────────────────────────────────
    @staticmethod
    def _guess_mime(name: str) -> str:
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        mime_map = {
            "pdf": "application/pdf",
            "doc": "application/msword",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "xls": "application/vnd.ms-excel",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "ppt": "application/vnd.ms-powerpoint",
            "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "txt": "text/plain",
            "csv": "text/csv",
            "json": "application/json",
            "xml": "application/xml",
            "html": "text/html",
            "css": "text/css",
            "js": "application/javascript",
            "py": "text/x-python",
            "zip": "application/zip",
            "gz": "application/gzip",
            "tar": "application/x-tar",
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "gif": "image/gif",
            "svg": "image/svg+xml",
            "webp": "image/webp",
            "mp3": "audio/mpeg",
            "mp4": "video/mp4",
            "avi": "video/x-msvideo",
            "mov": "video/quicktime",
        }
        return mime_map.get(ext, "application/octet-stream")
