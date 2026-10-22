"""PipelineState for the Viito execution layer.

Graph-state contract: plain-dict ``TypedDict`` (checkpointer-safe in all
langgraph versions, serializable to SQLite/Redis without custom encoders).
Pydantic models below are boundary validators: they validate the plain dicts
at node edges but are never stored inside the checkpointed state.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Boundary validation models (never checkpointed; built from plain dicts)
# ---------------------------------------------------------------------------
class RouterOutput(BaseModel):
    """Terminal envelope emitted by ``AttributesClassifierAgent.to_terminal()``."""

    intent: str = ""
    domains: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    # domain -> action_id -> {"fields": {field: value}}
    fields: dict[str, dict[str, dict[str, dict[str, Any]]]] = Field(default_factory=dict)
    raw_response: str = ""


class ValidationResult(BaseModel):
    passed: bool = False
    stage: Literal["structural", "semantic"] = "structural"
    reason: str | None = None


class ExecutorResult(BaseModel):
    domain: str = ""
    status: Literal["success", "error", "escalated"] = "success"
    results: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    attempts: int = 1


class FinalResponse(BaseModel):
    """UI-facing response object. Never a fabricated success."""

    outcome: Literal["finished", "escalated", "failed"] = "finished"
    message: str = ""
    domains: list[str] = Field(default_factory=list)
    actions_attempted: list[dict[str, Any]] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    escalated: bool = False
    user_id: str = ""
    org_id: str = ""
    conversation_id: str = ""


class SessionMemory(BaseModel):
    """Last completed turn, kept in checkpointed state for follow-up resolution.

    Lets turn-2 follow-ups like "actually also email that quote" be resolved:
    the executor sees the prior turn's actions/params/results via this slot.
    """

    last_domains: list[str] = Field(default_factory=list)
    last_actions: list[str] = Field(default_factory=list)
    last_fields: dict[str, dict[str, dict[str, dict[str, Any]]]] = Field(default_factory=dict)
    last_executor_results: list[dict[str, Any]] = Field(default_factory=list)
    last_final_response: dict[str, Any] = Field(default_factory=dict)
    last_intent: str = ""


# ---------------------------------------------------------------------------
# Graph state contract (what flows through the StateGraph + checkpointer)
# ---------------------------------------------------------------------------
class PipelineState(TypedDict, total=False):
    # --- identity (always present; reserved for future long-term memory) ---
    user_id: str
    org_id: str
    conversation_id: str  # == LangGraph thread_id
    # One id per user turn (uuid), stable across that turn's validation retries.
    # NOT the graph thread id: non-idempotent executors (e.g. sending a mail)
    # use it so a retried turn never sends twice while a new turn always does.
    turn_id: str

    # --- turn input -------------------------------------------------------
    router_output: dict[str, Any]  # matches RouterOutput layout (plain dict)
    intent: str
    query: str  # raw user query fed to the router node for (re)classification
    history: str | None  # prior-turn context for anaphora resolution

    # --- whole-pipeline (routing) retry control ----------------------------
    # Counter + reason log for the outer retry loop. A failed semantic check
    # on the results triggers a full re-classification through the router node
    # (routing -> dispatcher -> executor -> validator) up to MAX_ROUTER_RETRIES.
    router_retry_count: int
    router_error_history: list[str]
    _reroute_requested: bool  # set by validator, consumed by router node

    # --- sequential multi-domain control ----------------------------------
    current_domain: str  # domain being executed right now
    domain_queue: list[str]  # remaining domains, in execution order

    # --- execution ---------------------------------------------------------
    executor_result: dict[str, Any] | None  # matches ExecutorResult layout
    validation_result: dict[str, Any] | None  # matches ValidationResult layout
    retry_count: int
    error_history: list[str]  # fed back into executor retry context
    final_response: dict[str, Any] | None  # matches FinalResponse layout
    outcome: str  # "finished" | "escalated" | "failed"

    # Append-only per-session audit of every executed action. Reducer appends;
    # never reset (a fresh turn finds its slice via _log_start). Doubles as the
    # session-scoped action history.
    executor_log: Annotated[list[dict[str, Any]], operator.add]

    # Bookkeeping indexes. Underscore-prefixed = internal, not part of the
    # public log/or memory contract.
    _log_start: int  # len(executor_log) at turn start; formatter slices [start:]

    # --- session memory (cross-turn) ---------------------------------------
    session_memory: dict[str, Any]  # matches SessionMemory layout

    # --- file attachments (passed from UI, consumed by executor) -----------
    attachments: list[dict[str, Any]]  # [{filename, content_type, content_b64}]