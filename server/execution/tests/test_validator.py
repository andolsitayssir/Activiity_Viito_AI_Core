"""Validator tests: structural + semantic stages, and the routing table.

The semantic judge is injected (no live LLM). LangGraph state constants mirror
the dispatcher contract; the full-graph reroute loop is covered separately in
test_reroute_graph.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root

from server.execution.config import MAX_RETRIES, MAX_ROUTER_RETRIES
from server.execution.dispatcher import route_after_validation
from server.execution.executors.base import action_result, deferred_action
from server.execution.nodes.validator import (
    _compact_for_judge,
    _delivered_turn_verdict,
    _structural_pass,
    validator_node_factory,
)


def _executor_result(status="success", results=None, error=None):
    return {
        "domain": "test",
        "status": status,
        "results": results or [],
        "error": error,
        "attempts": 1,
    }


def _sent_result(with_id=True, ok=True):
    response = {"id": "msg-1", "thread_id": "msg-1", "attachment_names": ["rapport.pdf"]}
    if not with_id:
        response = {"attachment_names": ["rapport.pdf"]}
    return _executor_result(
        results=[
            action_result(
                "email.email_submission.set",
                {
                    "recipients": ["a@b.com"],
                    "subject": "final report",
                    "content": "vous trouverez ci joint le rapprot final",
                    "attachment_names": "rapport.pdf",
                },
                tool_calls=["gmail:email.email_submission.set"],
                data={"ok": ok, "response": response} if ok else {"ok": False, "error": "boom"},
            )
        ]
    )


def test_delivered_turn_passes_without_judge():
    """A send that returned a message id has been delivered: never escalate."""
    verdict = _delivered_turn_verdict(_sent_result())
    assert verdict is not None and verdict[0] is True
    assert "confirmé" in verdict[1]


def test_delivered_turn_not_claimed_without_confirmation():
    assert _delivered_turn_verdict(_sent_result(with_id=False)) is None


def test_delivered_turn_defers_to_judge_on_real_error():
    assert _delivered_turn_verdict(_sent_result(ok=False)) is None


def test_delivered_turn_never_escalates_over_user_typography():
    """The judge's spelling complaint must not turn a sent mail into a failure."""
    result = _sent_result()
    result["results"][0]["data"]["duplicate_suppressed"] = True

    def judge(query, res):  # pragma: no cover - must never run
        raise AssertionError("the judge must not be consulted for a delivered send")

    node = validator_node_factory(judge=judge)
    state = node(_state(query="envoie le rapport final", executor_result=result))
    # A delivered send is settled deterministically: no retry, no reroute.
    assert state["validation_result"]["passed"] is True
    assert "_reroute_requested" not in state
    assert "retry_count" not in state


