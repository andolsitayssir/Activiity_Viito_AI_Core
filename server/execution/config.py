"""Execution-layer configuration.

Central place for: domain -> executor node mapping, retry budget, ambiguity-prone
domains (semantic validator), and the checkpointer factory. Swapping the
checkpointer from in-memory to sqlite/redis is a config change here, never a
graph rewrite (``build_graph`` only sees the result of ``get_checkpointer``).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SQLITE_PATH = str(_PROJECT_ROOT / "memory" / "execution_ckpt.sqlite")

# Normalized domain name -> executor node name (dispatcher mapping table).
DOMAIN_EXECUTOR_MAP: dict[str, str] = {
    "crm": "crm_executor",
    "accounting": "accounting_executor",
    "sales": "sales_executor",
    "hr": "hr_executor",
    "contacts": "contacts_executor",
    "tasking": "tasking_executor",
    "office": "office_executor",
    "messaging": "messaging_executor",
    "email_action": "email_action_executor",
    "file_management": "file_management_executor",
    "calendaring": "calendaring_executor",
    "knowledge_kb": "knowledge_kb_executor",
    "general": "general_executor",
}

# Ordered executor node names (all 13 domains).
ALL_EXECUTOR_NODES: tuple[str, ...] = tuple(dict.fromkeys(DOMAIN_EXECUTOR_MAP.values()))

# Sequential is the default: one domain's output is often another's input
# (e.g. crm -> sales -> email_action). Domains here MAY be executed in
# parallel later; reserved for the parallel-aware dispatcher, not used yet.
PARALLEL_SAFE_DOMAINS: frozenset[str] = frozenset({"knowledge_kb", "general"})

MAX_RETRIES: int = 2  # max retry cycles after the initial attempt (== 3 total attempts)

# Max retries of the WHOLE pipeline (routing + execution) after a semantic
# mismatch between the execution results and the user query. Re-runs the
# router node to attempt a fresh (hopefully correct) classification.
MAX_ROUTER_RETRIES: int = 2

# Domains where structural success can still miss the user's intent — a cheap
# LLM-judge (semantic) step runs on top. Kept config-driven so it can grow.
AMBIGUITY_PRONE_DOMAINS: frozenset[str] = frozenset(
    {"office", "knowledge_kb", "sales", "email_action"}
)

DEFAULT_DOMAIN: str = "general"
UNKNOWN_DOMAIN: str = "general"


def get_checkpointer() -> Any:
    """Return a LangGraph BaseCheckpointSaver.

    Selection is purely config-driven (env ``LANGGRAPH_CHECKPOINTER``), so the
    graph code never hardcodes a MemorySaver assumption. Default: sqlite backed
    by a persistent file in ``memory/execution_ckpt.sqlite``.
    """
    kind = os.environ.get("LANGGRAPH_CHECKPOINTER", "sqlite").strip().lower()
    if kind in ("memory", "inmemory", "in_memory"):
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver()
    if kind == "sqlite":
        path = os.environ.get("LANGGRAPH_SQLITE_PATH", _DEFAULT_SQLITE_PATH)
        from langgraph.checkpoint.sqlite import SqliteSaver

        import sqlite3

        conn = sqlite3.connect(str(path), check_same_thread=False)
        return SqliteSaver(conn=conn)
    raise ValueError(
        f"Unsupported LANGGRAPH_CHECKPOINTER={kind!r}. Use 'memory' or 'sqlite'."
    )