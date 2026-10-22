from __future__ import annotations

import os
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

import requests

from server.config import load_environment

load_environment()

ROUTING_LABELS: tuple[str, ...] = ("workflow", "rag", "general_question")
DEFAULT_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "prompt_router_fr.md"


LABEL_ALIASES = {
    "workflow": "workflow",
    "workflows": "workflow",
    "flow": "workflow",
    "automation": "workflow",
    "rag": "rag",
    "retrieval": "rag",
    "retrieve": "rag",
    "search": "rag",
    "knowledge": "rag",
    "document": "rag",
    "documents": "rag",
    "docs": "rag",
    "vector": "rag",
    "general": "general_question",
    "general_question": "general_question",
    "general-question": "general_question",
    "question": "general_question",
    "faq": "general_question",
    "chat": "general_question",
    "assistant": "general_question",
}

WORKFLOW_HINTS = (
    "task",
    "project",
    "todo",
    "deadline",
    "due",
    "remind",
    "calendar",
    "calendaring",
    "schedule",
    "appointment",
    "meeting",
    "crm",
    "client",
    "lead",
    "opportunity",
    "pipeline",
    "erp",
    "invoice",
    "payment",
    "quote",
    "delivery",
    "stock",
    "order",
    "conversations",
    "conversation",
    "chatwoot",
    "ticket",
    "support",
    
)

RAG_HINTS = (
    "management",
    "manager",
    "coaching",
    "motivation",
    "time management",
    "delegation",
    "delegate",
    "leadership",
    "communication",
    "stress",
    "confidence",
    "feedback",
    "productivity",
    "prioritize",
    "prioritization",
    "focus",
    "boundaries",
    "procrastination",
    "burnout",
    "discipline",
)


class CompletionClient(Protocol):
    def complete(self, prompt: str) -> str: ...


@dataclass(frozen=True)
class RoutePrediction:
    intent: str
    predicted_label: str
    raw_response: str


@dataclass(frozen=True)
class OpenRouterConfig:
    model: str
    api_key: str
    temperature: float = 0.0
    top_p: float = 1.0
    max_tokens: int = 64
    frequency_penalty: float = 0.0
    presence_penalty: float = 0.0
    seed: int | None = 42
    base_url: str = "https://openrouter.ai/api/v1"
    site_url: str | None = None
    app_name: str = "activity-ai-engine"
    json_mode: bool = True


ROUTING_SYSTEM_PROMPT = (
    "You are a strict routing classifier. Return a single JSON object with a label field. "
    "Valid labels are workflow, rag, and general_question. "
    "workflow means tasking, ERP, CRM, calendar, project, Chatwoot, invoice, delivery, lead, opportunity, or other operational action requests. "
    "rag means management, coaching, motivation, time management, delegation, leadership, communication, prioritization, focus, burnout, or advice-seeking requests. "
    "general_question means everything else. "
    "Do not include explanations or extra text."
)
ROUTING_SYSTEM_PROMPT_FR=(
    "Vous êtes un classificateur de routage strict. Retournez un seul objet JSON avec un champ label. "
    "Les labels valides sont workflow, rag et general_question. "
    "workflow signifie des demandes d'action opérationnelle telles que la gestion de tâches, ERP, CRM, calendrier, projet, Chatwoot, facture, livraison, lead, opportunité ou autres. "
    "rag signifie des demandes de gestion, coaching, motivation, gestion du temps, délégation, leadership, communication, priorisation, concentration, burnout ou recherche de conseils. "
    "general_question signifie tout le reste. "
    "N'incluez pas d'explications ou de texte supplémentaire."
)



class OpenRouterCompletionClient:
    def __init__(self, config: OpenRouterConfig, prompt_template: str | None = None) -> None:
        self.config = config
        self.prompt_template = prompt_template

    def complete(self, prompt: str) -> str:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": ROUTING_SYSTEM_PROMPT_FR},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
            "max_tokens": self.config.max_tokens,
            "frequency_penalty": self.config.frequency_penalty,
            "presence_penalty": self.config.presence_penalty,
        }
        if self.config.seed is not None:
            payload["seed"] = self.config.seed
        if self.config.json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": self.config.site_url or "http://localhost",
            "X-Title": self.config.app_name,
        }
        last_error = None
        max_attempts = 10
        for attempt in range(1, max_attempts + 1):
            try:
                response = requests.post(
                    f"{self.config.base_url.rstrip('/')}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=60,
                )
                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 15))
                    if attempt < max_attempts:
                        time.sleep(retry_after)
                        continue
                response.raise_for_status()
                raw = str(response.json()["choices"][0]["message"]["content"]).strip()
                raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
                return raw
            except (requests.RequestException, KeyError, ValueError) as exc:
                last_error = exc
                if attempt < max_attempts:
                    time.sleep(min(2 ** attempt, 60))
                    continue
                if isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code == 401:
                    raise RuntimeError(
                        "OpenRouter rejected the API key loaded from .env. "
                        "Check that OPENROUTER_API_KEY is present, valid, and not expired."
                    ) from exc
                raise RuntimeError(
                    f"OpenRouter request failed after {attempt} attempts: {last_error}"
                ) from last_error


