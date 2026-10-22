"""Response formatter node — produces the final user-facing message.

The formatter normalizes executor output into the UI-facing ``final_response``
(= ``/conversations/:id/messages`` reply), updates ``session_memory`` for the
next turn, and clears per-turn execution fields so a checkpointed turn boundary
stays clean.

Message policy (per the pipeline spec, never delegated to the frontend):

* ``general`` / ``knowledge_kb`` turns whose executors returned a content
  ``answer`` are passed through verbatim (natural French, already formulated).
* Action turns are re-formulated by a fast LLM (``FORMATTER_MODEL``) into one
  or two natural French sentences — no technical jargon, no tool/action names.
* Failure/escalation gets a short, clear explanation.
* Any LLM hiccup (or missing key) falls back to the deterministic template so
  the pipeline always answers.
"""

from __future__ import annotations

import datetime
import os
import re
import time
from typing import Any

import requests

from server.config import load_environment, model_defaults
from server.execution.executors.odoo import _event_scope

load_environment()

_BASE_URL = "https://openrouter.ai/api/v1"
_REFORMULATE_TIMEOUT = 90

#: fallback formatter models, tried in order when FORMATTER_MODEL is
#: rate-limited or fails upstream (the shared pool can 429 at any time).
_FORMATTER_BACKUP_MODELS = ("google/gemma-4-31b-it", "google/gemma-3-12b-it")
_MAX_FORMATTER_ATTEMPTS = 2
_FORMATTER_RETRY_DELAY = 1.5

#: domains whose executor results are themselves the final answer.
_CONTENT_DOMAINS = {"general", "knowledge_kb"}

_SYSTEM_SUCCESS = (
    "Tu es le module de formulation de réponses de Viito, l'assistant intégré à Activiity Center. "
    "À partir de la demande de l'utilisateur et du résultat d'exécution ci-dessous, rédige UNE "
    "réponse en français naturel, chaleureux et concis (1 à 3 phrases maximum), qui dit ce qui a été fait.\n"
    "Règles :\n"
    "- Pas de jargon technique, pas de noms d'actions, pas de noms de domaines ni d'outils.\n"
    "- Si plusieurs opérations ont réussi, résume-les en une phrase globale.\n"
    "- Reprends les valeurs utiles (nom, référence, date) si elles sont présentes, sans rien inventer.\n"
    "- Confirmation directe, sans préambule.\n"
    "- Pour les recherches : indique le nombre de résultats et liste les éléments principaux (noms, montants).\n"
    "- Ne promets JAMAIS de fournir une information plus tard (« je vous la communique », « je reviens vers "
    "vous ») : si les résultats sont fournis (results_preview / answer / response), affiche-les directement. "
    "Si la liste est vide, dis simplement qu'il n'y a aucun résultat.\n"
    "- Réponds UNIQUEMENT avec le message final, sans titre ni balise."
)

_SYSTEM_FAILURE = (
    "Tu es le module de formulation de réponses de Viito, l'assistant intégré à Activiity Center. "
    "Une partie (ou toute) de la demande de l'utilisateur n'a pas pu être exécutée. Rédige UNE réponse "
    "en français naturel, courte et claire (1 à 3 phrases), qui explique ce qui n'a pas abouti.\n"
    "Règles :\n"
    "- Si une partie de la demande a RÉUSSI (objet créé, email envoyé, etc.), indique-le aussi brièvement "
    "avant le point bloquant : ne laisse pas croire que rien n'a été fait, ne prétends pas que le reste a réussi non plus.\n"
    "- Pas de jargon technique, pas de codes d'erreur bruts, pas de noms de domaines ni d'outils.\n"
    "- Reste factuel et rassurant, ne blâme jamais l'utilisateur.\n"
    "- Propose éventuellement une prochaine étape simple (vérifier X, réessayer).\n"
    "- Réponds UNIQUEMENT avec le message final, sans titre ni balise."
)

#: scalar keys worth echoing to the user when present in an action result.
_KEY_VALUE_KEYS = ("id", "name", "name_", "title", "reference", "number", "task_id", "project_id", "subject")

