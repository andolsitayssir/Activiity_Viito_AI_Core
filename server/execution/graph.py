"""Execution graph — StateGraph wiring.

Nodes (first entry order):
    router -> dispatcher -> executor -> validator -> {reroute|retry|escalate|next_domain|done}
    advance_domain -> executor        (next sequential domain)
    formatter -> END

The ``router`` node classifies the raw query (domain -> action -> attributes)
inside the graph. On a semantic mismatch the validator routes back to it
(``reroute``) to attempt a fresh, bounded whole-pipeline retry.

A single ``executor`` node is shared across domains; it routes internally on
``current_domain`` to the per-domain DomainExecutor registry (see step 2),
which preserves MCP tool scoping (each DomainExecutor holds ONLY its domain's
MCP client). ``DOMAIN_EXECUTOR_MAP`` in config is the domain -> executor name
mapping the dispatcher reads.
"""

from __future__ import annotations

from typing import Any

import uuid
from langgraph.graph import END, START, StateGraph

from server.execution.config import get_checkpointer
from server.execution.dispatcher import advance_domain, dispatch, route_after_validation
from server.execution.nodes.executor import executor_node
from server.execution.nodes.formatter import formatter_node
from server.execution.nodes.router import router_node
from server.execution.nodes.validator import validator_node
from server.execution.state import PipelineState

# Conditional-edge path map for the validator output.
_VALIDATOR_PATHS: dict[str, str] = {
    "retry": "executor",  # same domain, more execution attempts
    "reroute": "router",  # whole-pipeline retry (fresh classification + execution)
    "escalate": "formatter",  # all retries exhausted -> honest failure
    "next_domain": "advance_domain",  # pop next queued domain
    "done": "formatter",  # all domains handled
}

# Module-level singleton: compiled graph is created once per process so the
# same checkpointer instance is shared across all turns (thread_id is the
# conversation key inside the checkpointer).
_COMPILED_APP = None


def _add_edges(graph: StateGraph) -> None:
    graph.add_edge(START, "router")
    graph.add_edge("router", "dispatcher")
    graph.add_edge("dispatcher", "executor")
    graph.add_edge("executor", "validator")
    graph.add_conditional_edges("validator", route_after_validation, _VALIDATOR_PATHS)
    graph.add_edge("advance_domain", "executor")
    graph.add_edge("formatter", END)


def build_graph(checkpointer: Any | None = None):
    """Return the compiled execution graph.

    First call creates and caches the compiled graph with the default
    checkpointer. Subsequent calls return the cached instance so the same
    checkpointer is shared across all turns (thread_id == conversation id).

    Pass an explicit ``checkpointer`` to bypass the cache (used in tests).
    """
    global _COMPILED_APP

    def construct():
        graph = StateGraph(PipelineState)
        graph.add_node("router", router_node)
        graph.add_node("dispatcher", dispatch)
        graph.add_node("advance_domain", advance_domain)
        graph.add_node("executor", executor_node)
        graph.add_node("validator", validator_node)
        graph.add_node("formatter", formatter_node)
        _add_edges(graph)
        return graph

    if checkpointer is not None:
        return construct().compile(checkpointer=checkpointer)

    if _COMPILED_APP is None:
        _COMPILED_APP = construct().compile(checkpointer=get_checkpointer())

    return _COMPILED_APP


# Convenience sync entrypoint for scripts/tests.
def run_turn(
    app,
    *,
    router_output: dict[str, Any],
    thread_id: str,
    intent: str = "",
    user_id: str = "system",
    org_id: str = "activiity",
    conversation_id: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    query: str | None = None,
    history: str | None = None,
) -> dict[str, Any]:
    """Invoke one turn against a compiled graph with a stable thread_id.

    Returns the post-run checkpointed state (contains final_response,
    session_memory, executor_log). Reuses the same thread_id across turns to
    recover prior-turn memory.

    ``router_output`` may be pre-classified (passed straight to the router node)
    or omitted while ``query`` is supplied so the router node classifies inside
    the graph. ``history`` feeds the classifier on reroute attempts.
    """
    conv_id = conversation_id or thread_id
    initial: dict[str, Any] = {
        "user_id": user_id,
        "org_id": org_id,
        "conversation_id": conv_id,
        # One id per user turn: stable across that turn's validation retries
        # (so non-idempotent actions can guard against double execution) but
        # different for the next turn (so asking again really sends again).
        # The graph thread_id is the CONVERSATION id and must not be used here.
        "turn_id": uuid.uuid4().hex[:16],
        "router_output": router_output or {},
        "intent": intent or (router_output or {}).get("intent", ""),
        "query": query or intent,
        "history": history,
    }
    if attachments:
        initial["attachments"] = attachments
    return app.invoke(
        initial,
        config={"configurable": {"thread_id": thread_id}},
    )