"""Integration test: the full-pipeline reroute loop through a compiled graph.

Proves that a semantic mismatch routes the validator BACK to the router node,
which re-classifies, and the graph re-runs dispatcher -> executor -> validator
(whole-pipeline retry, bounded by MAX_ROUTER_RETRIES).
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from server.execution.dispatcher import advance_domain, dispatch, route_after_validation
from server.execution.executors.base import action_result, executor_result as mk_result
from server.execution.graph import _VALIDATOR_PATHS
from server.execution.nodes.formatter import formatter_node
from server.execution.nodes.router import router_node_factory
from server.execution.nodes.validator import validator_node_factory
from server.execution.state import PipelineState


def _build_graph(router_fn, executor_fn, validator_fn):
    graph = StateGraph(PipelineState)
    graph.add_node("router", router_fn)
    graph.add_node("dispatcher", dispatch)
    graph.add_node("advance_domain", advance_domain)
    graph.add_node("executor", executor_fn)
    graph.add_node("validator", validator_fn)
    graph.add_node("formatter", formatter_node)
    graph.add_edge(START, "router")
    graph.add_edge("router", "dispatcher")
    graph.add_edge("dispatcher", "executor")
    graph.add_edge("executor", "validator")
    graph.add_conditional_edges("validator", route_after_validation, _VALIDATOR_PATHS)
    graph.add_edge("advance_domain", "executor")
    graph.add_edge("formatter", END)
    return graph.compile(checkpointer=InMemorySaver())


def test_semantic_mismatch_triggers_whole_pipeline_reroute():
    """Call 1 classifies the wrong domain (finder validator judges mismatch);
    call 2 classifies the right one. The graph must recover via the router."""
    suffix = uuid.uuid4().hex[:6]
    classifier_calls = []

    def fake_classifier(query, history):
        classifier_calls.append(query)
        if len(classifier_calls) == 1:
            return {
                "intent": query,
                "domains": ["crm"],
                "actions": ["crm.lead.search_read"],
                "fields": {"crm": {"crm.lead.search_read": {"fields": {}}}},
            }
        # Second classification fixes the routing: contacts, not crm.
        return {
            "intent": query,
            "domains": ["contacts"],
            "actions": ["res.partner.search_read"],
            "fields": {"contacts": {"res.partner.search_read": {"fields": {}}}},
        }

    def fake_executor(state):
        domain = state.get("current_domain") or "contacts"
        return {
            "executor_result": mk_result(
                domain,
                [action_result("search_read", data={"records": []})],
            ),
            "executor_log": [mk_result(domain, [action_result("search_read", data={"records": []})])],
        }

    calls = {"routed_domains": []}
    routed_domain = fake_executor  

    def fake_validator(state):
        domain = state.get("current_domain") or ""
        calls["routed_domains"].append(domain)
        if domain == "crm":
            return {
                "validation_result": {
                    "passed": False,
                    "stage": "semantic",
                    "reason": "results do not match the query",
                },
                "router_retry_count": 1,
                "router_error_history": ["results do not match the query"],
                "_reroute_requested": True,
            }
        return {"validation_result": {"passed": True, "stage": "structural", "reason": None}}

    router = router_node_factory(classifier=fake_classifier)
    app = _build_graph(router, fake_executor, fake_validator)

    state = app.invoke(
        {
            "query": "Trouve le contact Acme",
            "router_output": {},
            "intent": "Trouve le contact Acme",
            "user_id": "tester",
            "org_id": "activiity",
            "conversation_id": f"reroute-{suffix}",
        },
        config={"configurable": {"thread_id": f"reroute-{suffix}"}},
    )

    # Router was called twice: initial + reroute after mismatch.
    assert len(classifier_calls) == 2, f"expected 2 classifications, got {classifier_calls}"
    # The graph recovered by re-executing with the corrected routing.
    assert calls["routed_domains"] == ["crm", "contacts"], calls["routed_domains"]
    # Final accounting in the state.
    assert state.get("router_retry_count") == 1
    assert state["final_response"]["outcome"] == "finished"
    assert "contacts" in state["final_response"]["domains"]


def test_semantic_mismatch_exhausts_router_retries_then_escalates():
    """If the router keeps producing mismatching routes, the turn escalates
    honestly after MAX_ROUTER_RETRIES instead of looping forever."""
    suffix = uuid.uuid4().hex[:6]
    classifier_calls = {"count": 0}

    def always_wrong(query, history):
        classifier_calls["count"] += 1
        return {
            "intent": query,
            "domains": ["crm"],
            "actions": ["crm.lead.search_read"],
            "fields": {"crm": {"crm.lead.search_read": {"fields": {}}}},
        }

    def fake_executor(state):
        domain = state.get("current_domain") or "crm"
        rec = mk_result(domain, [action_result("search_read", data={"records": []})])
        return {"executor_result": rec, "executor_log": [rec]}

    # Real validator semantics: an empty result for a find-query is a mismatch.
    from server.execution.nodes.validator import _semantic_judge

    def fake_judge(query, result):
        return (False, "nothing found")

    validator = validator_node_factory(judge=fake_judge)
    router = router_node_factory(classifier=always_wrong)
    app = _build_graph(router, fake_executor, validator)

    state = app.invoke(
        {
            "query": "Trouve le contact Acme",
            "router_output": {},
            "intent": "Trouve le contact Acme",
            "user_id": "tester",
            "org_id": "activiity",
            "conversation_id": f"reroute-exhaust-{suffix}",
        },
        config={"configurable": {"thread_id": f"reroute-exhaust-{suffix}"}},
    )

    from server.execution.config import MAX_ROUTER_RETRIES

    expected = 1 + MAX_ROUTER_RETRIES  # initial + bounded retries
    assert classifier_calls["count"] == expected, classifier_calls["count"]
    # The count ends one past the budget: the final mismatch incremented it
    # before route_after_validation decided to escalate.
    assert state.get("router_retry_count") == MAX_ROUTER_RETRIES + 1
    assert state["final_response"]["outcome"] == "escalated"
    assert state["final_response"]["escalated"] is True


def test_execution_failure_retries_same_domain_then_succeeds():
    """A structural failure must retry the SAME domain (not reroute). When the
    retry succeeds, the turn finishes normally with no rerouting."""
    suffix = uuid.uuid4().hex[:6]
    executions = {"count": 0}

    def fake_classifier(query, history):
        return {
            "intent": query,
            "domains": ["crm"],
            "actions": ["crm.lead.create"],
            "fields": {"crm": {"crm.lead.create": {"fields": {"name": "Acme"}}}},
        }

    def fake_executor(state):
        executions["count"] += 1
        first = executions["count"] == 1
        rec = mk_result(
            "crm",
            [action_result("crm.lead.create")],
            status="error" if first else "success",
            error="backend unreachable" if first else None,
        )
        return {"executor_result": rec, "executor_log": [rec]}

    # Real validator: structural stage only (semantic disabled by conftest).
    router = router_node_factory(classifier=fake_classifier)
    validator = validator_node_factory(judge=lambda q, r: (True, "ok"))
    app = _build_graph(router, fake_executor, validator)

    state = app.invoke(
        {
            "query": "Crée le lead Acme",
            "router_output": {},
            "intent": "Crée le lead Acme",
            "user_id": "tester",
            "org_id": "activiity",
            "conversation_id": f"struct-retry-{suffix}",
        },
        config={"configurable": {"thread_id": f"struct-retry-{suffix}"}},
    )

    assert executions["count"] == 2  # initial failure + 1 retry
    # retry_count is reset by the formatter at turn end; the retry is proven
    # by the 2 executions above and the non-failed outcome. Reroute never fired.
    assert state["final_response"]["outcome"] == "finished"
    assert state["final_response"]["escalated"] is False


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS {name}")
            except Exception as exc:
                failures += 1
                print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
    total = sum(1 for k, v in globals().items() if k.startswith("test_") and callable(v))
    print(f"\n{total - failures}/{total} passed")
    sys.exit(1 if failures else 0)