#: Read actions answer with a list of records; rendering them is deterministic
#: because asking the model to retype a list invites placeholders ("[noms des
#: réunions]") or an empty promise ("je vous la communique").
_RECORD_LIMIT = 10

_MONTHS_FR = ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août",
              "sept.", "oct.", "nov.", "déc.")
_DAYS_FR = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")


def _format_when(value: Any) -> str:
    """'2026-09-29 13:00:00' -> 'mar. 29 sept. 2026 à 13:00'."""
    text = str(value or "").strip().replace("T", " ")
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})[ ]?(\d{2}):(\d{2})", text)
    if not match:
        return text
    year, month, day, hour, minute = match.groups()
    try:
        weekday = _DAYS_FR[datetime.date(int(year), int(month), int(day)).weekday()]
    except ValueError:
        return text
    return (
        f"{weekday} {int(day)} {_MONTHS_FR[int(month) - 1]} {year} à {hour}:{minute}"
    )


def _record_label(record: dict[str, Any]) -> str:
    name = str(
        record.get("name") or record.get("title") or record.get("subject")
        or record.get("display_name") or "(sans nom)"
    )
    when = _format_when(record.get("start") or record.get("date") or record.get("deadline"))
    stop = _format_when(record.get("stop"))
    line = f"{name} — {when}" if when else name
    if stop and stop != when:
        line += f" (jusqu'à {stop})"
    place = record.get("location") or record.get("place")
    if place:
        line += f" — {place}"
    return line


