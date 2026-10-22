"""General-purpose domain executor (OpenRouter) with per-subtype models.

The ``general`` domain covers questions that trigger no system action: advice,
explanation, free-form writing, summarization, transcription. Subtype routing
picks the lightest/fastest model per task so answers stay good without paying
the routing model's latency:

* ``summarization``      -> qwen/qwen3-8b (fast, dense French summaries)
* ``redaction``/advice   -> google/gemma-4-31b-it (personal/professional help)
* ``speech_recognition`` -> mistralai/voxtral-small-24b-2507 (transcription)

Every action returns a natural-language ``answer`` that the formatter hands
straight to the UI (no client-side regeneration). With no API key, actions are
reported as deferred so the pipeline still completes honestly.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

import requests

from server.config import load_environment, model_defaults
from server.execution.executors.base import (
    DomainExecutor,
    ExecutionContext,
    action_result,
    deferred_action,
    executor_result,
)

load_environment()

_BASE_URL = "https://openrouter.ai/api/v1"
_ANSWER_TIMEOUT = 120

#: subtype -> (system_prompt, default model)
_SUBTYPES: dict[str, tuple[str, str]] = {
    "summarization": (
        "Tu es le module de résumé de Viito. Résume le texte fourni en français clair et "
        "structure : points essentiels, chiffres clés, liaisons logiques. Propose au besoin une "
        "synthèse en 3-6 lignes puis les points détaillés. Réponds uniquement en français.",
        "SUMMARIZATION_MODEL",
    ),
    "redaction": (
        "Tu es le module de rédaction et d'assistance professionnelle de Viito. Rédige en "
        "français professionnel, précis et naturel. Adapte le ton au contexte (courrier, email, "
        "proposition, conseil, poste) et demande-toi ce que l'utilisateur veut produire. Si la demande "
        "est un conseil, donne une réponse concise et actionnable. Quand on te demande de rédiger un "
        "paragraphe, un texte, un contenu ou un post à ajouter à un fichier ou à un document, rédige-le "
        "directement, sans poser de questions ni demander de précisions : produis le meilleur texte "
        "possible avec les éléments fournis. Réponds uniquement en français.",
        "REDACTION_MODEL",
    ),
    "speech_recognition": (
        "Tu es le module de reconnaissance vocale de Viito. Retranscris proprement le texte "
        "oral fourni : corrige les erreurs de transcription, restitue la ponctuation et "
        "normalise les abréviations, sans reformuler le fond. Ne réponds à la demande que si "
        "elle est explicite. Réponds uniquement en français.",
        "SPEECH_RECOGNITION_MODEL",
    ),
}

_REDACTION_ACTIONS = {"redaction"}
_SUMMARY_ACTIONS = {"resume", "summarize", "synthese"}
_SPEECH_ACTIONS = {"transcription", "transcrire", "speech_to_text", "reconnaissance vocale"}

#: A pure greeting (nothing being asked) -> Viito self-introduction, never the
#: sub-module system prompt.
_GREETING_ONLY_RE = re.compile(
    r"^\s*(bonjour(\s+(viito|tout le monde|à tous))?|bonsoir|salut|hello|hi|hey|coucou)\s*[.!?]?\s*$",
    re.IGNORECASE,
)

_GREETING_TEXT = (
    "Bonjour ! Je suis Viito, l'assistant intelligent d'Activiity Center. Comment puis-je vous aider ?\n\n"
    "Je peux vous accompagner notamment pour :\n"
    "• Rédiger ou reformuler des textes professionnels (e-mails, courriers, propositions, documents).\n"
    "• Résumer ou synthétiser des documents.\n"
    "• Répondre sur la vie en équipe et le bien-être au travail (conflits, ambiance, stress, motivation, "
    "communication, équilibre vie pro / vie perso).\n"
    "• Piloter vos outils Activiity (CRM, ventes et factures, tâches et projets, agenda, e-mails, fichiers, "
    "messagerie, contacts).\n"
    "• Chercher dans votre base de connaissances (RAG) et retranscrire des contenus audio.\n\n"
    "Dites-moi ce que vous voulez faire !"
)

_SUMMARY_RE = re.compile(r"\b(r[ée]sum[ée]|r[ée]sume|synth[ée]tise|syncopes|condense|r[ée]cap)", re.IGNORECASE)
_SPEECH_RE = re.compile(r"(transcri|retranscri|reconna[îi]ssance vocale|dict[ée]e|r[ée]transcription)", re.IGNORECASE)
_REDACTION_RE = re.compile(r"\b(r[ée]dig|r[ée]dige|r[ée]daction|lettre|email\b|courrier|note\b|proposition)", re.IGNORECASE)

#: Separators used by the Viito office-session flow to append the open file's
#: content to the user's instruction. The redaction must generate the requested
#: prose from the instruction alone — feeding the whole file dump into the
#: generator makes it rewrite the document instead of writing one paragraph.
_CONTEXT_SEP_RE = re.compile(
    r"\n\n---\n|\n\n\[Pi[eè]ce[s]? join(?:te[s]?|e)?:|\n\n\[Pi[eè]ces jointes:|\n\n\[Image disponible:"
)

#: Header of the WEBAPP-chat attachment block appended by _run_pipeline. Unlike
#: the office-session context ("Fichier actuel"), attached files in the chat are
#: reference material the model must READ to answer (summarize / analyze /
#: redact from), so this block is kept as the generation "source".
_CHAT_ATTACH_HEADER_RE = re.compile(
    r"\n\n---\nPi[eè]ce\(s\) attachée\(s\) au chat:[^\n]*\n?", re.IGNORECASE
)


def _strip_context(text: str) -> str:
    """Return the genuine user instruction, cutting any appended file-dump context."""
    if not text:
        return text
    match = _CONTEXT_SEP_RE.search(text)
    return (text[: match.start()] if match else text).strip()


def _attached_sources(text: str) -> str:
    """Extract the decoded chat-attachment text block (reference content).

    Kept separate from ``_strip_context``: chat attachments are informational
    and must reach the summarization/redaction LLM, whereas the office-session
    dump ("Fichier actuel (...)") stays stripped so a paragraph-redaction does
    not rewrite the whole document.
    """
    if not text:
        return ""
    match = _CHAT_ATTACH_HEADER_RE.search(text)
    if not match:
        return ""
    return text[match.end():].strip()


def _string_fields(actions: dict[str, Any]) -> dict[str, str]:
    """Router-provided attributes of the current turn (e.g. ``message``, ``tone``)."""
    picked: dict[str, str] = {}
    for payload in actions.values():
        fields = payload.get("fields") if isinstance(payload, dict) else None
        if isinstance(fields, dict):
            for key, value in fields.items():
                text = str(value).strip()
                if text:
                    picked[str(key)] = text
    return picked


def _subtype(actions: dict[str, Any], intent: str) -> str:
    names = " ".join(actions.keys())
    if _SUMMARY_ACTIONS & set(actions.keys()) or _SUMMARY_RE.search(names or "") or _SUMMARY_RE.search(intent):
        return "summarization"
    if _SPEECH_ACTIONS & set(actions.keys()) or _SPEECH_RE.search(intent):
        return "speech_recognition"
    if _REDACTION_ACTIONS & set(actions.keys()) or _REDACTION_RE.search(intent):
        return "redaction"
    return "redaction"


#: Fields that describe HOW to write, never the text to work on.
_STYLE_FIELDS = {"tone", "style", "language", "target_language", "length"}


def _source_text(actions: dict[str, Any], intent: str) -> str:
    """Best available source text: a long field value, else the raw query."""
    candidates: list[str] = []
    for params in actions.values():
        fields = params.get("fields") if isinstance(params, dict) else {}
        if isinstance(fields, dict):
            candidates.extend(
                str(v)
                for k, v in fields.items()
                if isinstance(v, str) and str(k).lower() not in _STYLE_FIELDS
            )
    text = max(candidates, key=len, default="").strip()
    if text:
        return text
    return _strip_context(intent)


def _transcript_block(ctx) -> str:
    """Format the short-term conversation transcript (prior turns) for the prompt.

    Lets a follow-up such as "redige la suite" or "suis l'option 3" resolve
    against content Viito produced in an earlier turn, because that content is
    included as prior context. Returns "" when there is no prior history.
    """
    transcript = (ctx.session_memory or {}).get("transcript") or []
    if not transcript:
        return ""
    lines = ["Conversation précédente (contexte à garder en mémoire) :"]
    for msg in transcript[-16:]:
        role = msg.get("role") or ""
        content = str(msg.get("content") or "").strip()
        if not content:
            continue
        label = "Utilisateur" if role == "user" else "Viito"
        lines.append(f"{label} : {content}\n")
    return "\n".join(lines).strip()


def _complete(model: str, api_key: str, system: str, user: str) -> str:
    last_error: Exception | None = None
    for attempt in (1, 2):
        try:
            resp = requests.post(
                f"{_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "X-Title": "activity-ai-engine",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.4,
                    "max_tokens": 1024,
                },
                timeout=_ANSWER_TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json()
            try:
                return payload["choices"][0]["message"]["content"].strip()
            except (KeyError, IndexError, TypeError) as exc:
                raise RuntimeError(f"malformed OpenRouter response: {payload}") from exc
        except Exception as exc:  # noqa: BLE001 - retried, then re-raised
            last_error = exc
            if attempt == 1:
                time.sleep(1.5)
    raise last_error


class GeneralExecutor(DomainExecutor):
    domains: tuple[str, ...] = ("general",)
    backend = "openrouter"

    def execute(self, ctx: ExecutionContext) -> dict[str, Any]:
        intent = (ctx.intent or "").strip()
        if intent and _GREETING_ONLY_RE.search(intent):
            action_id = next(iter(ctx.actions), "greeting")
            params = (ctx.actions.get(action_id) or {}).get("fields", {}) or {}
            return executor_result(
                ctx.domain,
                [
                    action_result(
                        action_id,
                        params,
                        tool_calls=["viito:greeting"],
                        data={
                            "ok": True,
                            "subtype": "greeting",
                            "model": "viito:static",
                            "answer": _GREETING_TEXT,
                        },
                    )
                ],
                status="success",
                error=None,
            )

        if not ctx.actions:
            return executor_result(ctx.domain, [], status="success", error=None)

        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            results = [
                deferred_action(
                    action,
                    params,
                    reason="OPENROUTER_API_KEY absent - can't answer general questions",
                )
                for action, params in ctx.actions.items()
            ]
            return executor_result(ctx.domain, results, status="success", error=None)

        models = model_defaults()
        subtype = _subtype(ctx.actions, ctx.intent)
        system, model_role = _SUBTYPES[subtype]
        model = str(models.get(model_role, models["REDACTION_MODEL"]))
        source = _source_text(ctx.actions, ctx.intent)
        history = _transcript_block(ctx)

        results: list[dict[str, Any]] = []
        # The router's attributes drive the generation: ``message`` is what to
        # write (context already stripped by the routing layer) and ``tone`` is
        # the register to respect.
        router_fields = _string_fields(ctx.actions)
        tone = router_fields.get("tone") or router_fields.get("style") or ""
        request = _strip_context(router_fields.get("message") or "") or _strip_context(ctx.intent or "")
        attached = _attached_sources(ctx.intent or "")
        source = _source_text(ctx.actions, ctx.intent)
        if attached:
            # Chat attachments are reference material the model must read to
            # answer (summary, analysis, redaction based on the file).
            source = f"{attached}\n\n{source}" if source else attached
        for action, payload in ctx.actions.items():
            # Recorded params are the attributes themselves (message, tone, ...),
            # like every other domain: not the {"fields": {...}} wrapper.
            params = (payload or {}).get("fields", payload) or {}
            user_prompt = (
                f"Demande de l'utilisateur :\n{request or action}\n\n"
                f"Texte à traiter :\n{source}\n"
                if source and source != request
                else f"Demande de l'utilisateur :\n{request or action}"
            )
            if tone:
                user_prompt += f"\n\nTon à respecter : {tone}."
            if history:
                user_prompt = history + "\n\n" + user_prompt
            clean = re.sub(r"\n{3,}", "\n\n", user_prompt).strip()
            try:
                answer = _complete(model, api_key, system, clean)
                data = {"ok": True, "subtype": subtype, "model": model, "answer": answer}
            except Exception as exc:  # noqa: BLE001 - network/API errors are reported, not raised
                data = {
                    "ok": False,
                    "subtype": subtype,
                    "model": model,
                    "answer": f"Je n'ai pas pu générer la réponse pour le moment ({exc}).",
                }
            results.append(
                action_result(
                    action,
                    params,
                    tool_calls=[f"viito:{model}"],
                    data=data,
                )
            )

        return executor_result(ctx.domain, results, status="success", error=None)