"""Router node — classification inside the LangGraph pipeline.

Integrates the 3-stage classification (domain -> action -> attributes) that the
webapp previously ran *before* invoking the graph. Moving it here lets the
validator route back to this node for a whole-pipeline retry when the results
do not match the user query (semantic mismatch).

The node:
  * On first entry  (START -> router)        : uses ``router_output`` if the
    caller pre-classified it (backward compatible with tests/callers); otherwise
    classifies ``query`` fresh.
  * On reroute       (validator --reroute--> router): ALWAYS re-classifies from
    ``query`` (a stale ``router_output`` would defeat the retry).

The classification itself is injected via ``_classify`` so the graph stays
unit-testable without live OpenRouter calls.
"""

from __future__ import annotations

import json
from typing import Any, Callable

# Default classifier: the production 3-stage pipeline used by the webapp.
def _default_classifier(query: str, history: str | None) -> dict[str, Any]:
    from server.webapp.api import _classify_and_envelope

    envelope = _classify_and_envelope(query, history=history)
    envelope["intent"] = query
    return envelope


def router_node_factory(classifier: Callable[[str, str | None], dict[str, Any]] | None = None) -> Callable:
    """Build the router node with an injectable classifier (tests pass a fake)."""
    classify = classifier or _default_classifier

    def router_node(state: dict[str, Any]) -> dict[str, Any]:
        reroute = bool(state.get("_reroute_requested"))
        query = state.get("query") or state.get("intent") or ""
        history = state.get("history")

        if reroute or not state.get("router_output"):
            if not query:
                raise RuntimeError("router node needs a query to classify")
            envelope = classify(query, history)
            envelope = {**envelope, "intent": query}
            print(f"[ROUTER] reroute={reroute} domains={envelope.get('domains')} actions={envelope.get('actions')}", flush=True)
        else:
            envelope = dict(state.get("router_output") or {})
            envelope.setdefault("intent", query)

        return {
            "router_output": envelope,
            "_reroute_requested": False,
        }

    return router_node


router_node = router_node_factory()