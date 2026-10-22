"""Executor registry: domain name -> DomainExecutor instance.

The graph's single ``executor`` node consults this registry per ``current_domain``,
so backends stay domain-scoped (each executor owns only its backend's client).
Executors are registered lazily; unknown domains fall back to a deferred
executor so the pipeline still runs (honestly) while a backend is unwired.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from server.execution.dispatcher import normalize_domain
from server.execution.executors.base import DomainExecutor, deferred_action, executor_result
from server.execution.executors.chatwoot import ChatwootExecutor
from server.execution.executors.telegram import TelegramExecutor
from server.execution.executors.odoo import OdooExecutor
from server.execution.executors.stalwart import StalwartExecutor
from server.execution.executors.gmail import GmailExecutor
from server.execution.executors.onlyoffice import OnlyOfficeExecutor
from server.execution.executors.vikunja import VikunjaExecutor
from server.execution.executors.rag import RagExecutor
from server.execution.executors.general import GeneralExecutor
from server.execution.executors.file_manager import FileExecutor


def _env_toggle(name: str, default: bool = True) -> bool:
    return os.environ.get(name, "1" if default else "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


class DeferredExecutor(DomainExecutor):
    """Served for registered-but-unbuilt domains: honest deferred results."""

    domains: tuple[str, ...] = ()
    backend = "deferred"

    def __init__(self, domain: str, reason: str = "") -> None:
        self.domains = (domain,)
        self._reason = reason or f"executor for '{domain}' not implemented yet"

    def execute(self, ctx: Any) -> dict[str, Any]:
        results = [deferred_action(a, p, self._reason) for a, p in ctx.actions.items()]
        return executor_result(ctx.domain, results, status="success", error=None)


class DomainExecutorRegistry:
    def __init__(self) -> None:
        self._map: dict[str, DomainExecutor] = {}

    def register(self, executor: DomainExecutor) -> None:
        for domain in executor.domains:
            self._map[normalize_domain(domain)] = executor

    def executor_for(self, domain: str) -> DomainExecutor:
        executor = self._map.get(normalize_domain(domain))
        if executor is not None:
            return executor
        return DeferredExecutor(domain)

    def registered_domains(self) -> list[str]:
        return sorted(self._map)


_registry: DomainExecutorRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> DomainExecutorRegistry:
    """Build (once) and return the global registry.

    Registration is config-driven via env toggles so a backend can be enabled
    only once its container is up (default: enabled).
    """
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                registry = DomainExecutorRegistry()
                # Messaging domain default backend is Telegram. Chatwoot stays
                # registered (last-wins) only when Telegram is disabled, so an
                # existing Chatwoot install keeps working as a fallback.
                if _env_toggle("VIITO_ENABLE_TELEGRAM", True):
                    registry.register(TelegramExecutor())
                elif _env_toggle("VIITO_ENABLE_CHATWOOT", True):
                    registry.register(ChatwootExecutor())
                if _env_toggle("VIITO_ENABLE_ODOO", True):
                    registry.register(OdooExecutor())
                if _env_toggle("VIITO_ENABLE_GMAIL", True):
                    registry.register(GmailExecutor())
                elif _env_toggle("VIITO_ENABLE_STALWART", True):
                    registry.register(StalwartExecutor())
                if _env_toggle("VIITO_ENABLE_ONLYOFFICE", True):
                    registry.register(OnlyOfficeExecutor())
                if _env_toggle("VIITO_ENABLE_VIKUNJA", True):
                    registry.register(VikunjaExecutor())
                if _env_toggle("VIITO_ENABLE_FILES", True):
                    registry.register(FileExecutor())
                if _env_toggle("VIITO_ENABLE_RAG", True):
                    registry.register(RagExecutor())
                if _env_toggle("VIITO_ENABLE_GENERAL", True):
                    registry.register(GeneralExecutor())
                _registry = registry
    return _registry