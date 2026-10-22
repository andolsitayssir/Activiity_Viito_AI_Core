from __future__ import annotations

import json
import os
import time
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from server.agents.router_v2 import (
    ROUTING_LABELS,
    build_openrouter_client,
)
from server.eval.routing_evaluation_v2 import (
    evaluate_routing,
    load_eval_samples,
    _read_csv_rows,
    _read_json_rows,
    _read_xlsx_rows,
)

router = APIRouter(prefix="/eval", tags=["eval"])
api_router = APIRouter(prefix="/api", tags=["api"])

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_METRICS_ROOT = PROJECT_ROOT / "metrics"
PROMPTS_DIR = PROJECT_ROOT / "server" / "prompts"
DEFAULT_PROMPT_PATH = PROMPTS_DIR / "prompt_router_v3_fr_fs.md"
ZERO_PROMPT_PATH = PROMPTS_DIR / "prompt_router_v3_fr_zs.md"
UPLOAD_DIR = Path(__file__).resolve().parent / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Viito — execution log (executor_log)
# Read-only view of the PipelineState.executor_log contract
# (see server/execution/state.py). Seeded per-session until the Logger node
# gains a durable sink; still returns the exact row shape the UI renders.
# ---------------------------------------------------------------------------
_SEED_EXECUTOR_LOG: list[dict] = [
    {
        "user_id": "u-1004",
        "org_id": "org-1",
        "conversation_id": "conv-1004",
        "domain": "email_action",
        "action_id": "email.email_submission.set",
        "attributes": {"to": "hugo.lefebvre@example.fr", "subject": "Confirmation appel"},
        "status": "success",
        "executor": "stalwart",
        "ts": "2026-08-31T14:08:00Z",
    },
    {
        "user_id": "u-1004",
        "org_id": "org-1",
        "conversation_id": "conv-1004",
        "domain": "calendaring",
        "action_id": "event.create",
        "attributes": {"subject": "Appel semaine prochaine", "start": "jeudi 14:00"},
        "status": "success",
        "executor": "nextcloud",
        "ts": "2026-08-31T14:09:00Z",
    },
]


PREDEFINED_MODELS = [
    {"id": "google/gemma-4-26b-a4b-it", "name": "Gemma 4 26B (A4B-IT)"},
    {"id": "google/gemma-4-31b-it", "name": "Gemma 4 31B"},
    {"id": "qwen/qwen3-14b", "name": "Qwen3 14B"},
    {"id": "qwen/qwen3-vl-8b-instruct", "name": "Qwen3 VL 8B"},
    {"id": "qwen/qwen3-vl-32b-instruct", "name": "Qwen3 VL 32B"},
]


@dataclass
class EvalTask:
    task_id: str
    status: str
    model_name: str
    prompt_version: str
    dataset_path: str
    started_at: float = 0.0
    finished_at: float = 0.0
    progress: str = ""
    completed: int = 0
    total: int = 0
    rate: float = 0.0
    summary: dict[str, Any] | None = None
    error: str | None = None


_tasks: dict[str, EvalTask] = {}
_tasks_lock = threading.Lock()


class EvalRunRequest(BaseModel):
    model_name: str
    prompt_version: str = "fewshot"
    dataset_path: str
    max_workers: int = 8
    text_column: str | None = None
    label_column: str | None = None


def _run_eval_background(task: EvalTask, dataset_path: str, model_name: str, prompt_version: str, max_workers: int, text_column: str | None, label_column: str | None):
    try:
        prompt_path = ZERO_PROMPT_PATH if prompt_version == "zeroshot" else DEFAULT_PROMPT_PATH

        model_client = build_openrouter_client(
            model=model_name,
            temperature=0.0,
            max_tokens=128,
            prompt_version=prompt_version,
        )

        with _tasks_lock:
            task.status = "running"
            task.started_at = time.time()
            task.progress = "Loading dataset..."

        samples = load_eval_samples(Path(dataset_path), text_column, label_column)
        with _tasks_lock:
            task.progress = f"Loaded {len(samples)} samples. Starting evaluation..."
            task.total = len(samples)

        def _on_progress(completed: int, total: int, rate: float):
            with _tasks_lock:
                task.completed = completed
                task.total = total
                task.rate = rate

        summary = evaluate_routing(
            data_root=dataset_path,
            metrics_root=DEFAULT_METRICS_ROOT,
            prompt_path=prompt_path,
            model_name=model_name,
            model_client=model_client,
            show_progress=False,
            prompt_version=prompt_version,
            request_delay=0.3,
            max_workers=max_workers,
            text_column=text_column,
            label_column=label_column,
            on_progress=_on_progress,
        )

        with _tasks_lock:
            task.status = "done"
            task.finished_at = time.time()
            task.summary = summary.to_dict()
            task.progress = "Complete"

    except Exception as exc:
        with _tasks_lock:
            task.status = "error"
            task.finished_at = time.time()
            task.error = str(exc)
            task.progress = f"Error: {exc}"


@router.get("/", response_class=HTMLResponse)
def serve_ui():
    html_path = STATIC_DIR / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@router.get("/models")
