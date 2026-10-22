"""Graph-skeleton smoke tests: full turn through stub nodes + 2-turn memory."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root

from server.execution.graph import build_graph, run_turn


def make_router_output(domains, actions, fields=None):
    if fields is None:
        fields = {d: {a: {"fields": {}} for a in actions} for d in domains}
    return {
        "intent": " ".join(actions),
        "domains": domains,
        "actions": actions,
        "fields": fields,
    }


def test_single_domain_turn_completes():
    import uuid
    app = build_graph()
    state = run_turn(
        app,
        thread_id=f"t-single-{uuid.uuid4().hex[:6]}",
        router_output=make_router_output(["contacts"], ["res.partner.search_read"]),
        intent="Cherche le contact Karim Belhaj",
    )
    final = state["final_response"]
    assert final is not None
    assert final["outcome"] == "finished"
    assert final["domains"] == ["contacts"]
    assert len(final["actions_attempted"]) == 1
    assert final["actions_attempted"][0]["results"][0]["action"] == "res.partner.search_read"
    # session memory must have been written
    assert state["session_memory"]["last_intent"].startswith("Cherche")


def test_multi_domain_turn_runs_sequentially_in_order():
    import uuid
    app = build_graph()
    suffix = uuid.uuid4().hex[:6]
    fields = {
        "crm": {"crm.lead.search_read": {"fields": {}}},
        "sales": {"sale.order.search_read": {"fields": {}}},
        "contacts": {"res.partner.search_read": {"fields": {}}},
    }
    state = run_turn(
        app,
        thread_id=f"t-multi-{suffix}",
        router_output=make_router_output(
            ["crm", "sales", "contacts"],
            ["crm.lead.search_read", "sale.order.search_read", "res.partner.search_read"],
            fields,
        ),
    )
    final = state["final_response"]
    assert final["domains"] == ["crm", "sales", "contacts"]
    attempted_domains = [r["domain"] for r in final["actions_attempted"]]
    assert attempted_domains == ["crm", "sales", "contacts"]  # order preserved


def test_two_turn_memory_recovery():
    import uuid
    app = build_graph()
    thread_id = f"t-two-turn-{uuid.uuid4().hex[:6]}"

    turn1 = run_turn(
        app,
        thread_id=thread_id,
        router_output=make_router_output(
            ["sales"], ["sale.order.create"], {"sales": {"sale.order.create": {"fields": {"partner": "TechNova", "amount": 8500}}}}
        ),
        intent="Cree le devis TechNova de 8500 euros",
    )
    assert turn1["final_response"]["outcome"] == "finished"

    # Turn 2 references "that quote" — must resolve from checkpointed state.
    turn2 = run_turn(
        app,
        thread_id=thread_id,
        router_output=make_router_output(
            ["email_action"],
            ["email.email_submission.set"],
            {"email_action": {"email.email_submission.set": {"fields": {"recipients": "TechNova"}}}},
        ),
        intent="Envoie ce devis par email a TechNova",
    )
    memory = turn2["session_memory"]
    # Prior-turn history recovered: the formatter's session_memory includes turn 1.
    assert memory["last_intent"] == "Envoie ce devis par email a TechNova"  # this turn
    # executor_log is append-only across turns: must contain turn-1 action too.
    log = turn2["executor_log"]
    actions_in_log = []
    for record in log:
        actions_in_log.extend(r["action"] for r in record.get("results", []))
    assert "sale.order.create" in actions_in_log  # recovered from turn 1
    assert "email.email_submission.set" in actions_in_log


def test_short_term_transcript_memory_accumulates():
    """The rolling short-term transcript must accumulate across turns in a session.

    Uses the general/greeting path (static, no backend/LLM network) so the test
    is hermetic. Turn 1 produces an assistant answer; turn 2's session_memory
    must carry turn 1's user+assistant entries appended with turn 2's own.
    """
    import uuid
    app = build_graph()
    thread_id = f"t-transcript-{uuid.uuid4().hex[:6]}"

    turn1 = run_turn(
        app,
        thread_id=thread_id,
        router_output={"intent": "Bonjour", "domains": ["general"], "actions": [],
                       "fields": {"general": {}}},
        intent="Bonjour",
    )
    t1_transcript = (turn1.get("session_memory") or {}).get("transcript") or []
    assert t1_transcript[0]["role"] == "user"
    assert t1_transcript[0]["content"] == "Bonjour"
    assert t1_transcript[1]["role"] == "assistant"
    assert "Bonjour" in t1_transcript[1]["content"]  # greeting reply persisted

    turn2 = run_turn(
        app,
        thread_id=thread_id,
        router_output={"intent": "suis l option 3", "domains": ["general"], "actions": [],
                       "fields": {"general": {}}},
        intent="suis l option 3",
    )
    t2_transcript = (turn2.get("session_memory") or {}).get("transcript") or []
    contents = [m["content"] for m in t2_transcript]
    # Turn 1's messages are recovered (in order) before turn 2's.
    assert contents[0] == "Bonjour"
    assert "Bonjour" in contents[1]
    assert contents[2] == "suis l option 3"
    assert len(t2_transcript) >= 4  # 2 prior messages + 2 from this turn


def test_no_state_leak_between_unrelated_threads():
    import uuid

    app = build_graph()
    suffix = uuid.uuid4().hex[:6]
    a = run_turn(app, thread_id=f"t-a-{suffix}", router_output=make_router_output(["contacts"], ["res.partner.search_read"]))
    b = run_turn(app, thread_id=f"t-b-{suffix}", router_output=make_router_output(["general"], []))
    assert b["final_response"]["domains"] == ["general"]
    # Thread-b's log must NOT contain thread-a's records.
    b_actions = [
        r["action"]
        for rec in b["executor_log"]
        for r in rec.get("results", [])
    ]
    assert "res.partner.search_read" not in b_actions, (
        f"Thread-b leaked thread-a records: {b_actions}"
    )


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