def _record_lists(attempts: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Every successful action's payload, but only when they are all plain reads.

    A turn that also mutated something keeps its LLM wording, so a created record
    is never hidden behind a read-only rendering.
    """
    lists: list[list[dict[str, Any]]] = []
    for record in attempts:
        for result in record.get("results") or []:
            data = result.get("data") or {}
            if data.get("answer") or data.get("error"):
                return []
            payload = data.get("response")
            if isinstance(payload, list):
                lists.append([rec for rec in payload if isinstance(rec, dict)])
            elif isinstance(payload, dict):
                return []
            elif payload not in (None, "", {}):
                return []
    return lists


def _format_records(intent: str, attempts: list[dict[str, Any]]) -> str | None:
    """Render read results as the final message, or None if not a pure read turn."""
    lists = _record_lists(attempts)
    if not lists:
        return None
    records = lists[0]
    count = len(records)
    scope = _event_scope(intent)
    if scope == "upcoming":
        if not count:
            return "Vous n'avez aucune réunion à venir."
        header = f"Vous avez {count} réunion{'' if count == 1 else 's'} à venir :"
    elif scope == "past":
        if not count:
            return "Vous n'avez aucune réunion passée."
        header = f"Voici vos {count} dernière{'' if count == 1 else 's'} réunion{'' if count == 1 else 's'} :"
    else:
        if not count:
            return "Je n'ai trouvé aucun résultat."
        header = f"J'ai trouvé {count} résultat{'' if count == 1 else 's'} :"
    lines = [f"{i}. {_record_label(rec)}" for i, rec in enumerate(records[:_RECORD_LIMIT], 1)]
    if count > _RECORD_LIMIT:
        lines.append(f"… et {count - _RECORD_LIMIT} autre{'' if count - _RECORD_LIMIT == 1 else 's'}.")
    return "\n".join([header, *lines])


def formatter_node(state: dict[str, Any]) -> dict[str, Any]:
    user_id = state.get("user_id", "")
    org_id = state.get("org_id", "")
    conversation_id = state.get("conversation_id", "")
    intent = state.get("intent", "")
    router_output = state.get("router_output") or {}

    start = int(state.get("_log_start") or 0)
    attempts = list((state.get("executor_log") or [])[start:])
    domains = _unique_domains(attempts) or (router_output.get("domains") or [])

    # Judge success/failure on the LATEST attempt per domain only: a transient
    # error recovered by a retry must not taint the final message as failed.
    latest_by_domain: dict[str, dict[str, Any]] = {}
    for record in attempts:
        latest_by_domain[record.get("domain") or ""] = record
    latest_attempts = list(latest_by_domain.values())

    outcome = state.get("outcome") or "finished"
    # Semantic escalation: the last validation failed because the results do
    # NOT match the user query (all whole-pipeline retries exhausted).
    last_validation = state.get("validation_result") or {}
    semantic_escalated = (
        last_validation.get("stage") == "semantic" and not last_validation.get("passed")
    )
    escalated = outcome in ("escalated", "failed") or semantic_escalated
    # Deferred actions (backend not wired) and hard status errors count as
    # failures so the formatter never reformulates them as success.
    deferred = any(
        (result.get("data") or {}).get("deferred")
        for record in latest_attempts
        for result in (record.get("results") or [])
    )
    failed = (
        escalated
        or deferred
        or any(record.get("status") == "error" for record in latest_attempts)
        or any(
            (result.get("data") or {}).get("ok") is False
            for record in latest_attempts
            for result in (record.get("results") or [])
        )
    )

    final_response: dict[str, Any] = {
        "outcome": "escalated" if escalated else "finished",
        "message": _build_message(intent, domains, attempts, escalated, failed),
        "domains": domains,
        "actions_attempted": attempts,
        "data": {},
        "escalated": escalated,
        "failed": failed,
        "user_id": user_id,
        "org_id": org_id,
        "conversation_id": conversation_id,
    }
    print(f"[FORMATTER] outcome={outcome} escalated={escalated} failed={failed} message={final_response['message'][:120]!r}", flush=True)

    session_memory: dict[str, Any] = {
        "last_intent": intent,
        "last_domains": domains,
        "last_actions": [r.get("domain") and r.get("results") and [x.get("action") for x in r.get("results") or []] for r in attempts],
        "last_fields": router_output.get("fields") or {},
        "last_executor_results": attempts,
        "last_final_response": final_response,
    }
    # Flatten the nested action list for ergonomic follow-up resolution.
    session_memory["last_actions"] = [
        action_id
        for r in attempts
        for action_id in ([x.get("action") for x in r.get("results") or []] if r.get("results") else [r.get("domain")])
    ]

    # Short-term memory: keep a rolling transcript of recent turns (user + assistant)
    # so a follow-up like "redige la suite" / "suis l'option 3" has prior context.
    transcript = list((state.get("session_memory") or {}).get("transcript") or [])
    assistant_msg = str(final_response.get("message") or "")
    if intent:
        transcript.append({"role": "user", "content": intent})
    if assistant_msg:
        transcript.append({"role": "assistant", "content": assistant_msg})
    # Cap to the last ~8 turns (each turn = up to 2 messages).
    session_memory["transcript"] = transcript[-16:]

    patch: dict[str, Any] = {
        "final_response": final_response,
        "session_memory": session_memory,
    }
    # Clear per-turn execution fields so the next turn starts clean.
    for key, empty in {
        "current_domain": "",
        "domain_queue": [],
        "executor_result": None,
        "validation_result": None,
        "retry_count": 0,
        "error_history": [],
        "outcome": "finished",
    }.items():
        if key in state and state.get(key):
            patch[key] = empty
    return patch


def _build_message(
    intent: str,
    domains: list[str],
    attempts: list[dict[str, Any]],
    escalated: bool,
    failed: bool,
) -> str:
    answers = _content_answers(attempts)
    # A pure content turn (general / knowledge_kb) already holds the finished message.
    if not escalated and answers and set(domains) <= _CONTENT_DOMAINS:
        return "\n\n".join(answers)

    # Nothing actually executed (e.g. a turn with zero results): keep it deterministic.
    if not any(record.get("results") for record in attempts):
        return _fallback_message(attempts, escalated, failed)

    # Pure read turn ("liste les réunions à venir"): render the records verbatim.
    # The model would retype them as placeholders or promise them "later".
    if not escalated:
        listed = _format_records(intent, attempts)
        if listed:
            return listed

    return _reformulate(intent, attempts, failed) or _fallback_message(attempts, escalated, failed)


def _reformulate(intent: str, attempts: list[dict[str, Any]], failed: bool) -> str | None:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return None
    defaults = model_defaults()
    candidates = [str(defaults.get("FORMATTER_MODEL") or "qwen/qwen3-8b")]
    for candidate in _FORMATTER_BACKUP_MODELS:
        if candidate not in candidates:
            candidates.append(candidate)

    system_prompt = _SYSTEM_FAILURE if failed else _SYSTEM_SUCCESS
    user_content = (
        "Demande de l'utilisateur :\n"
        f"{intent or '(non fournie)'}\n\n"
        "Résultat d'exécution :\n"
        f"{_compact(_attempts_for_llm(attempts), depth=0)}"
    )

    for model in candidates:
        for attempt in range(1, _MAX_FORMATTER_ATTEMPTS + 1):
            try:
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 300,
                }
                resp = requests.post(
                    f"{_BASE_URL}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "X-Title": "activity-ai-engine",
                    },
                    json=payload,
                    timeout=_REFORMULATE_TIMEOUT,
                )
                if resp.status_code == 429:
                    time.sleep(_FORMATTER_RETRY_DELAY * attempt)
                    continue
                resp.raise_for_status()
                body = resp.json()
                message = str(body["choices"][0]["message"]["content"]).strip()
                if message:
                    return message
                return None
            except Exception:  # noqa: BLE001 - reformulation is best-effort by design.
                time.sleep(_FORMATTER_RETRY_DELAY * attempt)
    return None


def _attempts_for_llm(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in attempts:
        actions: list[dict[str, Any]] = []
        for result in record.get("results") or []:
            data = result.get("data") or {}
            raw_response = data.get("response")
            response = raw_response if isinstance(raw_response, dict) else {}
            # Read-oriented actions (event.query, crm.lead.search_read, ...) answer
            # with a LIST of records: it must reach the model, otherwise it invents
            # a promise ("je vous la communique") instead of listing the records.
            records = raw_response if isinstance(raw_response, list) else []
            entry: dict[str, Any] = {"action": result.get("action")}
            error = data.get("error") or response.get("error")
            if error:
                entry["status"] = "error"
                entry["error"] = str(error)
            elif response.get("answer"):
                entry["status"] = "ok"
                entry["answer"] = str(response["answer"])
            elif data.get("answer"):
                entry["status"] = "ok"
                entry["answer"] = str(data["answer"])
            else:
                entry["status"] = "ok" if data.get("ok", True) else "error"
            entry["params"] = {
                str(k): _compact(str(v), depth=0)
                for k, v in (result.get("params") or {}).items()
                if v not in (None, "", [], {})
            }
            for key in _KEY_VALUE_KEYS:
                if key in data:
                    entry[key] = str(data[key])
                elif key in response:
                    entry[key] = str(response[key])
            if records:
                entry["result_count"] = len(records)
                entry["results_preview"] = [
                    {k: v for k, v in rec.items() if k != "id" and v is not None}
                    for rec in records[:10]
                    if isinstance(rec, dict)
                ]
            elif isinstance(response, dict) and not response.get("answer"):
                entry["response"] = _compact(str(response), depth=0)
            actions.append(entry)
        rows.append({"domain": record.get("domain"), "status": record.get("status"), "actions": actions})
    return rows


def _content_answers(attempts: list[dict[str, Any]]) -> list[str]:
    answers: list[str] = []
    for record in attempts:
        if record.get("status") != "success":
            continue
        for result in record.get("results") or []:
            data = result.get("data") or {}
            if data.get("ok") is False:
                continue
            answer = (data.get("response") or {}).get("answer") if isinstance(data.get("response"), dict) else None
            if not isinstance(answer, str) or not answer.strip():
                answer = data.get("answer")
            if isinstance(answer, str) and answer.strip():
                answers.append(answer.strip())
    return answers


def _fallback_message(attempts: list[dict[str, Any]], escalated: bool, failed: bool) -> str:
    if escalated or failed:
        return "Impossible de terminer la demande : quelque chose n'a pas abouti. Voulez-vous que je réessaie d'une autre façon ?"
    lines = [_fallback_action_line(record) for record in attempts]
    lines = [line for line in lines if line]
    if lines:
        return "\n".join(lines)
    return f"Executed {len(attempts)} action(s) across {len(_unique_domains(attempts))} domain(s)."


#: deterministic fallback (used only when no LLM key / model is reachable).
_VERB_FR = {
    "action_convert": "converti",
    "action_post": "validé",
    "action_reconcile": "rapproché",
    "action_undo_reconciliation": "dé-rapproché",
    "mark_as_done": "clôturé",
    "archived": "archivé",
    "duplicate": "dupliqué",
    "send": "envoyé",
    "create": "créé",
    "write": "mis à jour",
    "update": "mis à jour",
    "unlink": "supprimé",
    "convert": "converti",
    "upload": "importé",
    "ingest": "importé",
    "share": "partagé",
}

_RESOURCE_FR = {
    "crm.lead": "le lead",
    "crm.opportunity": "l'opportunité",
    "res.partner": "le contact",
    "sale.order": "le devis",
    "account.move.line": "l'écriture comptable",
    "account.move": "la facture",
    "account.bank.statement": "le relevé bancaire",
    "hr.leave": "le congé",
    "hr.expense": "la note de frais",
    "project.task": "la tâche",
    "project.project": "le projet",
    "calendar.event": "l'événement",
    "mail.message": "l'email",
    "kb.space": "l'espace",
    "kb.document": "le document",
    "document": "le document",
}

_KV_LABELS = {
    "company": "société",
    "partner": "société",
    "company_name": "société",
    "name": "nom",
    "subject": "objet",
    "task_name": "nom",
    "event_name": "nom",
    "email": "email",
    "phone": "téléphone",
    "mobile": "mobile",
    "probability": "probabilité",
    "expected_revenue": "CA attendu",
    "amount_total": "montant",
    "amount": "montant",
    "price_total": "montant",
    "stage": "étape",
    "priority": "priorité",
    "due_date": "échéance",
    "start": "début",
    "end": "fin",
    "to": "destinataire",
    "question": "question",
    "attachments": "pièces jointes",
    "filename": "fichier",
    "project": "projet",
    "project_id": "projet",
    "space_id": "espace",
}

_FALLBACK_PARAM_SKIP = ("fields", "tool_calls")


def _fallback_params(params: dict[str, Any]) -> str:
    segments: list[str] = []
    for key, value in (params or {}).items():
        if key in _FALLBACK_PARAM_SKIP or value in (None, "", [], {}):
            continue
        if isinstance(value, dict):
            continue
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(x) for x in value)
        label = _KV_LABELS.get(key, key)
        segments.append(f"{label} : {_compact(str(value), depth=0)}")
    return " ; ".join(segments)[:180]


def _fallback_action_line(record: dict[str, Any]) -> str | None:
    results = record.get("results") or []
    if not results:
        return None
    actions: list[str] = []
    for result in results:
        action = (result.get("action") or "").lower()
        if not action:
            continue
        verb = next((label for suffix, label in _VERB_FR.items() if action.endswith(suffix)), None)
        resource = next((label for prefix, label in _RESOURCE_FR.items() if action.startswith(prefix)), None)
        phrase = None
        if verb and resource:
            phrase = f"{verb} {resource}"
        elif verb:
            phrase = f"{verb} l'élément"
        params = result.get("params") or {}
        values = _fallback_params(params)
        if values:
            phrase = f"{phrase or action} ({values})" if phrase else f"{action} ({values})"
        if (result.get("data") or {}).get("ok") is False:
            phrase = (phrase or action) + " — en échec"
        actions.append(phrase or action)
    if not actions:
        return None
    if len(actions) == 1:
        return "J'ai " + actions[0] + "."
    return "J'ai " + " ; ".join(actions) + "."


def _compact(value: Any, depth: int) -> Any:
    """Keep LLM payloads small and deterministic (strings capped, keys limited)."""
    if depth > 5:
        return type(value).__name__
    if isinstance(value, str):
        return value if len(value) <= 300 else f"{value[:297]}..."
    if isinstance(value, dict):
        return {str(k): _compact(v, depth + 1) for k, v in list(value.items())[:12]}
    if isinstance(value, list):
        return [_compact(v, depth + 1) for v in value[:10]]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)[:300]


def _unique_domains(records: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for record in records:
        domain = record.get("domain")
        if domain and domain not in seen:
            seen.append(domain)
    return seen