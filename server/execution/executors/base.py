"""Executor contracts: exceptions, result builders, and the DomainExecutor ABC.

A :class:`DomainExecutor` encapsulates ONE backend (Chatwoot, Odoo, Stalwart,
...). It sees ONLY its domain's actions+params (domain-scoped), plus session
memory and retry context, and returns an ExecutorResult-layout dict that the
pipeline node hands to the validator.

Never fabricates success: an unresolved target / failed call raises
:class:`ExecutorError` with a human-readable reason, and partial hits are
reported per-action so the validator can judge them.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Result-layout builders (mirror ExecutorResult in state.py, as plain dicts).
# ---------------------------------------------------------------------------
def action_result(
    action: str,
    params: dict[str, Any] | None = None,
    *,
    tool_calls: list[str] | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One executed (or attempted) action record for ``results``."""
    return {
        "action": action,
        "params": params or {},
        "tool_calls": tool_calls or [],
        "data": data or {},
    }


def executor_result(
    domain: str,
    results: list[dict[str, Any]],
    *,
    status: str = "success",
    error: str | None = None,
    attempts: int = 1,
) -> dict[str, Any]:
    """ExecutorResult-layout dict. status: success | error | escalated."""
    return {
        "domain": domain,
        "status": status,
        "results": results,
        "error": error,
        "attempts": attempts,
    }


def deferred_action(
    action: str,
    params: dict[str, Any] | None = None,
    reason: str = "executor not wired for this backend yet",
) -> dict[str, Any]:
    """Honest no-op: the action is deferred, not silently 'completed'."""
    return action_result(
        action,
        params,
        tool_calls=[],
        data={"deferred": True, "reason": reason},
    )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class ExecutorError(Exception):
    """A hard, non-retryable executor failure (bad config, missing target...)."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


# ---------------------------------------------------------------------------
# Context handed to an executor for one turn/attempt
# ---------------------------------------------------------------------------
@dataclass
class ExecutionContext:
    domain: str
    actions: dict[str, dict[str, Any]] = field(default_factory=dict)
    session_memory: dict[str, Any] = field(default_factory=dict)
    retry_count: int = 0
    error_history: list[str] = field(default_factory=list)
    user_id: str = "system"
    org_id: str = "activiity"
    conversation_id: str = ""
    intent: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    #: Stable id of the current user turn (same value for every retry of that
    #: turn). Non-idempotent executors use it to avoid firing twice.
    turn_id: str = ""
    #: ExecutorResult-layout records from all domains executed so far in the
    #: current turn (append-only). Lets a later domain (e.g. onlyoffice) reuse
    #: text produced by an earlier domain (e.g. general.summarize/resume) to
    #: inject it into a document instead of a placeholder.
    executor_log: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# DomainExecutor ABC
# ---------------------------------------------------------------------------
class DomainExecutor(abc.ABC):
    """Domain-scoped backend client. One instance per backend, registered once."""

    #: normalized domain name(s) this executor serves (primary first).
    domains: tuple[str, ...] = ()

    #: human-readable backend label, e.g. \"chatwoot\".
    backend: str = ""

    @abc.abstractmethod
    def execute(self, ctx: ExecutionContext) -> dict[str, Any]:
        """Run every action in ``ctx.actions`` for ``ctx.domain``.

        Returns an ExecutorResult-layout dict. Raises ExecutorError for a
        terminal failure of the whole domain attempt.
        """
        raise NotImplementedError