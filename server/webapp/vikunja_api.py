"""Viito HTTP API — Vikunja tasking backend.

Read-only browsing surface for the running Vikunja instance (localhost:3456).
Reuses the production ``VikunjaExecutor`` (same JWT auth, same client) so the
webapp always talks to the exact backend the AI engine drives. The optional
``vikunja_config.json`` store lets the Configuration page override the
environment credentials (VIKUNJA_URL/USERNAME/PASSWORD) without restarting.

Endpoints
---------
GET  /vikunja/status             -> { configured, connected, url, username, user, error }
POST /vikunja/config             -> save { url, username, password } + connection test
GET  /vikunja/projects           -> [{ id, title, description, virtual, ... }]
GET  /vikunja/projects/{id}      -> project detail (incl. views)
GET  /vikunja/projects/{id}/tasks-> tasks belonging to one project
GET  /vikunja/tasks              -> all tasks (optional ?s= search, ?project= filter)
GET  /vikunja/tasks/{id}         -> task detail (labels/assignees/related/attachments)
GET  /vikunja/tasks/{id}/comments-> comments of a task
GET  /vikunja/labels             -> label list
GET  /vikunja/notifications      -> notifications
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server.config import load_environment
from server.execution.executors.base import ExecutorError
from server.execution.executors.http import HttpClient
from server.execution.executors.vikunja import VikunjaExecutor

load_environment()

router = APIRouter(tags=["vikunja"])

_CONFIG_PATH = Path(__file__).resolve().parent / "vikunja_config.json"
_lock = threading.Lock()

_PER_PAGE = 200

_DEFAULT_URL = os.environ.get("VIKUNJA_URL", "http://localhost:3456").rstrip("/")

#: Reused authenticated clients keyed by effective credentials so the UI burst
#: (several parallel GETs) triggers a single /login instead of one per request,
#: which Vikunja rate-limits (no-auth routes: 10/60s per IP) and then 429s.
_AUTH_CACHE: dict[tuple[str, str, str], VikunjaExecutor] = {}


def _load_config() -> dict:
    try:
        if _CONFIG_PATH.exists():
            data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:  # noqa: BLE001 - best-effort config read
        pass
    return {}


def _save_config(config: dict) -> None:
    try:
        _CONFIG_PATH.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001 - best-effort config write
        pass


def _apply_store() -> dict:
    """The effective credentials: UI store overrides the environment."""
    store = _load_config()
    return {
        "url": (store.get("url") or _DEFAULT_URL).rstrip("/"),
        "username": store.get("username") or os.environ.get("VIKUNJA_USERNAME", ""),
        "password": store.get("password") or os.environ.get("VIKUNJA_PASSWORD", ""),
    }


def _client() -> VikunjaExecutor:
    cfg = _apply_store()
    if not cfg["username"] or not cfg["password"]:
        raise HTTPException(
            status_code=503,
            detail="Vikunja non configuré (VIKUNJA_USERNAME/PASSWORD absents).",
        )
    key = (cfg["url"], cfg["username"], cfg["password"])
    with _lock:
        executor = _AUTH_CACHE.get(key)
        if executor is None:
            executor = VikunjaExecutor()
            executor.base_url = cfg["url"]
            executor.username = cfg["username"]
            executor.password = cfg["password"]
            executor._http = HttpClient(cfg["url"])
            executor._token = None
            # Authenticate once now so every concurrent request reuses the token
            # instead of hammering the (rate-limited) /login endpoint.
            try:
                executor._get("/api/v1/user")
            except ExecutorError as exc:
                raise HTTPException(status_code=503, detail=f"Vikunja auth: {exc}") from exc
            _AUTH_CACHE[key] = executor
            while len(_AUTH_CACHE) > 4:
                oldest_key, oldest = next(iter(_AUTH_CACHE.items()))
                _AUTH_CACHE.pop(oldest_key)
                try:
                    oldest._http.close()
                except Exception:  # noqa: BLE001 - best-effort session cleanup
                    pass
        return executor


def _failed(message: str) -> dict:
    return {"configured": False, "connected": False, "url": None, "username": None, "user": None, "error": message}


def _status(executor: VikunjaExecutor | None = None, cfg: dict | None = None) -> dict:
    cfg = cfg if cfg is not None else _apply_store()
    if not cfg["username"] or not cfg["password"]:
        return _failed("VIKUNJA_USERNAME/VIKUNJA_PASSWORD sont absents de l'environnement.")
    try:
        exec_ = executor or _client()
        user = exec_._get("/api/v1/user")
        name = (user or {}).get("name") or (user or {}).get("username") or ""
        return {
            "configured": True,
            "connected": True,
            "url": cfg["url"],
            "username": cfg["username"],
            "user": {"id": (user or {}).get("id"), "name": name, "username": (user or {}).get("username", "")},
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - surfaced as an explicit status
        return {
            "configured": True,
            "connected": False,
            "url": cfg["url"],
            "username": cfg["username"],
            "user": None,
            "error": str(exc or type(exc).__name__),
        }


def _require_connected(executor: VikunjaExecutor) -> None:
    status = _status(executor)
    if not status["connected"]:
        raise HTTPException(
            status_code=503,
            detail=status.get("error") or "Vikunja injoignable.",
        )


class VikunjaConfigRequest(BaseModel):
    url: str = ""
    username: str = ""
    password: str = ""


@router.get("/vikunja/status")
def vikunja_status():
    cfg = _apply_store()
    with _lock:
        try:
            exec_ = _client() if cfg["username"] and cfg["password"] else None
        except HTTPException:
            return _status(cfg=cfg)
    return _status(exec_ if exec_ is not None else None, cfg)


@router.post("/vikunja/config")
def vikunja_save_config(payload: VikunjaConfigRequest):
    url = (payload.url or "").strip().rstrip("/") or _DEFAULT_URL
    username = payload.username.strip()
    password = payload.password
    if not username or not password:
        raise HTTPException(status_code=400, detail="Nom d'utilisateur et mot de passe sont requis.")
    with _lock:
        _save_config({"url": url, "username": username, "password": password})
        # Credentials changed: drop cached sessions so the next request re-logs in.
        for cached_key, cached in list(_AUTH_CACHE.items()):
            if cached_key[:2] == (url, username):
                _AUTH_CACHE.pop(cached_key, None)
                try:
                    cached._http.close()
                except Exception:  # noqa: BLE001 - best-effort session cleanup
                    pass
    # Test the freshly-saved connection immediately.
    cfg = {"url": url, "username": username, "password": password}
    return _status(_client(), cfg)


@router.get("/vikunja/projects")
def vikunja_projects():
    exec_ = _client()
    _require_connected(exec_)
    try:
        projects = exec_._get(f"/api/v1/projects?per_page={_PER_PAGE}")
    except ExecutorError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    projects = projects if isinstance(projects, list) else []
    rows = []
    for proj in projects:
        rows.append({
            "id": proj.get("id"),
            "title": proj.get("title") or "",
            "description": proj.get("description") or "",
            "virtual": bool(proj.get("id") is not None and proj["id"] < 0),
            "is_archived": bool(proj.get("is_archived")),
            "is_favorite": bool(proj.get("is_favorite")),
            "hex_color": proj.get("hex_color") or "",
            "views": ([v.get("view_kind") or "list" for v in proj.get("views") or []]) or [],
            "created": proj.get("created") or "",
            "updated": proj.get("updated") or "",
        })
    rows.sort(key=lambda p: (p["virtual"], (p["title"] or "").lower()))
    return {"projects": rows}


@router.get("/vikunja/projects/{project_id}")
def vikunja_project_detail(project_id: int):
    exec_ = _client()
    _require_connected(exec_)
    try:
        project = exec_._get(f"/api/v1/projects/{project_id}")
    except ExecutorError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not isinstance(project, dict) or project.get("id") is None:
        raise HTTPException(status_code=404, detail=f"Projet {project_id} introuvable")
    return {
        "id": project.get("id"),
        "title": project.get("title") or "",
        "description": project.get("description") or "",
        "hex_color": project.get("hex_color") or "",
        "is_archived": bool(project.get("is_archived")),
        "is_favorite": bool(project.get("is_favorite")),
        "views": project.get("views") or [],
        "created": project.get("created") or "",
        "updated": project.get("updated") or "",
    }


def _task_row(task: dict) -> dict:
    def _labels():
        out: list[dict] = []
        for label in task.get("labels") or []:
            if isinstance(label, dict):
                out.append({"id": label.get("id"), "title": label.get("title") or "", "hex_color": label.get("hex_color") or ""})
            elif label is not None:
                out.append({"id": label, "title": str(label)})
        return out

    def _assignees():
        out: list[dict] = []
        for a in task.get("assignees") or []:
            if isinstance(a, dict):
                out.append({"id": a.get("id"), "name": a.get("name") or a.get("username") or ""})
            elif a is not None:
                out.append({"id": a, "name": str(a)})
        return out

    attachments = task.get("attachments") or []
    if isinstance(attachments, (int, float)):
        attachments = []
    return {
        "id": task.get("id"),
        "title": task.get("title") or "",
        "description": task.get("description") or "",
        "done": bool(task.get("done")),
        "priority": task.get("priority") or 0,
        "project_id": task.get("project_id"),
        "bucket_id": task.get("bucket_id"),
        "position": task.get("position"),
        "index": task.get("index"),
        "identifier": task.get("identifier") or f"#{task.get('index')}",
        "due_date": task.get("due_date") or "",
        "start_date": task.get("start_date") or "",
        "end_date": task.get("end_date") or "",
        "percent_done": task.get("percent_done") or 0,
        "hex_color": task.get("hex_color") or "",
        "is_favorite": bool(task.get("is_favorite")),
        "labels": _labels(),
        "assignees": _assignees(),
        "created_by": (task.get("created_by") or {}).get("username") if isinstance(task.get("created_by"), dict) else "",
        "created": task.get("created") or "",
        "updated": task.get("updated") or "",
        "done_at": task.get("done_at") or "",
        "attachments": len(attachments) if isinstance(attachments, list) else 0,
        "related_tasks": len(task.get("related_tasks") or {}) if isinstance(task.get("related_tasks"), dict) else 0,
    }


@router.get("/vikunja/projects/{project_id}/tasks")
def vikunja_project_tasks(project_id: int, s: str = ""):
    exec_ = _client()
    _require_connected(exec_)
    try:
        tasks = exec_._get(f"/api/v1/projects/{project_id}/tasks?per_page={_PER_PAGE}" + (f"&s={s}" if s else ""))
    except ExecutorError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    tasks = tasks if isinstance(tasks, list) else []
    return {"tasks": [_task_row(t) for t in tasks]}


@router.get("/vikunja/tasks")
def vikunja_tasks(s: str = "", project: int | None = None, done: str = ""):
    exec_ = _client()
    _require_connected(exec_)
    query = f"/api/v1/tasks?per_page={_PER_PAGE}"
    if s:
        query += f"&s={s}"
    if project is not None:
        query += f"&project_id={project}"
    if done in ("true", "false"):
        query += f"&done={done}"
    try:
        tasks = exec_._get(query)
    except ExecutorError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    tasks = tasks if isinstance(tasks, list) else []
    return {"tasks": [_task_row(t) for t in tasks]}


@router.get("/vikunja/tasks/{task_id}")
def vikunja_task_detail(task_id: int):
    exec_ = _client()
    _require_connected(exec_)
    try:
        task = exec_._get(f"/api/v1/tasks/{task_id}")
    except ExecutorError as exc:
        if "404" in str(exc):
            raise HTTPException(status_code=404, detail=f"Tâche {task_id} introuvable") from exc
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not isinstance(task, dict) or task.get("id") is None:
        raise HTTPException(status_code=404, detail=f"Tâche {task_id} introuvable")
    return {"task": _task_row(task)}


@router.get("/vikunja/tasks/{task_id}/comments")
def vikunja_task_comments(task_id: int):
    exec_ = _client()
    _require_connected(exec_)
    try:
        comments = exec_._get(f"/api/v1/tasks/{task_id}/comments?per_page={_PER_PAGE}")
    except ExecutorError as exc:
        if "404" in str(exc):
            raise HTTPException(status_code=404, detail=f"Tâche {task_id} introuvable") from exc
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    comments = comments if isinstance(comments, list) else []
    rows = []
    for c in comments:
        author = c.get("author") or {}
        rows.append({
            "id": c.get("id"),
            "comment": c.get("comment") or "",
            "author": (author.get("name") or author.get("username") or "") if isinstance(author, dict) else "",
            "created": c.get("created") or "",
            "updated": c.get("updated") or "",
        })
    return {"comments": rows}


@router.get("/vikunja/labels")
def vikunja_labels():
    exec_ = _client()
    _require_connected(exec_)
    try:
        labels = exec_._get(f"/api/v1/labels?per_page={_PER_PAGE}")
    except ExecutorError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    labels = labels if isinstance(labels, list) else []
    return {"labels": [{
        "id": l.get("id"),
        "title": l.get("title") or "",
        "description": l.get("description") or "",
        "hex_color": l.get("hex_color") or "",
    } for l in labels]}


@router.get("/vikunja/notifications")
def vikunja_notifications():
    exec_ = _client()
    _require_connected(exec_)
    try:
        notifications = exec_._get(f"/api/v1/notifications?per_page={_PER_PAGE}")
    except ExecutorError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    notifications = notifications if isinstance(notifications, list) else []
    rows = []
    for n in notifications:
        rows.append({
            "id": n.get("id"),
            "name": n.get("name") or "",
            "notification_type": n.get("notification_type") or "",
            "read": bool(n.get("read")),
            "created": n.get("created") or "",
        })
    return {"notifications": rows}