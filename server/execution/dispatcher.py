"""Dispatcher node — pure Python, no LLM, no MCP.

Responsibilities (per spec):
  1. Read the domains list from ``router_output``.
  2. Map each domain -> its executor node name (via ``DOMAIN_EXECUTOR_MAP``).
  3. Multi-domain: sequential by default, in the order given.

The dispatcher is entered ONLY at ``START`` (fresh turn) and always re-initializes
the per-turn execution fields from ``router_output``. Popping the *next* queued
domain after a successful validation is a separate, tiny ``advance_domain`` node.

All functions here are pure: fully unit-testable without LLM/MCP.
"""

from __future__ import annotations

from typing import Any

from server.execution.config import DEFAULT_DOMAIN


def normalize_domain(value: Any) -> str:
    return str(value).strip().lower()


def domains_from_router_output(router_output: dict[str, Any] | None) -> list[str]:
    """Ordered, deduplicated domain list. Empty/missing falls back to general."""
    if not router_output:
        return [DEFAULT_DOMAIN]
    raw = router_output.get("domains") or []
    seen: list[str] = []
    for value in raw:
        domain = normalize_domain(value)
        if domain and domain not in seen:
            seen.append(domain)
    return seen or [DEFAULT_DOMAIN]


def executor_node_for(domain: str) -> str:
    """Map a domain to its executor node name; unknown domains -> general."""
    from server.execution.config import DOMAIN_EXECUTOR_MAP, UNKNOWN_DOMAIN

    return DOMAIN_EXECUTOR_MAP.get(normalize_domain(domain)) or DOMAIN_EXECUTOR_MAP.get(
        UNKNOWN_DOMAIN
    )


def build_domain_plan(domains: list[str]) -> tuple[str, list[str]]:
    """Return ``(current_domain, domain_queue)`` for sequential execution.

    Pure and side-effect free — the heart of the dispatcher, unit-tested.
    """
    pending = domains_from_router_output({"domains": domains})
    current = pending[0]
    queue = pending[1:]
    return current, queue


def dispatch(state: dict[str, Any]) -> dict[str, Any]:
    """Dispatcher node body: init a fresh turn from router_output.

    Called once per turn at START. Resets per-turn execution fields so a
    checkpointed previous turn never leaks into this one.
    """
    router_output = state.get("router_output") or {}
    current, queue = build_domain_plan(router_output.get("domains") or [])
    print(f"[DISPATCHER] current_domain={current!r} domain_queue={queue!r}", flush=True)

    return {
        "current_domain": current,
        "domain_queue": queue,
        "retry_count": 0,
        "error_history": [],
        "executor_result": None,
        "validation_result": None,
        "outcome": "finished",  # pending; overridden by validator/formatter
        "_log_start": len(state.get("executor_log") or []),
    }


def advance_domain(state: dict[str, Any]) -> dict[str, Any]:
    """Pop the next queued domain into ``current_domain`` and reset per-domain
    retry/error state. Called after a successful validation when the queue is
    non-empty.
    """
    queue = list(state.get("domain_queue") or [])
    if not queue:
        return {"domain_queue": []}

    return {
        "current_domain": queue[0],
        "domain_queue": queue[1:],
        "retry_count": 0,
        "error_history": [],
        "executor_result": None,
        "validation_result": None,
    }


def route_after_validation(state: dict[str, Any]) -> str:
    """Conditional-edge router after the validator node.

    The validator records ``validation_result.stage`` (structural | semantic)
    so the routing decision here can apply the right retry budget:

    structural failure + execution retries left     -> "retry"
    structural failure + retries exhausted          -> "escalate"
    semantic mismatch  + routing retries left       -> "reroute"
    semantic mismatch  + routing retries exhausted  -> "escalate"
    success + queue non-empty                       -> "next_domain"
    success + queue empty                           -> "done"

    Returns one of:
      "retry"        -> re-run executor on the SAME domain (bounded MAX_RETRIES)
      "reroute"      -> re-run the WHOLE pipeline via the router node
                        (bounded MAX_ROUTER_RETRIES)
      "escalate"     -> retries exhausted -> formatter (honest failure)
      "next_domain"  -> queue non-empty -> advance_domain
      "done"         -> all domains handled -> formatter
    """
    from server.execution.config import MAX_RETRIES, MAX_ROUTER_RETRIES

    validation = state.get("validation_result") or {}
    stage = validation.get("stage") or "structural"

    if not validation.get("passed"):
        if stage == "semantic":
            router_retries = int(state.get("router_retry_count") or 0)
            if router_retries <= MAX_ROUTER_RETRIES:
                return "reroute"
            return "escalate"
        retries = int(state.get("retry_count") or 0)
        if retries <= MAX_RETRIES:
            return "retry"
        return "escalate"

    if state.get("domain_queue"):
        return "next_domain"
    return "done"