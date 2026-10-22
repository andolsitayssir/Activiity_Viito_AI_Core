"""Tasking domain executor backed by the Vikunja REST API (localhost:3456).

Implements the ``tasking.*`` action set from ``tools/attr_defs/tasking.json``
against Vikunja's /api/v1/... endpoints. Auth is a JWT obtained from
``/api/v1/login`` using VIKUNJA_USERNAME/VIKUNJA_PASSWORD from the environment
(never hardcoded, never read from the secrets file at call time).

Vikunja 2.x inverted the classic verb scheme: PUT on a *collection* creates
(``PUT /projects``, ``PUT /projects/{id}/tasks``, ``PUT /labels``...), POST on a
singular item updates (``POST /tasks/{id}``, ``POST /projects/{id}``), and the
bulk-relabel / bulk-assign endpoints replace the full set. All verbs used here
were live-verified against the running container.

Live-verified actions create/update/read/delete real objects; the rest (project
shares, attachment download bytes, webhooks...) are deferred.
"""

from __future__ import annotations

import io
import os
import re
import threading
import time
from datetime import date
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

# Actions implemented against the live Vikunja instance.
_TASK_CREATE = "tasking.task.create"
_TASK_UPDATE = "tasking.task.update"
_TASK_PARTIAL_UPDATE = "tasking.task.partial_update"
_TASK_DELETE = "tasking.task.delete"
_TASK_DUPLICATE = "tasking.task.duplicate"
_TASK_READ = "tasking.task.read"
_PROJECT_CREATE = "tasking.project.create"
_PROJECT_UPDATE = "tasking.project.update"
_PROJECT_READ = "tasking.project.read"
_PROJECT_DELETE = "tasking.project.delete"
_PROJECT_DUPLICATE = "tasking.project.duplicate"
_BUCKET_CREATE = "tasking.bucket.create"
_BUCKET_UPDATE = "tasking.bucket.update"
_BUCKET_DELETE = "tasking.bucket.delete"
_BUCKET_MOVE_TASK = "tasking.bucket.move_task"
_VIEW_CREATE = "tasking.view.create"
_VIEW_UPDATE = "tasking.view.update"
_VIEW_DELETE = "tasking.view.delete"
_ASSIGNEE_ADD = "tasking.assignee.add"
_ASSIGNEE_REMOVE = "tasking.assignee.remove"
_ASSIGNEE_BULK = "tasking.assignee.bulk"
_LABEL_ADD = "tasking.label.add"
_LABEL_REMOVE = "tasking.label.remove"
_LABEL_BULK = "tasking.label.bulk"
_COMMENT_CREATE = "tasking.comment.create"
_COMMENT_UPDATE = "tasking.comment.update"
_COMMENT_DELETE = "tasking.comment.delete"
_RELATION_CREATE = "tasking.relation.create"
_RELATION_DELETE = "tasking.relation.delete"
_ATTACH_UPLOAD = "tasking.attachment.upload"
_ATTACH_DOWNLOAD = "tasking.attachment.download"
_ATTACH_DELETE = "tasking.attachment.delete"
_SHARE_LINK = "tasking.share.link"
_SHARE_PROJECT = "tasking.share.project"
_NOTIF_LIST = "tasking.notification.list"
_NOTIF_READ = "tasking.notification.mark_read"

_PER_PAGE = 200


def _as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "done", "complete", "completed", "closed"):
        return True
    if text in ("0", "false", "no", "open", "todo", "pending", "in_progress", "in-progress", ""):
        return False
    return None


def _as_int(value: Any, default: Any = None) -> Any:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_FRENCH_MONTHS: dict[str, int] = {
    "janvier": 1, "jan": 1,
    "fevrier": 2, "février": 2, "fev": 2, "feb": 2,
    "mars": 3, "mar": 3,
    "avril": 4, "avr": 4,
    "mai": 5,
    "juin": 6, "juillet": 7, "juil": 7,
    "aout": 8, "août": 8,
    "septembre": 9, "sept": 9,
    "octobre": 10, "oct": 10,
    "novembre": 11, "nov": 11,
    "decembre": 12, "décembre": 12, "dec": 12,
}


def _parse_month(value: Any) -> int | None:
    """Map a month part (int 1-12 or a (French) month name) to 1-12."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        m = int(value)
        return m if 1 <= m <= 12 else None
    text = str(value).strip().lower()
    if not text:
        return None
    if text.isdigit():
        m = int(text)
        return m if 1 <= m <= 12 else None
    normalized = (
        text.replace("é", "e").replace("è", "e").replace("ê", "e")
        .replace("à", "a").replace("â", "a")
        .replace("ô", "o").replace("ö", "o")
        .replace("î", "i").replace("ï", "i")
        .replace("û", "u").replace("ü", "u")
        .replace("ç", "c")
    )
    return _FRENCH_MONTHS.get(normalized)


def _parse_french_date(value: Any) -> str | None:
    """Best-effort parse of a spoken/free-form date ('25 octobre 2026',
    '25/10/2026', '2026-10-25') into ISO 'YYYY-MM-DD'."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    iso = re.fullmatch(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*", text)
    if iso:
        return f"{int(iso.group(1)):04d}-{int(iso.group(2)):02d}-{int(iso.group(3)):02d}"
    dmy = re.fullmatch(r"\s*(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})\s*", text)
    if dmy:
        day, month, year = int(dmy.group(1)), int(dmy.group(2)), int(dmy.group(3))
        if year < 100:
            year += 2000
        return f"{year:04d}-{month:02d}-{day:02d}"
    dm = re.fullmatch(r"\s*(\d{1,2})\s+([a-zà-ÿ]+)(?:\s+(\d{2,4}))?\s*", text.lower())
    if dm:
        month = _parse_month(dm.group(2))
        if month:
            return f"{int(dm.group(3) or date.today().year):04d}-{month:02d}-{int(dm.group(1)):02d}"
    return None


