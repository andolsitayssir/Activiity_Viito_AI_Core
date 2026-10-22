"""Knowledge-base (RAG) domain executor backed by activiitykdb + qdrant.

Implements the ``kb.*`` action set from ``tools/attr_defs/knowledge_kb.json``:

* ``kb.answer.grounded`` / ``kb.search.vector`` -> the RAG panel's
  ``POST /api/chat/ask`` (real retrieval + answer over the Activiity KB).
* ``kb.search.fulltext`` -> lexicographic match over qdrant node payloads
  (scroll + local substring scan; qdrant has no text index).
* ``kb.status.batch`` / ``kb.status.processing`` -> GET /api/runs.
* ``kb.space.read`` -> distinct UA ids found in the qdrant collection.

Auth: none (panel + qdrant are LAN services). The panel's config endpoint
returns a private OpenRouter API key — the executor never reads it and never
echoes it into action results. Everything with no panel/qdrant surface
(ingest, note/generate/export, space write) is deferred.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
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
from server.config import model_defaults

_ANSWER_GROUNDED = "kb.answer.grounded"
_SEARCH_VECTOR = "kb.search.vector"
_SEARCH_FULLTEXT = "kb.search.fulltext"
_STATUS_BATCH = "kb.status.batch"
_STATUS_PROCESSING = "kb.status.processing"
_SPACE_READ = "kb.space.read"

_TIERS = ("reference", "medium", "advanced")
_DEFAULT_COLLECTION = "activiity_kb__ibm-granite_granite-embedding-311m-multilingual-r2__structure"
_ASK_TIMEOUT = 300
_UA_RE = re.compile(r"UA-\d+", re.IGNORECASE)


def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


class RagExecutor(DomainExecutor):
    domains: tuple[str, ...] = ("knowledge_kb",)
    backend = "activiitykdb"

    def __init__(self) -> None:
        self.rag_url = os.environ.get("RAG_URL", "http://localhost:8787").rstrip("/")
        self.qdrant_url = os.environ.get("QDRANT_URL", "http://localhost:6333").rstrip("/")
        self.collection = os.environ.get(
            "RAG_COLLECTION", _DEFAULT_COLLECTION
        )
        self._panel = HttpClient(self.rag_url)
        self._qdrant = HttpClient(self.qdrant_url)

    # ------------------------------------------------------------------
    # Qdrant helpers
    # ------------------------------------------------------------------
    def _resolve_collection(self) -> str:
        """Use the configured collection when it exists, else the first KB one."""
        try:
            data = self._qdrant.get("/collections")
            names = [c.get("name", "") for c in (data or {}).get("result", {}).get("collections", [])]
        except ExecutorError:
            names = [self.collection]
        if self.collection in names:
            return self.collection
        for name in names:
            if name.startswith("activiity_kb__") or "__structure" in name:
                return name
        if names:
            return names[0]
        raise ExecutorError("RAG executor: no qdrant collection found")

    def _scroll_points(self, collection: str, limit: int = 200, offset: Any = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "limit": min(limit, 1000),
            "with_payload": True,
            "with_vector": False,
        }
        if offset is not None:
            body["offset"] = offset
        return self._qdrant.post(
            f"/collections/{collection}/points/scroll", json=body
        )

    def _all_points(self, collection: str) -> list[dict[str, Any]]:
        points: list[dict[str, Any]] = []
        offset: Any = None
        while True:
            data = self._scroll_points(collection, offset=offset)
            result = (data or {}).get("result", {})
            batch = result.get("points", [])
            points.extend(batch)
            next_offset = result.get("next_page_offset")
            if next_offset is None:
                break
            offset = next_offset
        return points

    @staticmethod
    def _node_text(payload: dict[str, Any]) -> str:
        raw = payload.get("_node_content")
        text = ""
        if isinstance(raw, str):
            try:
                text = (json.loads(raw) or {}).get("text") or ""
            except (ValueError, TypeError):
                text = ""
        return text if isinstance(text, str) else ""

    def _fulltext(self, query: str, space: str | None, collection: str) -> dict[str, Any]:
        keywords = [
            _strip_accents(k).lower()
            for k in re.split(r"[\s.,;:!?()\"'-]+", query)
            if len(k) > 2
        ]
        hits: list[dict[str, Any]] = []
        for point in self._all_points(collection):
            payload = (point or {}).get("payload", {}) or {}
            ua_id = str(payload.get("ua_id") or "")
            ua_title = str(payload.get("ua_title") or "")
            if space:
                sp = str(space).strip()
                m = _UA_RE.search(sp.upper())
                if m:
                    if m.group(0) != ua_id.upper():
                        continue
                elif sp.lower() not in ua_title.lower():
                    continue
            text = _strip_accents(self._node_text(payload)).lower()
            if not keywords or all(k in text for k in keywords):
                hits.append(
                    {
                        "text": self._node_text(payload)[:3000],
                        "ua_id": ua_id,
                        "ua_title": payload.get("ua_title"),
                        "file_name": payload.get("file_name"),
                        "header": payload.get("header"),
                        "page": payload.get("page"),
                        "section_type": payload.get("section_type"),
                        "source_path": payload.get("source_path"),
                    }
                )
                if len(hits) >= 10:
                    break
        return {"total": len(hits), "hits": hits}

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _ask(self, params: dict[str, Any]) -> dict[str, Any]:
        question = params.get("question")
        if not question:
            raise ExecutorError("kb.answer.grounded: missing 'question'")
        space = str(params.get("space") or "").strip()
        target = space if space in _TIERS else "reference"
        models = model_defaults()
        body: dict[str, Any] = {
            "question": str(question),
            "target": target,
            "model": str(params.get("model") or models["RAG_ROUTER_MODEL"]),
            "synth_model": str(params.get("synth_model") or models["SYNTHESIZER_MODEL"]),
            "use_reranker": True,
            "compute_ragas": False,
        }
        if params.get("ground_truth"):
            body["ground_truth"] = str(params["ground_truth"])
        try:
            data = self._panel.post("/api/chat/ask", json=body, timeout=_ASK_TIMEOUT)
        except ExecutorError as exc:
            raise ExecutorError(f"kb.answer.grounded: {exc}", retryable=True) from exc
        if not isinstance(data, dict):
            raise ExecutorError("kb.answer.grounded: panel returned a non-JSON body")
        return {
            "answer": data.get("answer"),
            "model": data.get("model"),
            "contexts": data.get("contexts") or [],
            "units_called": data.get("units_called") or [],
            "tools_called": data.get("tools_called") or [],
            "iterations": data.get("iterations"),
            "space": space or target,
        }

    def _status(self) -> dict[str, Any]:
        try:
            runs = self._panel.get("/api/runs")
        except ExecutorError as exc:
            raise ExecutorError(f"kb.status.batch: {exc}") from exc
        runs = runs if isinstance(runs, list) else []
        compact = [
            {
                "run_id": r.get("id"),
                "status": r.get("status"),
                "tier": r.get("tier"),
                "gate": r.get("gate"),
                "ragas": r.get("ragas"),
                "mock": r.get("mock"),
                "progress": r.get("progress"),
                "started_at": r.get("started_at"),
                "finished_at": r.get("finished_at"),
                "error": r.get("error"),
            }
            for r in runs
        ]
        return {"n_runs": len(compact), "runs": compact}

    def _processing(self, params: dict[str, Any]) -> dict[str, Any]:
        data = self._status()
        space = str(params.get("space") or "").strip()
        doc = str(params.get("document") or "").strip()
        runs = data["runs"]
        if space:
            runs = [r for r in runs if str(r.get("tier") or "").lower() == space.lower()]
        if doc:
            runs = [r for r in runs if doc.lower() in str(r.get("run_id") or "").lower()]
        latest = runs[0] if runs else None
        if not latest:
            return {"found": False, "reason": "no run found for document/space", "runs": runs}
        return {"found": True, "status": latest.get("status"), "gate": latest.get("gate"), "run": latest, "runs": runs}

    def _spaces(self) -> dict[str, Any]:
        collection = self._resolve_collection()
        seen: dict[str, str] = {}
        for point in self._all_points(collection):
            payload = (point or {}).get("payload", {}) or {}
            ua_id = payload.get("ua_id")
            if not ua_id:
                continue
            seen.setdefault(str(ua_id), str(payload.get("ua_title") or ""))
        spaces = [{"id": ua_id, "title": title} for ua_id, title in sorted(seen.items())]
        return {"collection": collection, "spaces": spaces}

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    def execute(self, ctx: ExecutionContext) -> dict[str, Any]:
        results: list[dict[str, Any]] = []

        for action, payload in ctx.actions.items():
            params = (payload or {}).get("fields", {})
            try:
                if action == _ANSWER_GROUNDED:
                    data = self._ask(params)
                elif action == _SEARCH_VECTOR:
                    data = self._ask({**params, "question": params.get("search_query") or params.get("question"), "space": params.get("space")})
                elif action == _SEARCH_FULLTEXT:
                    query = params.get("search_query") or ""
                    if not query:
                        raise ExecutorError("kb.search.fulltext: missing 'search_query'")
                    collection = self._resolve_collection()
                    data = self._fulltext(query, params.get("space"), collection)
                elif action == _STATUS_BATCH:
                    data = self._status()
                elif action == _STATUS_PROCESSING:
                    data = self._processing(params)
                elif action == _SPACE_READ:
                    data = self._spaces()
                else:
                    results.append(deferred_action(action, params))
                    continue

                results.append(
                    action_result(
                        action,
                        params,
                        tool_calls=[f"activiitykdb:{action}"],
                        data={"ok": True, "response": data},
                    )
                )
            except ExecutorError as exc:
                results.append(
                    action_result(
                        action,
                        params,
                        tool_calls=[f"activiitykdb:{action}"],
                        data={"ok": False, "error": str(exc)},
                    )
                )

        return executor_result(ctx.domain, results, attempts=ctx.retry_count + 1)