def _normalize_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def normalize_label(value: str | None) -> str | None:
    if value is None:
        return None

    token = _normalize_token(str(value))
    if not token:
        return None

    if token in ROUTING_LABELS:
        return token

    return LABEL_ALIASES.get(token)


def _extract_text_from_json(raw_response: str) -> str | None:
    try:
        parsed = json.loads(raw_response)
    except json.JSONDecodeError:
        return None

    if isinstance(parsed, str):
        return parsed

    if isinstance(parsed, dict):
        for key in ("label", "prediction", "route", "class", "intent_class", "category"):
            value = parsed.get(key)
            if value is not None:
                return str(value)

    return None


def extract_label_from_response(raw_response: str) -> str:
    text_from_json = _extract_text_from_json(raw_response)
    if text_from_json is not None:
        normalized = normalize_label(text_from_json)
        if normalized is not None:
            return normalized

    normalized = normalize_label(raw_response)
    if normalized is not None:
        return normalized

    lowered = raw_response.lower()
    ordered_patterns = (
        (r"\bgeneral[_\s-]?question\b", "general_question"),
        (r"\bworkflow\b", "workflow"),
        (r"\brag\b", "rag"),
        (r"\bretrieval\b", "rag"),
        (r"\bknowledge[_\s-]?base\b", "rag"),
    )
    for pattern, label in ordered_patterns:
        if re.search(pattern, lowered):
            return label

    return "invalid"


def load_prompt_template(prompt_path: Path | str | None = None) -> str:
    path = Path(prompt_path) if prompt_path is not None else DEFAULT_PROMPT_PATH
    if not path.exists():
        raise FileNotFoundError(f"Router prompt file not found: {path}")
    return path.read_text(encoding="utf-8")


def render_router_prompt(intent: str, prompt_template: str) -> str:
    rendered = prompt_template.replace("__LABELS__", ", ".join(ROUTING_LABELS))
    return rendered.replace("__INTENT__", intent)


def build_openrouter_client(
    *,
    model: str,
    api_key: str | None = None,
    temperature: float = 0.0,
    top_p: float = 1.0,
    max_tokens: int = 64,
    frequency_penalty: float = 0.0,
    presence_penalty: float = 0.0,
    seed: int | None = 42,
    base_url: str = "https://openrouter.ai/api/v1",
    site_url: str | None = None,
    app_name: str = "activity-ai-engine",
    json_mode: bool = True,
) -> OpenRouterCompletionClient:
    resolved_api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if not resolved_api_key:
        raise ValueError("OPENROUTER_API_KEY is required to use an OpenRouter client")

    return OpenRouterCompletionClient(
        OpenRouterConfig(
            model=model,
            api_key=resolved_api_key,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
            frequency_penalty=frequency_penalty,
            presence_penalty=presence_penalty,
            seed=seed,
            base_url=base_url,
            site_url=site_url,
            app_name=app_name,
            json_mode=json_mode,
        )
    )


class RuleBasedRouterClient:
    def complete(self, prompt: str) -> str:
        match = re.search(r"Intent:\s*(.+)", prompt, flags=re.IGNORECASE | re.DOTALL)
        intent = match.group(1).strip() if match else prompt
        lowered = intent.lower()

        if any(keyword in lowered for keyword in WORKFLOW_HINTS):
            return "workflow"
        if any(keyword in lowered for keyword in RAG_HINTS):
            return "rag"
        return "general_question"


def coerce_completion_client(model_client: Any | None) -> Callable[[str], str]:
    if model_client is None:
        return RuleBasedRouterClient().complete

    if callable(model_client):
        return model_client

    for method_name in ("complete", "generate", "predict", "invoke"):
        method = getattr(model_client, method_name, None)
        if callable(method):
            return method

    raise TypeError(
        "Unsupported model client. Pass a callable or an object exposing complete(), generate(), predict(), or invoke()."
    )


class RouterAgent:
    def __init__(self, model_client: Any | None = None, prompt_path: Path | str | None = None) -> None:
        self._completion = coerce_completion_client(model_client)
        self.prompt_path = Path(prompt_path) if prompt_path is not None else DEFAULT_PROMPT_PATH
        self.prompt_template = load_prompt_template(self.prompt_path)

    def build_prompt(self, intent: str) -> str:
        return render_router_prompt(intent=intent, prompt_template=self.prompt_template)

    def predict(self, intent: str) -> RoutePrediction:
        prompt = self.build_prompt(intent)
        raw_response = str(self._completion(prompt)).strip()
        predicted_label = extract_label_from_response(raw_response)
        return RoutePrediction(intent=intent, predicted_label=predicted_label, raw_response=raw_response)


def route_intent(intent: str, model_client: Any | None = None, prompt_path: Path | str | None = None) -> RoutePrediction:
    return RouterAgent(model_client=model_client, prompt_path=prompt_path).predict(intent)
