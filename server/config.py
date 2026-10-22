from __future__ import annotations

import os
from importlib import import_module
from pathlib import Path

load_dotenv = import_module("dotenv").load_dotenv

_MODEL_DEFAULTS: dict[str, str] = {
	"CLASSIFIER_MODEL": "google/gemma-4-31b-it",
	"VALIDATOR_MODEL": "openai/gpt-4o-mini",
	"RAG_ROUTER_MODEL": "google/gemma-4-31b-it",
	"SYNTHESIZER_MODEL": "google/gemma-3-12b-it",
	"FORMATTER_MODEL": "google/gemma-3-12b-it",
	"SUMMARIZATION_MODEL": "qwen/qwen3-8b",
	"REDACTION_MODEL": "google/gemma-4-31b-it",
	"SPEECH_RECOGNITION_MODEL": "qwen/qwen3-asr-1.7b",
}

def model_defaults() -> dict[str, str]:
	"""Per-role LLM models, env-overridable (call AFTER ``load_environment``)."""
	return {role: os.environ.get(role, default) for role, default in _MODEL_DEFAULTS.items()}


def load_environment() -> None:
	root = Path(__file__).resolve().parents[1]
	load_dotenv(dotenv_path=root / ".env", override=False)