def _build_due_date(params: dict[str, Any]) -> str | None:
    due = params.get("due_date")
    if due is not None:
        parsed = _parse_french_date(due)
        if parsed:
            hour = _as_int(params.get("due_hour"), 0)
            minute = _as_int(params.get("due_minute"), 0)
            return f"{parsed}T{hour:02d}:{minute:02d}:00Z"
        return str(due)
    day = _as_int(params.get("due_day"))
    month = _parse_month(params.get("due_month"))
    year = _as_int(params.get("due_year"))
    if day is None and month is None and year is None:
        return None
    today = date.today()
    try:
        when = date(
            year if year is not None else today.year,
            month if month is not None else today.month,
            day if day is not None else today.day,
        )
    except ValueError as exc:
        raise ExecutorError(f"invalid due date ({exc})") from exc
    hour = _as_int(params.get("due_hour"), 0)
    minute = _as_int(params.get("due_minute"), 0)
    return f"{when.isoformat()}T{hour:02d}:{minute:02d}:00Z"


class VikunjaExecutor(DomainExecutor):
    domains: tuple[str, ...] = ("tasking",)
    backend = "vikunja"

    def __init__(self) -> None:
        self.base_url = os.environ.get("VIKUNJA_URL", "http://localhost:3456").rstrip("/")
        self.username = os.environ.get("VIKUNJA_USERNAME", "")
        self.password = os.environ.get("VIKUNJA_PASSWORD", "")
        self._http = HttpClient(self.base_url)
        self._token: str | None = None
        self._auth_lock = threading.Lock()
        #: (kind, lowercase title) -> id for entities created within the current
        #: execute() turn, so a later action referencing them by name resolves
        #: to the just-created record instead of an older duplicate.
        self._turn_created: dict[tuple[str, str], str] = {}

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    def _login(self, force: bool = False) -> str:
        with self._auth_lock:
            if not force and self._token:
                return self._token
            if not self.username or not self.password:
                raise ExecutorError(
                    "VikunjaExecutor: VIKUNJA_USERNAME/VIKUNJA_PASSWORD unset"
                )
            # /login is a no-auth route rate-limited per IP (default 10/60s).
            # Back off briefly when that shared bucket is exhausted.
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    data = self._http.post(
                        "/api/v1/login",
                        json={"username": self.username, "password": self.password},
                    )
                except ExecutorError as exc:
                    if "429" not in str(exc):
                        raise
                    last_error = exc
                    if attempt < 2:
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    raise ExecutorError(
                        f"Vikunja login rate-limited (429): {exc}"
                    ) from exc
                token = (data or {}).get("token")
                if not token:
                    raise ExecutorError("Vikunja login returned no token")
                self._token = str(token)
                return self._token
            raise ExecutorError(f"Vikunja login rate-limited: {last_error}")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._login()}", "Content-Type": "application/json"}

    # ------------------------------------------------------------------
    # HTTP helpers (with one retry on fresh authentication)
    # ------------------------------------------------------------------
    def _request(self, method: str, path: str, **kw: Any) -> Any:
        try:
            return self._http.request(method, path, headers=self._headers(), **kw)
        except ExecutorError as exc:
            if "401" in str(exc) or "403" in str(exc):
                fresh = {"Authorization": f"Bearer {self._login(force=True)}", "Content-Type": "application/json"}
                return self._http.request(method, path, headers=fresh, **kw)
            raise

    def _get(self, path: str, **kw: Any) -> Any:
        return self._request("GET", path, **kw)

    def _put(self, path: str, **kw: Any) -> Any:
        return self._request("PUT", path, **kw)

    def _post(self, path: str, **kw: Any) -> Any:
        return self._request("POST", path, **kw)

    def _delete(self, path: str, **kw: Any) -> Any:
        return self._request("DELETE", path, **kw)

    # ------------------------------------------------------------------
    # Resolvers (id or fuzzy name -> backend id)
    # ------------------------------------------------------------------
    def _match(self, items: list[dict[str, Any]], value: Any, name_fields: tuple[str, ...] = ("title", "name")) -> str | None:
        text = str(value or "").strip()
        for item in items:
            idv = item.get("id")
            if idv is None:
                continue
            if isinstance(value, (int, float)) or str(value).strip().isdigit():
                if str(idv) == str(value).strip():
                    return str(idv)
            if not text:
                continue
            name = " ".join(str(item.get(k) or "") for k in name_fields)
            if name and (text.lower() in name.lower() or name.lower() in text.lower()):
                if isinstance(value, (int, float)) or str(value).strip().isdigit():
                    continue
                return str(idv)
        return None

    def _resolve_project(self, value: Any) -> str | None:
        if str(value).strip().isdigit():
            return str(value).strip()
        if not value:
            return None
        text = str(value).strip().lower()
        same_turn = self._turn_created.get(("project", text))
        if same_turn:
            return same_turn
        try:
            projects = self._get(f"/api/v1/projects?per_page={_PER_PAGE}")
        except ExecutorError:
            return None
        return self._match(projects if isinstance(projects, list) else [], value)

    def _sole_turn_project(self) -> str | None:
        """Id of the single project created earlier in this same turn, or None.

        Backstop for the router not repeating the project title onto dependent
        actions (task.create/bucket.create/view.create) when the request also
        created that project: 'crée le projet X et ajoute la tâche Y' must link
        Y inside X, not drop it into the Inbox.
        """
        ids = {pid for (kind, _), pid in self._turn_created.items() if kind == "project"}
        return next(iter(ids)) if len(ids) == 1 else None

    def _project_target(self, params: dict[str, Any]) -> str | None:
        """Project id for ``params``: explicit reference first, else the single
        project created within the current turn."""
        if params.get("project"):
            pid = self._resolve_project(params["project"])
            if pid:
                return pid
        return self._sole_turn_project()

    def _resolve_task(self, value: Any) -> str | None:
        if str(value).strip().isdigit():
            return str(value).strip()
        if not value:
            return None
        text = str(value).strip().lower()
        same_turn = self._turn_created.get(("task", text))
        if same_turn:
            return same_turn
        try:
            tasks = self._get(
                f"/api/v1/tasks?per_page={_PER_PAGE}&s={value}"
            )
        except ExecutorError:
            return None
        return self._match(tasks if isinstance(tasks, list) else [], value, ("title", "name"))

    def _resolve_label(self, value: Any) -> str | None:
        if str(value).strip().isdigit():
            return str(value).strip()
        if not value:
            return None
        try:
            labels = self._get(f"/api/v1/labels?per_page={_PER_PAGE}")
        except ExecutorError:
            return None
        return self._match(labels if isinstance(labels, list) else [], value)

    def _resolve_user(self, value: Any) -> str | None:
        text = str(value).strip()
        if text.isdigit():
            return text
        if text == self.username:
            raise ExecutorError(
                "assignee username resolves to the authenticated user; pass its numeric id instead"
            )
        raise ExecutorError(f"assignee must be a numeric Vikunja user id (got '{text}')")

    def _resolve_view(self, project_id: str, value: Any = None) -> str | None:
        """Lowest-position view, preferring one matching ``value`` or a kanban."""
        try:
            views = self._get(f"/api/v1/projects/{project_id}/views?per_page={_PER_PAGE}")
        except ExecutorError:
            return None
        views = views if isinstance(views, list) else []
        if value:
            found = self._match(views, value, ("title", "view_kind"))
            if found:
                return found
        for pref in ("kanban", "list", "gantt", "table"):
            for view in views:
                if (view.get("view_kind") or "").lower() == pref:
                    return str(view.get("id"))
        return str(views[0]["id"]) if views else None

    def _resolve_bucket(self, project_id: str, view_id: str, value: Any) -> str | None:
        if str(value).strip().isdigit():
            return str(value).strip()
        if not value:
            return None
        try:
            buckets = self._get(
                f"/api/v1/projects/{project_id}/views/{view_id}/buckets?per_page={_PER_PAGE}"
            )
        except ExecutorError:
            return None
        return self._match(buckets if isinstance(buckets, list) else [], value)

    def _resolve_comment(self, task_id: str, value: Any) -> str | None:
        if str(value).strip().isdigit():
            return str(value).strip()
        if not value:
            return None
        try:
            comments = self._get(f"/api/v1/tasks/{task_id}/comments?per_page={_PER_PAGE}")
        except ExecutorError:
            return None
        return self._match(comments if isinstance(comments, list) else [], value, ("comment",))

    def _get_bucket_view(self, project_id: str, bucket_id: str) -> str | None:
        """Bucket objects carry the project_view_id that owns them."""
        try:
            views = self._get(f"/api/v1/projects/{project_id}/views?per_page={_PER_PAGE}")
            views = views if isinstance(views, list) else []
            for view in views:
                buckets = self._get(
                    f"/api/v1/projects/{project_id}/views/{view['id']}/buckets?per_page={_PER_PAGE}"
                )
                for bucket in buckets if isinstance(buckets, list) else []:
                    if str(bucket.get("id")) == str(bucket_id):
                        return str(view["id"])
        except (ExecutorError, KeyError, TypeError):
            return None
        return None

    # ------------------------------------------------------------------
    # Payload builders
    # ------------------------------------------------------------------
    def _task_payload(self, params: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if params.get("task") is not None:
            payload["title"] = str(params["task"])
        if params.get("description") is not None:
            payload["description"] = str(params["description"])
        done = _as_bool(params.get("done"))
        status = _as_bool(params.get("status"))
        if done is not None:
            payload["done"] = done
        elif status is not None:
            payload["done"] = status
        priority = _as_int(params.get("priority"))
        if priority is not None:
            payload["priority"] = priority
        due = _build_due_date(params)
        if due:
            payload["due_date"] = due
        if params.get("project") is not None:
            pid = self._resolve_project(params["project"])
            if pid:
                payload["project_id"] = _as_int(pid)
        return payload

    def _label_bodies(self, labels: Any) -> list[int]:
        if labels is None:
            return []
        if isinstance(labels, list):
            return [_as_int(x) for x in labels if _as_int(x) is not None]
        return [_as_int(labels)] if _as_int(labels) is not None else []

    def _assignee_bodies(self, assignees: Any) -> list[dict[str, Any]]:
        if isinstance(assignees, list):
            return [{"id": _as_int(x)} for x in assignees if _as_int(x) is not None]
        if assignees is not None and _as_int(assignees) is not None:
            return [{"id": _as_int(assignees)}]
        return []

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _create_task(self, params: dict[str, Any]) -> dict[str, Any]:
        pid = self._project_target(params) or self._resolve_project("Inbox")
        if not pid:
            raise ExecutorError("tasking.task.create: project not found")
        task = self._task_payload(params)
        if not task.get("title"):
            raise ExecutorError("tasking.task.create: missing 'task' (title)")
        data = self._put(f"/api/v1/projects/{pid}/tasks", json=task)
        tid = str((data or {}).get("id"))
        if not tid:
            raise ExecutorError("tasking.task.create: response missing task id")
        self._turn_created[("task", str(task.get("title") or "").strip().lower())] = tid
        self._apply_traits(tid, params)
        return {"id": tid, "project_id": pid, "response": data}

    def _update_task(self, tid: str, params: dict[str, Any]) -> dict[str, Any]:
        current = self._get(f"/api/v1/tasks/{tid}")
        merged: dict[str, Any] = {}
        keys = ("title", "done", "priority", "due_date")
        payload = self._task_payload(params)
        for key in keys:
            if key in payload:
                merged[key] = payload[key]
        if params.get("description") is not None:
            merged["description"] = str(params["description"])
        elif current.get("description") not in (None, ""):
            merged["description"] = current.get("description")
        if "title" not in merged and current.get("title"):
            merged["title"] = current.get("title")
        if "due_date" in merged and (merged["due_date"].startswith("0001") or merged["due_date"] in ("", None)):
            merged.pop("due_date", None)
        data = self._post(f"/api/v1/tasks/{tid}", json=merged)
        self._apply_traits(tid, params)
        return {"id": tid, "response": data}

    def _apply_traits(self, tid: str, params: dict[str, Any]) -> None:
        """Attach labels/assignees/attachments after create/update."""
        for label in self._label_bodies(params.get("labels")):
            self._put(f"/api/v1/tasks/{tid}/labels", json={"label_id": label})
        for assignee in self._assignee_bodies(params.get("assignees")):
            self._put(f"/api/v1/tasks/{tid}/assignees", json={"user_id": assignee["id"]})
        for attachment in self._attachment_files(params.get("attachements")):
            self._upload_attachment(tid, attachment)

    @staticmethod
    def _attachment_files(value: Any) -> list[tuple[str, bytes]]:
        files: list[tuple[str, bytes]] = []
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    name = str(item.get("name") or "attachment.bin")
                    content = (item.get("content") or b"").encode() if isinstance(item.get("content"), str) else item.get("content") or b""
                    files.append((name, content))
                elif isinstance(item, (bytes, bytearray)):
                    files.append(("attachment.bin", bytes(item)))
        return files

    def _upload_attachment(self, tid: str, item: tuple[str, bytes]) -> str | None:
        name, content = item
        data = {"files": (name, io.BytesIO(content), "application/octet-stream")}
        self._http.request(
            "PUT",
            f"/api/v1/tasks/{tid}/attachments",
            headers={"Authorization": f"Bearer {self._login()}"},
            data=data,
        )
        return name

    def _create_project(self, params: dict[str, Any]) -> dict[str, Any]:
        title = params.get("title")
        if not title:
            raise ExecutorError("tasking.project.create: missing 'title'")
        payload: dict[str, Any] = {"title": str(title)}
        if params.get("description") is not None:
            payload["description"] = str(params["description"])
        data = self._put("/api/v1/projects", json=payload)
        if not (data or {}).get("id"):
            raise ExecutorError("tasking.project.create: response missing project id")
        self._turn_created[("project", str(payload["title"]).strip().lower())] = str(data["id"])
        return {"id": str(data["id"]), "response": data}

    def _update_project(self, pid: str, params: dict[str, Any]) -> dict[str, Any]:
        current = self._get(f"/api/v1/projects/{pid}")
        payload = {"title": current.get("title") or str(params.get("project") or ""), "description": params.get("description")}
        if params.get("description") is None and current.get("description"):
            payload["description"] = current.get("description")
        data = self._post(f"/api/v1/projects/{pid}", json=payload)
        return {"id": pid, "response": data}

    def _create_bucket(self, params: dict[str, Any]) -> dict[str, Any]:
        pid = self._project_target(params) or self._resolve_project("Inbox")
        if not pid:
            raise ExecutorError("tasking.bucket.create: project not found")
        if not params.get("title"):
            raise ExecutorError("tasking.bucket.create: missing 'title'")
        vid = self._resolve_view(pid, params.get("view"))
        if not vid:
            raise ExecutorError("tasking.bucket.create: no view available")
        data = self._put(
            f"/api/v1/projects/{pid}/views/{vid}/buckets",
            json={"title": str(params["title"])},
        )
        if not (data or {}).get("id"):
            raise ExecutorError("tasking.bucket.create: response missing bucket id")
        return {"id": str(data["id"]), "view_id": vid, "response": data}

    def _update_bucket(self, params: dict[str, Any]) -> dict[str, Any]:
        pid = self._resolve_project(params.get("project") or "Inbox")
        if not pid:
            raise ExecutorError("tasking.bucket.update: project not found")
        if not params.get("bucket"):
            raise ExecutorError("tasking.bucket.update: missing 'bucket'")
        bid = self._resolve_bucket(pid, self._resolve_view(pid) or "", params["bucket"])
        if not bid:
            raise ExecutorError("tasking.bucket.update: bucket not found")
        vid = self._get_bucket_view(pid, bid)
        vid = vid or self._resolve_view(pid) or ""
        data = self._post(
            f"/api/v1/projects/{pid}/views/{vid}/buckets/{bid}",
            json={"title": str(params.get("title") or params["bucket"])},
        )
        return {"id": bid, "response": data}

    def _delete_bucket(self, params: dict[str, Any]) -> dict[str, Any]:
        pid = self._resolve_project(params.get("project") or "Inbox")
        if not pid or not params.get("bucket"):
            raise ExecutorError("tasking.bucket.delete: project/bucket required")
        bid = self._resolve_bucket(pid, self._resolve_view(pid) or "", params["bucket"])
        if not bid:
            raise ExecutorError("tasking.bucket.delete: bucket not found")
        vid = self._get_bucket_view(pid, bid) or self._resolve_view(pid) or ""
        self._delete(f"/api/v1/projects/{pid}/views/{vid}/buckets/{bid}")
        return {"id": bid}

    def _move_task_to_bucket(self, params: dict[str, Any]) -> dict[str, Any]:
        pid = self._resolve_project(params.get("project") or "Inbox")
        tid = self._resolve_task(params.get("task"))
        if not pid or not tid or not params.get("bucket"):
            raise ExecutorError("tasking.bucket.move_task: project/task/bucket required")
        if not params.get("bucket"):
            raise ExecutorError("tasking.bucket.move_task: missing 'bucket'")
        bid = self._resolve_bucket(pid, self._resolve_view(pid) or "", params["bucket"])
        if not bid:
            raise ExecutorError("tasking.bucket.move_task: bucket not found")
        vid = self._get_bucket_view(pid, bid) or self._resolve_view(pid) or ""
        data = self._post(
            f"/api/v1/projects/{pid}/views/{vid}/buckets/{bid}/tasks",
            json={"task_id": _as_int(tid), "position": _as_int(params.get("position"), 0)},
        )
        return {"task_id": tid, "bucket_id": bid, "response": data}

    def _create_view(self, params: dict[str, Any]) -> dict[str, Any]:
        pid = self._project_target(params) or self._resolve_project("Inbox")
        if not pid:
            raise ExecutorError("tasking.view.create: project not found")
        if not params.get("view_name"):
            raise ExecutorError("tasking.view.create: missing 'view_name'")
        payload = {"title": str(params["view_name"])}
        kb = {"kanban": "kanban", "gantt": "gantt", "list": "list", "table": "table", "timeline": "gantt"}
        kind = (str(params.get("view_kind") or params.get("type") or "list")).strip().lower()
        payload["view_kind"] = kb.get(kind, "list")
        data = self._put(f"/api/v1/projects/{pid}/views", json=payload)
        if not (data or {}).get("id"):
            raise ExecutorError("tasking.view.create: response missing view id")
        return {"id": str(data["id"]), "project_id": pid, "response": data}

    def _update_view(self, params: dict[str, Any]) -> dict[str, Any]:
        pid = self._resolve_project(params.get("project") or "Inbox")
        if not pid:
            raise ExecutorError("tasking.view.update: project not found")
        vid = self._resolve_view(pid, params.get("view")) if params.get("view") else self._resolve_view(pid)
        if not vid:
            raise ExecutorError("tasking.view.update: view not found")
        data = self._post(f"/api/v1/projects/{pid}/views/{vid}", json={"title": str(params.get("view_name") or params.get("title") or "View")})
        return {"id": vid, "project_id": pid, "response": data}

    def _delete_view(self, params: dict[str, Any]) -> dict[str, Any]:
        pid = self._resolve_project(params.get("project") or "Inbox")
        if not pid or not params.get("view"):
            raise ExecutorError("tasking.view.delete: project/view required")
        vid = self._resolve_view(pid, params["view"]) if params.get("view") else None
        if not vid:
            raise ExecutorError("tasking.view.delete: view not found")
        self._delete(f"/api/v1/projects/{pid}/views/{vid}")
        return {"id": vid, "project_id": pid}

    def _add_assignee(self, params: dict[str, Any]) -> dict[str, Any]:
        tid = self._resolve_task(params.get("task"))
        if not tid:
            raise ExecutorError("tasking.assignee.add: task not found")
        uid = self._resolve_user(params.get("assignee"))
        if not uid:
            raise ExecutorError("tasking.assignee.add: could not resolve assignee")
        data = self._put(f"/api/v1/tasks/{tid}/assignees", json={"user_id": _as_int(uid)})
        return {"task_id": tid, "assignee_id": uid, "response": data}

    def _remove_assignee(self, params: dict[str, Any]) -> dict[str, Any]:
        tid = self._resolve_task(params.get("task"))
        if not tid:
            raise ExecutorError("tasking.assignee.remove: task not found")
        uid = self._resolve_user(params.get("assignee"))
        if not uid:
            raise ExecutorError("tasking.assignee.remove: could not resolve assignee")
        self._delete(f"/api/v1/tasks/{tid}/assignees/{uid}")
        return {"task_id": tid, "assignee_id": uid}

    def _bulk_assignees(self, params: dict[str, Any]) -> dict[str, Any]:
        tid = self._resolve_task(params.get("task"))
        if not tid or not params.get("assignees"):
            raise ExecutorError("tasking.assignee.bulk: task/assignees required")
        bodies = self._assignee_bodies(params["assignees"])
        data = self._post(f"/api/v1/tasks/{tid}/assignees/bulk", json={"assignees": bodies})
        return {"task_id": tid, "assignees": bodies, "response": data}

    def _add_label(self, params: dict[str, Any]) -> dict[str, Any]:
        tid = self._resolve_task(params.get("task"))
        if not tid:
            raise ExecutorError("tasking.label.add: task not found")
        lid = self._resolve_label(params.get("label"))
        if not lid and params.get("label"):
            created = self._put(
                "/api/v1/labels",
                json={"title": str(params["label"]), "description": params.get("label_description") or ""},
            )
            lid = str((created or {}).get("id"))
        if not lid:
            raise ExecutorError("tasking.label.add: label not found")
        data = self._put(f"/api/v1/tasks/{tid}/labels", json={"label_id": _as_int(lid)})
        return {"task_id": tid, "label_id": lid, "response": data}

    def _remove_label(self, params: dict[str, Any]) -> dict[str, Any]:
        tid = self._resolve_task(params.get("task"))
        lid = self._resolve_label(params.get("label"))
        if not tid or not lid:
            raise ExecutorError("tasking.label.remove: task/label not found")
        self._delete(f"/api/v1/tasks/{tid}/labels/{lid}")
        return {"task_id": tid, "label_id": lid}

    def _bulk_labels(self, params: dict[str, Any]) -> dict[str, Any]:
        tid = self._resolve_task(params.get("task"))
        if not tid:
            raise ExecutorError("tasking.label.bulk: task required")
        labels = params.get("labels") or [params["label"]] if params.get("label") else []
        bodies = [{"id": x} for x in self._label_bodies(labels)]
        data = self._post(f"/api/v1/tasks/{tid}/labels/bulk", json={"labels": bodies})
        return {"task_id": tid, "labels": bodies, "response": data}

    def _create_comment(self, params: dict[str, Any]) -> dict[str, Any]:
        tid = self._resolve_task(params.get("task"))
        if not tid:
            raise ExecutorError("tasking.comment.create: task not found")
        if not params.get("content"):
            raise ExecutorError("tasking.comment.create: missing 'content'")
        data = self._put(f"/api/v1/tasks/{tid}/comments", json={"comment": str(params["content"])})
        if not (data or {}).get("id"):
            raise ExecutorError("tasking.comment.create: response missing comment id")
        return {"task_id": tid, "id": str(data["id"]), "response": data}

    def _update_comment(self, params: dict[str, Any]) -> dict[str, Any]:
        tid = self._resolve_task(params.get("task"))
        if not tid:
            raise ExecutorError("tasking.comment.update: task not found")
        cid = self._resolve_comment(tid, params.get("comment"))
        if not cid:
            raise ExecutorError("tasking.comment.update: comment not found")
        data = self._post(
            f"/api/v1/tasks/{tid}/comments/{cid}",
            json={"comment": str(params.get("content") or params.get("comment"))},
        )
        return {"task_id": tid, "id": cid, "response": data}

    def _delete_comment(self, params: dict[str, Any]) -> dict[str, Any]:
        tid = self._resolve_task(params.get("task"))
        if not tid:
            raise ExecutorError("tasking.comment.delete: task not found")
        cid = self._resolve_comment(tid, params.get("comment"))
        if not cid:
            raise ExecutorError("tasking.comment.delete: comment not found")
        self._delete(f"/api/v1/tasks/{tid}/comments/{cid}")
        return {"task_id": tid, "id": cid}

    def _create_relation(self, params: dict[str, Any]) -> dict[str, Any]:
        tid = self._resolve_task(params.get("task"))
        otid = self._resolve_task(params.get("related_task"))
        if not tid or not otid:
            raise ExecutorError("tasking.relation.create: task/related_task not found")
        kind = str(params.get("relation_kind") or params.get("relation") or "related")
        data = self._put(
            f"/api/v1/tasks/{tid}/relations",
            json={"other_task_id": _as_int(otid), "relation_kind": kind},
        )
        return {"task_id": tid, "related_task_id": otid, "kind": kind, "response": data}

    def _delete_relation(self, params: dict[str, Any]) -> dict[str, Any]:
        tid = self._resolve_task(params.get("task"))
        otid = self._resolve_task(params.get("related_task"))
        if not tid or not otid:
            raise ExecutorError("tasking.relation.delete: task/related_task not found")
        kind = str(params.get("relation_kind") or params.get("relation") or "related")
        self._delete(f"/api/v1/tasks/{tid}/relations/{kind}/{otid}")
        return {"task_id": tid, "related_task_id": otid, "kind": kind}

    def _upload_attachment_action(self, params: dict[str, Any]) -> dict[str, Any]:
        tid = self._resolve_task(params.get("task"))
        if not tid:
            raise ExecutorError("tasking.attachment.upload: task not found")
        files = self._attachment_files(params.get("attachments") or params.get("attachment"))
        uploaded = []
        for name in files:
            self._upload_attachment(tid, name)
            uploaded.append(name)
        if not uploaded:
            return {"task_id": tid, "deferred": True, "reason": "no binary attachment provided (needs content/bytes)"}
        return {"task_id": tid, "uploaded": uploaded}

    def _download_attachment(self, params: dict[str, Any]) -> dict[str, Any]:
        tid = self._resolve_task(params.get("task"))
        if not tid:
            raise ExecutorError("tasking.attachment.download: task not found")
        try:
            attachments = self._get(f"/api/v1/tasks/{tid}/attachments?per_page={_PER_PAGE}")
        except ExecutorError:
            raise ExecutorError("tasking.attachment.download: attachments not reachable")
        return {"task_id": tid, "attachments": attachments}

    def _delete_attachment(self, params: dict[str, Any]) -> dict[str, Any]:
        tid = self._resolve_task(params.get("task"))
        if not tid or not params.get("attachment"):
            raise ExecutorError("tasking.attachment.delete: task/attachment required")
        data = self._delete(f"/api/v1/tasks/{tid}/attachments/{params['attachment']}")
        return {"task_id": tid, "attachment_id": str(params["attachment"])}

    def _share_link(self, params: dict[str, Any]) -> dict[str, Any]:
        pid = self._resolve_project(params.get("project") or "Inbox")
        if not pid:
            raise ExecutorError("tasking.share.link: project not found")
        payload = {"shared": True}
        if params.get("recipient"):
            payload["name"] = str(params["recipient"])
        data = self._put(f"/api/v1/projects/{pid}/shares", json=payload)
        if not (data or {}).get("hash"):
            raise ExecutorError("tasking.share.link: response missing share hash")
        link = f"{self.base_url}/share/{data['hash']}"
        return {"project_id": pid, "hash": data["hash"], "link": link, "response": data}

    def _share_project(self, params: dict[str, Any]) -> dict[str, Any]:
        pid = self._resolve_project(params.get("project") or "Inbox")
        if not pid or not params.get("recipient"):
            raise ExecutorError("tasking.share.project: project/recipient required")
        data = self._put(f"/api/v1/projects/{pid}/users", json={"user_id": _as_int(params["recipient"])})
        return {"project_id": pid, "recipient": str(params["recipient"]), "response": data}

    def _list_notifications(self) -> dict[str, Any]:
        data = self._get(f"/api/v1/notifications?per_page={_PER_PAGE}")
        return {"notifications": data}

    def _mark_notifications_read(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("notification"):
            nid = _as_int(params["notification"])
            data = self._post(f"/api/v1/notifications/{nid}", json={})
            return {"id": str(nid), "response": data}
        self._post("/api/v1/notifications", json={})
        return {"all": True}

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    _CREATE_PRIORITY = {_PROJECT_CREATE: 0, _BUCKET_CREATE: 1, _VIEW_CREATE: 2, _TASK_CREATE: 3}

    def execute(self, ctx: ExecutionContext) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        self._turn_created = {}

        sorted_actions = sorted(
            ctx.actions.items(),
            key=lambda ap: self._CREATE_PRIORITY.get(ap[0], 10),
        )

        for action, payload in sorted_actions:
            params = (payload or {}).get("fields", {})
            try:
                if action == _TASK_CREATE:
                    data = self._create_task(params)
                elif action in (_TASK_UPDATE, _TASK_PARTIAL_UPDATE):
                    tid = self._resolve_task(params.get("task"))
                    data = self._update_task(tid, params) if tid else {"error": "task not found"}
                elif action == _TASK_DELETE:
                    tid = self._resolve_task(params.get("task"))
                    data = self._delete(f"/api/v1/tasks/{tid}") if tid else {"error": "task not found"}
                    if isinstance(data, dict):
                        data = {"id": tid, "response": data}
                    else:
                        data = {"id": tid}
                elif action == _TASK_DUPLICATE:
                    tid = self._resolve_task(params.get("task"))
                    if not tid:
                        data = {"error": "task not found"}
                    else:
                        body: dict[str, Any] = {}
                        if params.get("target_project"):
                            tpid = self._resolve_project(params["target_project"])
                            if tpid:
                                body["project_id"] = _as_int(tpid)
                        dup = self._put(f"/api/v1/tasks/{tid}/duplicate", json=body)
                        new = ((dup or {}).get("duplicated_task") or {}).get("id")
                        data = {"id": tid, "duplicate_id": str(new) if new else None, "response": dup}
                elif action == _TASK_READ:
                    tid = self._resolve_task(params.get("task"))
                    data = self._get(f"/api/v1/tasks/{tid}") if tid else {"error": "task not found"}
                elif action == _PROJECT_CREATE:
                    data = self._create_project(params)
                elif action == _PROJECT_UPDATE:
                    pid = self._resolve_project(params.get("project"))
                    data = self._update_project(pid, params) if pid else {"error": "project not found"}
                elif action == _PROJECT_READ:
                    pid = self._resolve_project(params.get("project"))
                    data = self._get(f"/api/v1/projects/{pid}") if pid else {"error": "project not found"}
                elif action == _PROJECT_DELETE:
                    pid = self._resolve_project(params.get("project"))
                    data = self._delete(f"/api/v1/projects/{pid}") if pid else {"error": "project not found"}
                    if isinstance(data, dict):
                        data = {"id": pid, "response": data}
                    else:
                        data = {"id": pid}
                elif action == _PROJECT_DUPLICATE:
                    pid = self._resolve_project(params.get("project"))
                    if not pid:
                        data = {"error": "project not found"}
                    else:
                        dup = self._put(f"/api/v1/projects/{pid}/duplicate", json={})
                        new = ((dup or {}).get("duplicated_project") or {}).get("id")
                        data = {"id": pid, "duplicate_id": str(new) if new else None, "response": dup}
                elif action == _BUCKET_CREATE:
                    data = self._create_bucket(params)
                elif action == _BUCKET_UPDATE:
                    data = self._update_bucket(params)
                elif action == _BUCKET_DELETE:
                    data = self._delete_bucket(params)
                elif action == _BUCKET_MOVE_TASK:
                    data = self._move_task_to_bucket(params)
                elif action == _VIEW_CREATE:
                    data = self._create_view(params)
                elif action == _VIEW_UPDATE:
                    data = self._update_view(params)
                elif action == _VIEW_DELETE:
                    data = self._delete_view(params)
                elif action == _ASSIGNEE_ADD:
                    data = self._add_assignee(params)
                elif action == _ASSIGNEE_REMOVE:
                    data = self._remove_assignee(params)
                elif action == _ASSIGNEE_BULK:
                    data = self._bulk_assignees(params)
                elif action == _LABEL_ADD:
                    data = self._add_label(params)
                elif action == _LABEL_REMOVE:
                    data = self._remove_label(params)
                elif action == _LABEL_BULK:
                    data = self._bulk_labels(params)
                elif action == _COMMENT_CREATE:
                    data = self._create_comment(params)
                elif action == _COMMENT_UPDATE:
                    data = self._update_comment(params)
                elif action == _COMMENT_DELETE:
                    data = self._delete_comment(params)
                elif action == _RELATION_CREATE:
                    data = self._create_relation(params)
                elif action == _RELATION_DELETE:
                    data = self._delete_relation(params)
                elif action == _ATTACH_UPLOAD:
                    data = self._upload_attachment_action(params)
                elif action == _ATTACH_DOWNLOAD:
                    data = self._download_attachment(params)
                elif action == _ATTACH_DELETE:
                    data = self._delete_attachment(params)
                elif action == _SHARE_LINK:
                    data = self._share_link(params)
                elif action == _SHARE_PROJECT:
                    data = self._share_project(params)
                elif action == _NOTIF_LIST:
                    data = self._list_notifications()
                elif action == _NOTIF_READ:
                    data = self._mark_notifications_read(params)
                else:
                    results.append(deferred_action(action, params))
                    continue

                results.append(
                    action_result(
                        action,
                        params,
                        tool_calls=[f"vikunja:{action}"],
                        data={"ok": True, "response": data},
                    )
                )
            except ExecutorError as exc:
                results.append(
                    action_result(
                        action,
                        params,
                        tool_calls=[f"vikunja:{action}"],
                        data={"ok": False, "error": str(exc)},
                    )
                )

        return executor_result(ctx.domain, results, attempts=ctx.retry_count + 1)