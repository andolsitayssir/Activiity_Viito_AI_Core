"""Shared pytest fixtures.

Keeps graph runs deterministic: the semantic (LLM) judge must NOT fire during
tests, otherwise every graph test would hit OpenRouter with the key from .env.
Structural validation (rule-based) stays fully active.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _disable_semantic_judge(monkeypatch):
    monkeypatch.setenv("VIITO_ENABLE_SEMANTIC_VALIDATION", "0")
    yield