def _state(**overrides):
    base = {
        "current_domain": "test",
        "retry_count": 0,
        "error_history": [],
        "router_retry_count": 0,
        "router_error_history": [],
        "query": "Trouve le contact Acme",
        "executor_result": _executor_result(),
        "domain_queue": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Structural stage (rule-based)
# ---------------------------------------------------------------------------
def test_structural_pass_on_success():
    assert _structural_pass(_executor_result()) == (True, None)


def test_structural_fail_on_error_status():
    ok, reason = _structural_pass(_executor_result(status="error", error="boom"))
    assert ok is False
    assert "status" in reason


def test_structural_fail_on_unsupported_alone_when_nothing_ran():
    result = _executor_result(results=[deferred_action("x.find", reason="not wired")])
    ok, reason = _structural_pass(result)
    assert ok is False
    assert "not wired" in reason


def test_structural_pass_when_only_some_actions_are_unsupported():
    """Gmail has no mailbox endpoint: "envoyer ce fichier" also planned
    email.mailbox.create, which comes back deferred. The mail went out, but the
    turn was retried 3x and then reported as failed. An unsupported action is a
    missing capability, not an error."""
    sent = action_result(
        "email.email_submission.set",
        data={"ok": True, "response": {"id": "1a0f6b28", "thread_id": "1a0f6b28"}},
    )
    result = _executor_result(
        results=[sent, deferred_action("email.mailbox.create", reason="executor not wired for this backend yet")]
    )
    ok, reason = _structural_pass(result)
    assert ok is True, reason


def test_structural_fail_on_action_ok_false():
    data = {"ok": False, "error": "odoo res.partner.create: Contacts require a name"}
    result = _executor_result(results=[action_result("res.partner.create", data=data)])
    ok, reason = _structural_pass(result)
    assert ok is False
    assert "res.partner.create" in reason
    assert "Contacts require a name" in reason


def test_action_ok_false_uses_structural_loop_not_reroute():
    """A per-action execution failure (ok=false) is NOT a routing problem: it
    must retry the SAME domain locally, not re-run the whole multi-domain
    pipeline (which would duplicate side-effects like re-sent emails)."""
    v = validator_node_factory(judge=lambda q, r: (True, "irrelevant"))
    patch = v(
        _state(
            executor_result=_executor_result(
                results=[action_result("res.partner.create", data={"ok": False, "error": "Contacts require a name"})]
            )
        )
    )
    vr = patch["validation_result"]
    assert vr["passed"] is False
    assert vr["stage"] == "structural"
    assert patch["retry_count"] == 1
    assert "_reroute_requested" not in patch


def test_structural_pass_skips_null():
    assert _structural_pass(None) == (False, "executor_result missing or not a dict")


def test_compact_for_judge_includes_requested_params():
    """The judge must see what each action asked for, otherwise it cannot link
    e.g. `unlink asked={"partner": "newayit"}` to the returned ids [105, 104]
    and it will wrongly report 'no confirmation'."""
    result = _executor_result(
        results=[
            action_result(
                "odoo:res.partner.unlink",
                params={"partner": "newayit"},
                data={"ok": True, "response": {"ids": [105, 104], "ok": True}},
            ),
        ]
    )
    compact = _compact_for_judge(result)
    assert "asked" in compact
    assert "newayit" in compact
    assert "105" in compact


# ---------------------------------------------------------------------------
# Validator stage selection (fake judge injected)
# ---------------------------------------------------------------------------
def test_semantic_match_passes():
    v = validator_node_factory(judge=lambda q, r: (True, "found it"))
    patch = v(_state())
    assert patch["validation_result"]["passed"] is True
    assert patch["validation_result"]["stage"] == "structural"
    assert "router_retry_count" not in patch and "retry_count" not in patch


def test_semantic_mismatch_fails_and_requests_reroute():
    v = validator_node_factory(judge=lambda q, r: (False, "nothing found"))
    patch = v(_state())
    vr = patch["validation_result"]
    assert vr["passed"] is False
    assert vr["stage"] == "semantic"
    assert vr["reason"] == "nothing found"
    assert patch["router_retry_count"] == 1
    assert patch["_reroute_requested"] is True
    assert patch["router_error_history"] == ["nothing found"]


def test_execution_failure_uses_structural_loop_not_reroute():
    v = validator_node_factory(judge=lambda q, r: (False, "irrelevant"))
    patch = v(_state(executor_result=_executor_result(status="error")))
    vr = patch["validation_result"]
    assert vr["passed"] is False
    assert vr["stage"] == "structural"
    assert patch["retry_count"] == 1
    assert "_reroute_requested" not in patch


# ---------------------------------------------------------------------------
# Multi-domain: semantic judge is deferred until the whole queue has executed
# ---------------------------------------------------------------------------
def test_semantic_judge_deferred_while_domains_pending():
    """An intermediate domain must NOT be judged against the full multi-domain
    query: structural pass + advance only, no LLM call."""
    calls = {"n": 0}

    def judge(q, r):
        calls["n"] += 1
        return (False, "would false-mismatch")

    v = validator_node_factory(judge=judge)
    patch = v(_state(domain_queue=["crm", "email_action"]))
    vr = patch["validation_result"]
    assert vr["passed"] is True
    assert vr["stage"] == "structural"
    assert vr["reason"] == "deferred until all queued domains executed"
    assert calls["n"] == 0
    assert "router_retry_count" not in patch and "retry_count" not in patch


def test_final_domain_judges_aggregate_of_all_domains():
    """On the last domain, the judge receives ALL this-turn domain results
    (from executor_log), not just the current domain's result."""
    seen = {}

    def judge(q, r):
        seen["result"] = r
        return (True, "ok")

    log = [
        {**_executor_result(results=[action_result("res.partner.create", data={"id": 104})]), "domain": "contacts"},
        {**_executor_result(results=[action_result("email.email_submission.set", data={"id": 1})]), "domain": "email_action"},
    ]
    v = validator_node_factory(judge=judge)
    patch = v(
        _state(
            domain_queue=[],
            executor_result=log[-1],
            executor_log=log,
            _log_start=0,
        )
    )
    assert patch["validation_result"]["passed"] is True
    actions = [a["action"] for a in seen["result"]["results"]]
    assert actions == ["res.partner.create", "email.email_submission.set"]


def test_final_domain_aggregate_keeps_latest_attempt_per_domain():
    """A transient error recovered by a retry must not taint the judge's input:
    only the latest attempt per domain is aggregated."""
    seen = {}

    def judge(q, r):
        seen["result"] = r
        return (True, "ok")

    log = [
        _executor_result(status="error", error="backend unreachable"),
        _executor_result(results=[action_result("res.partner.create", data={"id": 104})]),
    ]
    v = validator_node_factory(judge=judge)
    v(
        _state(
            domain_queue=[],
            executor_result=log[-1],
            executor_log=log,
            _log_start=0,
        )
    )
    assert seen["result"]["status"] == "success"
    assert seen["result"]["error"] is None


# ---------------------------------------------------------------------------
# route_after_validation decision table
# ---------------------------------------------------------------------------
def test_route_structural_retry_within_budget():
    s = _state(retry_count=0)
    s["validation_result"] = {"passed": False, "stage": "structural"}
    assert route_after_validation(s) == "retry"


def test_route_structural_retries_up_to_max_cycles():
    s = _state(retry_count=MAX_RETRIES)
    s["validation_result"] = {"passed": False, "stage": "structural"}
    assert route_after_validation(s) == "retry"


def test_route_structural_escalates_exhausted():
    s = _state(retry_count=MAX_RETRIES + 1)
    s["validation_result"] = {"passed": False, "stage": "structural"}
    assert route_after_validation(s) == "escalate"


def test_route_semantic_reroute_within_budget():
    s = _state(router_retry_count=0)
    s["validation_result"] = {"passed": False, "stage": "semantic"}
    assert route_after_validation(s) == "reroute"


def test_route_semantic_reroutes_up_to_max_cycles():
    s = _state(router_retry_count=MAX_ROUTER_RETRIES)
    s["validation_result"] = {"passed": False, "stage": "semantic"}
    assert route_after_validation(s) == "reroute"


def test_route_semantic_escalates_exhausted():
    s = _state(router_retry_count=MAX_ROUTER_RETRIES + 1)
    s["validation_result"] = {"passed": False, "stage": "semantic"}
    assert route_after_validation(s) == "escalate"


def test_route_passed_empty_queue_done():
    s = _state()
    s["validation_result"] = {"passed": True, "stage": "structural"}
    assert route_after_validation(s) == "done"


def test_route_passed_nonempty_queue_next_domain():
    s = _state(domain_queue=["sales"])
    s["validation_result"] = {"passed": True, "stage": "structural"}
    assert route_after_validation(s) == "next_domain"


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