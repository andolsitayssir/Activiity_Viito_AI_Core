"""Per-domain backend executors (direct API calls, no MCP)."""

__all__ = [
    "DomainExecutor",
    "ExecutionContext",
    "ExecutorError",
    "action_result",
    "deferred_action",
    "executor_result",
    "get_registry",
]

from server.execution.executors.base import (
    DomainExecutor,
    ExecutionContext,
    ExecutorError,
    action_result,
    deferred_action,
    executor_result,
)
from server.execution.executors.registry import DomainExecutorRegistry, get_registry
from server.execution.executors.odoo import OdooExecutor
from server.execution.executors.chatwoot import ChatwootExecutor
from server.execution.executors.stalwart import StalwartExecutor
from server.execution.executors.onlyoffice import OnlyOfficeExecutor
from server.execution.executors.vikunja import VikunjaExecutor
from server.execution.executors.rag import RagExecutor