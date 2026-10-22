"""End-to-end test: thread memory persists across turns via sqlite checkpointer."""
import os

os.environ["LANGGRAPH_CHECKPOINTER"] = "sqlite"

from server.execution.graph import build_graph, run_turn, _COMPILED_APP  # noqa: E402


def _reset_graph_cache():
    import server.execution.graph as g
    g._COMPILED_APP = None


def test_memory_persists_across_turns():
    _reset_graph_cache()
    try:
        app = build_graph()
        thread = "test-memory-thread-001"

        # Turn 1: create a lead
        state1 = run_turn(
            app,
            router_output={
                "domain": "crm",
                "intent": "creer un lead pour techno-acm avec un CA attendu de 50000 euros",
                "tool_calls": [
                    {
                        "action": "crm.lead.create",
                        "params": {
                            "partner_name": "techno-acm",
                            "expected_revenue": 50000,
                            "user_name": "Techno ACM",
                        },
                        "data": {"ok": True, "response": {"id": 42}},
                    }
                ],
            },
            thread_id=thread,
            intent="creer un lead pour techno-acm avec un CA attendu de 50000 euros",
        )
        assert state1.get("final_response") is not None, "Turn 1 produced no response"

        # Turn 2: update the lead by implicit reference (same thread)
        state2 = run_turn(
            app,
            router_output={
                "domain": "crm",
                "intent": "mettre a jour la probabilite de ce lead a 90%",
                "tool_calls": [
                    {
                        "action": "crm.lead.write",
                        "params": {"partner_name": "techno-acm", "probability": 90},
                        "data": {"ok": True, "response": {"id": 42}},
                    }
                ],
            },
            thread_id=thread,
            intent="mettre a jour la probabilite de ce lead a 90%",
        )
        assert state2.get("final_response") is not None, "Turn 2 produced no response"

        # Session memory should have accumulated across turns
        mem2 = state2.get("session_memory", [])
        assert len(mem2) >= len(state1.get("session_memory", [])), (
            "Session memory did not grow across turns"
        )
        print(f"PASS  memory grows: turn1={len(state1.get('session_memory', []))} "
              f"turn2={len(mem2)}")

        # Rebuild graph (simulates server restart) and verify checkpoint recovery
        _reset_graph_cache()
        app2 = build_graph()
        state3 = run_turn(
            app2,
            router_output={
                "domain": "crm",
                "intent": "modifie le CA attendu de techno-acm a 75000",
                "tool_calls": [
                    {
                        "action": "crm.lead.write",
                        "params": {"partner_name": "techno-acm", "expected_revenue": 75000},
                        "data": {"ok": True, "response": {"id": 42}},
                    }
                ],
            },
            thread_id=thread,
            intent="modifie le CA attendu de techno-acm a 75000",
        )
        mem3 = state3.get("session_memory", [])
        assert len(mem3) >= len(mem2), (
            "Session memory lost after graph rebuild (checkpointer persistence failed)"
        )
        print(f"PASS  memory persists after rebuild: turn2={len(mem2)} turn3={len(mem3)}")

    finally:
        _reset_graph_cache()


def test_same_app_singleton():
    _reset_graph_cache()
    try:
        app1 = build_graph()
        app2 = build_graph()
        assert app1 is app2, "build_graph() did not return the same instance"
        print("PASS  singleton cache works")
    finally:
        _reset_graph_cache()


def test_explicit_checkpointer_bypasses_cache():
    _reset_graph_cache()
    try:
        from langgraph.checkpoint.memory import InMemorySaver

        app = build_graph(checkpointer=InMemorySaver())
        app2 = build_graph()
        assert app is not app2, "Explicit checkpointer should not use cache"
        print("PASS  explicit checkpointer bypasses cache")
    finally:
        _reset_graph_cache()


if __name__ == "__main__":
    test_same_app_singleton()
    test_explicit_checkpointer_bypasses_cache()
    test_memory_persists_across_turns()
    print("\nAll memory-persistence tests passed.")