def list_models():
    return {"predefined": PREDEFINED_MODELS, "labels": list(ROUTING_LABELS)}


@router.post("/upload")
async def upload_dataset(file: UploadFile = File(...)):
    if not file.filename:
        return JSONResponse(status_code=400, content={"error": "No file provided"})
    ext = Path(file.filename).suffix.lower()
    if ext not in {".json", ".csv", ".xlsx"}:
        return JSONResponse(status_code=400, content={"error": f"Unsupported file type: {ext}. Use .json, .csv, or .xlsx"})

    file_id = uuid.uuid4().hex[:12]
    save_path = UPLOAD_DIR / f"{file_id}{ext}"
    content = await file.read()
    save_path.write_bytes(content)

    try:
        samples = load_eval_samples(save_path)
        sample_count = len(samples)
    except Exception:
        sample_count = -1

    return {
        "file_id": file_id,
        "filename": file.filename,
        "path": str(save_path),
        "sample_count": sample_count,
    }


@router.post("/run")
def start_eval(payload: EvalRunRequest):
    task_id = uuid.uuid4().hex[:12]
    task = EvalTask(
        task_id=task_id,
        status="pending",
        model_name=payload.model_name,
        prompt_version=payload.prompt_version,
        dataset_path=payload.dataset_path,
    )
    with _tasks_lock:
        _tasks[task_id] = task

    thread = threading.Thread(
        target=_run_eval_background,
        args=(task, payload.dataset_path, payload.model_name, payload.prompt_version, payload.max_workers, payload.text_column, payload.label_column),
        daemon=True,
    )
    thread.start()

    return {"task_id": task_id, "status": "pending"}


@router.get("/columns")
def get_columns(path: str):
    file_path = Path(path.replace("/", "\\") if "\\" not in path and "/" in path else path)
    if not file_path.exists():
        return JSONResponse(status_code=404, content={"error": f"File not found: {path}"})
    try:
        suffix = file_path.suffix.lower()
        if suffix == ".csv":
            rows = _read_csv_rows(file_path)
        elif suffix == ".json":
            rows = _read_json_rows(file_path)
        else:
            rows = _read_xlsx_rows(file_path)
        if not rows:
            return JSONResponse(status_code=400, content={"error": "No data rows found"})
        columns = list(rows[0].keys())
        sample = {k: rows[0][k] for k in columns[:10]}
        return {"columns": columns, "sample": sample, "total_rows": len(rows)}
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})


@router.get("/status/{task_id}")
def get_status(task_id: str):
    with _tasks_lock:
        task = _tasks.get(task_id)
    if not task:
        return JSONResponse(status_code=404, content={"error": "Task not found"})

    elapsed = 0.0
    if task.started_at > 0:
        end = task.finished_at if task.finished_at > 0 else time.time()
        elapsed = end - task.started_at

    return {
        "task_id": task.task_id,
        "status": task.status,
        "model_name": task.model_name,
        "prompt_version": task.prompt_version,
        "progress": task.progress,
        "completed": task.completed,
        "total": task.total,
        "rate": round(task.rate, 2),
        "elapsed_seconds": round(elapsed, 1),
        "error": task.error,
    }


@router.get("/results/{task_id}")
def get_results(task_id: str):
    with _tasks_lock:
        task = _tasks.get(task_id)
    if not task:
        return JSONResponse(status_code=404, content={"error": "Task not found"})
    if task.status != "done":
        return JSONResponse(status_code=400, content={"error": f"Task is {task.status}"})
    return task.summary


@api_router.get("/executor_log")
def list_executor_log(conversation_id: str | None = None, limit: int = 50):
    """Read-only executor log (PipelineState.executor_log).

    Seeded with representative rows until the Logger node persists real runs.
    Optional filters: conversation_id, limit.
    """
    rows = _SEED_EXECUTOR_LOG
    if conversation_id:
        rows = [r for r in rows if r.get("conversation_id") == conversation_id]
    return {"executor_log": rows[:max(0, min(limit, 500))], "total": len(rows)}


@router.get("/runs")
def list_past_runs():
    runs_dir = DEFAULT_METRICS_ROOT / "runs"
    if not runs_dir.exists():
        return {"runs": []}

    results = []
    for run_dir in sorted(runs_dir.iterdir(), reverse=True):
        summary_path = run_dir / "summary.json"
        if summary_path.exists():
            try:
                s = json.loads(summary_path.read_text(encoding="utf-8"))
                results.append({
                    "run_id": s.get("run_id", ""),
                    "run_name": s.get("run_name", ""),
                    "model_name": s.get("model_name", ""),
                    "prompt_version": s.get("prompt_version", ""),
                    "total_samples": s.get("total_samples", 0),
                    "accuracy": s.get("exact_match_accuracy", 0),
                    "f1": s.get("micro_f1", 0),
                    "precision": s.get("micro_precision", 0),
                    "recall": s.get("micro_recall", 0),
                    "runtime_seconds": s.get("runtime_seconds", 0),
                    "run_dir": str(run_dir),
                })
            except Exception:
                continue
    return {"runs": results}
