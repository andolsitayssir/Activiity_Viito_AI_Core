"""Executor node — dispatches the current domain to its DomainExecutor.

Step 1 shipped this node as a stub; step 2 replaced the canned success with a
real registry: the node looks up ``current_domain`` in the executor registry
and runs the domain's actions against the live backend (or a deferred no-op
when the backend isn't wired yet). The ExecutorResult-layout contract is
unchanged, so the validator/formatter never needed modification.
"""

from __future__ import annotations

from typing import Any

from server.execution.executors.base import (
    ExecutorError,
    ExecutionContext,
    executor_result,
)
from server.execution.executors.registry import get_registry


def executor_node(state: dict[str, Any]) -> dict[str, Any]:
    domain = state.get("current_domain") or "general"
    router_output = state.get("router_output") or {}
    actions_for_domain = (router_output.get("fields") or {}).get(domain, {})

    ctx = ExecutionContext(
        domain=domain,
        actions=actions_for_domain or {},
        session_memory=state.get("session_memory") or {},
        retry_count=int(state.get("retry_count") or 0),
        error_history=list(state.get("error_history") or []),
        user_id=state.get("user_id", "system"),
        org_id=state.get("org_id", "activiity"),
        conversation_id=state.get("conversation_id", ""),
        intent=state.get("intent") or (router_output.get("intent") or ""),
        attachments=state.get("attachments") or [],
        turn_id=str(state.get("thread_id") or state.get("turn_id") or ""),
        executor_log=list((state.get("executor_log") or [])[int(state.get("_log_start") or 0):]),
    )

    executor = get_registry().executor_for(domain)
    try:
        executor_result_dict = executor.execute(ctx)
    except ExecutorError as exc:
        executor_result_dict = executor_result(
            domain,
            [],
            status="error",
            error=str(exc),
            attempts=ctx.retry_count + 1,
        )
    except Exception as exc:  # noqa: BLE001 - backend failures must stay in-band
        executor_result_dict = executor_result(
            domain,
            [],
            status="error",
            error=f"{domain}: {exc}",
            attempts=ctx.retry_count + 1,
        )

    import json as _json
    print(f"[EXECUTOR] domain={domain!r} result={_json.dumps(executor_result_dict, ensure_ascii=False, default=str)[:800]}", flush=True)

    return {
        "executor_result": executor_result_dict,
        "executor_log": [executor_result_dict],
    }