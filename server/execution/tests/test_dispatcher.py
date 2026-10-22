"""Step-1 dispatcher unit tests — pure logic"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root

from server.execution.config import DOMAIN_EXECUTOR_MAP, MAX_RETRIES
from server.execution.dispatcher import (
    advance_domain,
    build_domain_plan,
    dispatch,
    domains_from_router_output,
    executor_node_for,
    route_after_validation,
)


def make_router_output(domains, actions=None, fields=None):
    return {
        "intent": "test",
        "domains": domains,
        "actions": actions or [],
        "fields": fields or {d: {} for d in domains},
    }


# --- domains_from_router_output -------------------------------------------------
def test_empty_router_falls_back_to_general():
    assert domains_from_router_output(None) == ["general"]
    assert domains_from_router_output({}) == ["general"]
    assert domains_from_router_output({"domains": []}) == ["general"]


def test_domains_are_normalized_and_deduplicated():
    out = {"domains": ["Sales", " sales ", "EMAIL_ACTION", "sales"]}
    assert domains_from_router_output(out) == ["sales", "email_action"]


def test_domains_preserve_order():
    out = {"domains": ["crm", "sales", "email_action"]}
    assert domains_from_router_output(out) == ["crm", "sales", "email_action"]


# --- build_domain_plan -----------------------------------------------------------
def test_plan_single_domain_empty_queue():
    current, queue = build_domain_plan(["contacts"])
    assert current == "contacts"
    assert queue == []


def test_plan_multi_domain_sequential_order():
    current, queue = build_domain_plan(["crm", "sales", "email_action"])
    assert current == "crm"
    assert queue == ["sales", "email_action"]


def test_plan_empty_falls_back_to_general():
    current, queue = build_domain_plan([])
    assert current == "general"
    assert queue == []


# --- executor_node_for -----------------------------------------------------------
def test_domain_maps_to_registered_executor_node():
    for domain, node in DOMAIN_EXECUTOR_MAP.items():
        assert executor_node_for(domain) == node


def test_unknown_domain_maps_to_general_executor():
    assert executor_node_for("paris_hilton") == DOMAIN_EXECUTOR_MAP["general"]


# --- dispatch --------------------------------------------------------------------
def test_dispatch_initializes_turn_fields():
    state = {
        "router_output": make_router_output(["sales", "email_action"]),
        "executor_log": [],
    }
    patch = dispatch(state)
    assert patch["current_domain"] == "sales"
    assert patch["domain_queue"] == ["email_action"]
    assert patch["retry_count"] == 0
    assert patch["error_history"] == []
    assert patch["executor_result"] is None
    assert patch["validation_result"] is None
    assert patch["_log_start"] == 0


def test_dispatch_log_start_reflects_prior_turn_log():
    state = {
        "router_output": make_router_output(["general"]),
        "executor_log": [{"domain": "sales", "status": "success"}],
    }
    assert dispatch(state)["_log_start"] == 1


def test_dispatch_resets_stale_fields_from_previous_turn():
    # Simulate checkpointed turn-1 leftovers — dispatch must NOT leak them.
    state = {
        "router_output": make_router_output(["accounting"]),
        "executor_log": [],
        "current_domain": "sales",  # stale from turn 1
        "domain_queue": ["email_action"],  # stale
        "retry_count": 2,  # stale
        "error_history": ["old error"],
    }
    patch = dispatch(state)
    assert patch["current_domain"] == "accounting"
    assert patch["domain_queue"] == []
    assert patch["retry_count"] == 0
    assert patch["error_history"] == []


# --- advance_domain ----------------------------------------------------------------
def test_advance_pops_next_domain_and_resets_retries():
    state = {
        "current_domain": "sales",
        "domain_queue": ["email_action", "tasking"],
        "retry_count": 2,
        "error_history": ["x", "y"],
    }
    patch = advance_domain(state)
    assert patch["current_domain"] == "email_action"
    assert patch["domain_queue"] == ["tasking"]
    assert patch["retry_count"] == 0
    assert patch["error_history"] == []


def test_advance_empty_queue_returns_empty():
    assert advance_domain({"domain_queue": []}) == {"domain_queue": []}


# --- route_after_validation ---------------------------------------------------------
def test_route_passed_with_queue_goes_next_domain():
    state = {"validation_result": {"passed": True}, "domain_queue": ["sales"]}
    assert route_after_validation(state) == "next_domain"


def test_route_passed_last_domain_goes_done():
    state = {"validation_result": {"passed": True}, "domain_queue": []}
    assert route_after_validation(state) == "done"


def test_route_failed_within_budget_retries():
    state = {"validation_result": {"passed": False}, "retry_count": MAX_RETRIES - 1}
    assert route_after_validation(state) == "retry"


def test_route_failed_last_budget_cycle_still_retries():
    # MAX_RETRIES means 2 retry cycles after the initial attempt (3 total).
    state = {"validation_result": {"passed": False}, "retry_count": MAX_RETRIES}
    assert route_after_validation(state) == "retry"


def test_route_failed_exhausted_escalates():
    state = {"validation_result": {"passed": False}, "retry_count": MAX_RETRIES + 1}
    assert route_after_validation(state) == "escalate"


def test_route_failed_without_retry_count_starts_at_zero():
    state = {"validation_result": {"passed": False}}
    assert route_after_validation(state) == "retry"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL {name}: {exc}")
    print(f"\n{sum(1 for k, v in globals().items() if k.startswith('test_') and callable(v)) - failures}/{sum(1 for k, v in globals().items() if k.startswith('test_') and callable(v))} passed")
    sys.exit(1 if failures else 0)