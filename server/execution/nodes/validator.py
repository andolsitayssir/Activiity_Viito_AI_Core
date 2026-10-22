"""Validator node — structural (rule-based) + semantic (LLM judge) stages.

Two failure classes, each with its own bounded retry loop:

1. **Execution failure** (structural): the executor reported ``status=="error"``
   or marked an action as ``deferred`` (backend not wired). The results *may
   still match* the query, but the execution itself failed.
      -> route ``retry``: re-run executor on the SAME domain (bounded by
         ``MAX_RETRIES``), then ``escalate`` to the formatter when exhausted.

2. **Semantic mismatch** (LLM judge): the execution *succeeded* structurally but
   the results do not answer the user query (wrong routing, empty result set
   where a record was expected, etc.).
      -> route ``reroute``: re-run the WHOLE pipeline (router -> dispatcher ->
         executor -> validator, bounded by ``MAX_ROUTER_RETRIES``), then
         ``escalate`` when exhausted.

On multi-domain turns the semantic judge is **deferred until the last queued
domain executes**: an intermediate domain is validated structurally and the
turn simply advances (``next_domain``). Judging one domain's result against the
FULL query ("create contact AND add as prospect AND email it") before the other
domains have run would false-mismatch the whole turn into a reroute loop. The
final domain is judged against the aggregate of every domain's results.

Decision table (after both checks):

    structural fail, execution retries left      -> "retry"
    structural fail, retries exhausted           -> "escalate"
    structural ok, semantic mismatch, router retries left -> "reroute"
    structural ok, semantic mismatch, retries exhausted   -> "escalate"
    structural ok, queue non-empty                        -> "next_domain"
    structural ok, semantic match, queue empty            -> "done"
"""

from __future__ import annotations

import datetime
import json
import os
from typing import Any, Callable

from server.config import model_defaults, load_environment
from server.execution.config import MAX_RETRIES, MAX_ROUTER_RETRIES
from server.execution.executors.odoo import _event_scope

load_environment()

VALIDATOR_MODEL = model_defaults()["VALIDATOR_MODEL"]  # e.g. openai/gpt-4o-mini

_SEMANTIC_SUBMIT_TIMEOUT = 90
_SEMANTIC_BACKUP_MODELS = ("google/gemma-4-31b-it", "google/gemma-3-12b-it")

_SYSTEM_SEMANTIC = (
    "Tu es le module de validation des résultats de Viito. Tu compares le RÉSULTAT "
    "d'exécution ci-dessous avec la DEMANDE de l'utilisateur.\n"
    "Réponds UNIQUEMENT par du JSON : {\"match\": true|false, \"reason\": \"...\"}\n"
    "- match=true : le résultat répond réellement à la demande (objet trouvé, action effectuée).\n"
    "- match=false : le résultat ne répond PAS à la demande (rien trouvé alors qu'un objet était "
    "attendu, mauvais objet, valeur absente, action non effectuée).\n"
    "Sans préjugés : une recherche qui ne trouve RIEN est une réponse complète quand l'utilisateur "
    "demandait une liste (« aucune réunion à venir » est un match, pas un échec). En revanche, "
    "une création/modification qui n'a rien produit est un mismatch.\n"
    "Tiens compte de la date du moment fournie : « à venir » = après maintenant, « passées » = "
    "avant maintenant. Ne redemande pas la date du jour.\n"
"Une confirmation explicite dans la réponse (ok:true, ids non vides, nombre de lignes supprimées) "
    "v vaut action effectuée. Après une suppression réussie, une recherche de vérification ne contenant "
    "PLUS l'objet supprimé n'est PAS un mismatch : l'objet absent est la preuve de la suppression.\n"
    "duplicate_suppressed=true signifie que l'action a déjà été effectuée lors d'un essai précédent du "
    "MÊME tour (l'envoi a bien eu lieu) : c'est une SUCCÈS, jamais un échec.\n"
    "Ne signale JAMAIS comme mismatch une faute de frappe ou une formulation de l'utilisateur (objet, "
    "corps du mail, nom de fichier) : ces textes sont envoyés tels quels, sans reformulation. Juge "
    "l'action effectuée, pas l'orthographe ni la mise en forme du texte fourni par l'utilisateur.\n"
    "Une pièce jointe demandée ET présente dans le résultat (champ attachment_names, ou attached non "
    "vide) vaut pièce jointe correctement envoyée.\n"
    "Chaque action affiche 'asked=...' : utilise-le pour relier la demande à son résultat."
)


def _structural_pass(result: dict[str, Any]) -> tuple[bool, str | None]:
    """Rule-based structural check of one ExecutorResult-layout dict.

    Fails when: status != success, no action actually ran, or any action
    reported an execution failure (``data.ok is False``). A per-action
    execution failure is NOT a routing problem: retrying the same domain
    locally (and escalating once exhausted) beats triggering a whole-pipeline
    reroute that would re-execute the OTHER domains too — with duplicate
    side-effects (re-sent emails, re-created records).

    A ``deferred`` action ("executor not wired for this backend yet") is a
    capability Viito does not have, not an error: it used to fail the turn,
    which retried the domain three times and then told the user the mail had
    not been sent while it was sitting in the recipient's inbox. Unsupported
    actions are still reported in the action list, and the semantic stage
    decides whether they matter for what the user asked.
    """
    if not isinstance(result, dict):
        return False, "executor_result missing or not a dict"
    status = result.get("status") or "success"
    if status != "success":
        return False, f"executor status={status!r}"
    actions = result.get("results") or []
    executed = 0
    for action in actions:
        data = action.get("data") or {}
        if data.get("ok") is False:
            return False, f"action {action.get('action')!r} failed: {data.get('error') or 'execution error'}"
        if not data.get("deferred"):
            executed += 1
    if actions and not executed:
        names = ", ".join(
            f"{a.get('action')} ({(a.get('data') or {}).get('reason') or 'not wired'})"
            for a in actions
        )
        return False, f"no action could run, all unsupported: {names}"
    return True, None


def _parse_dt(value: Any) -> datetime.datetime | None:
    text = str(value or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(text[: len(fmt) + 2].strip(), fmt)
        except ValueError:
            continue
    return None


#: Actions that produce a real external side effect, plus the response keys that
#: prove it happened (Gmail returns the sent message id).
_DELIVERED_ACTIONS = {
    "email.email_submission.set": ("id", "thread_id", "message_id"),
    "email.email.reply": ("id", "thread_id", "message_id"),
}


def _deterministic_verdict(
    query: str, result: dict[str, Any]
) -> tuple[bool, str] | None:
    """Rule-based semantic verdicts that must not depend on the LLM judge."""
    delivered = _delivered_turn_verdict(result)
    if delivered is not None:
        return delivered
    return _read_turn_verdict(query, result)


def _delivered_turn_verdict(result: dict[str, Any]) -> tuple[bool, str] | None:
    """Settle a delivery turn without the LLM judge, or None to let it judge.

    A send that returned a message id HAS been delivered. Escalating such a turn
    made Viito tell the user the email could not be sent (and, before the
    idempotency guard, re-send it) while the mail was sitting in the recipient's
    inbox. The judge used to be asked to double-check it and kept failing the
    turn over the user's own spelling inside the message body, which is sent
    verbatim on purpose.
    """
    actions = result.get("results") or []
    if not actions:
        return None
    delivered: list[str] = []
    for action in actions:
        data = action.get("data") or {}
        if data.get("ok") is False or data.get("error"):
            return None  # a real error: let the judge describe it
        name = str(action.get("action") or "")
        keys = _DELIVERED_ACTIONS.get(name)
        if not keys:
            continue
        response = data.get("response")
        if isinstance(response, dict) and any(response.get(k) for k in keys):
            delivered.append(name)
    if delivered:
        return True, f"envoi confirmé par l'identifiant du message ({', '.join(delivered)})"
    return None


def _read_turn_verdict(query: str, result: dict[str, Any]) -> tuple[bool, str] | None:
    """Settle plain read turns without the LLM judge, or None to let it judge.

    Two cases the judge used to get wrong, both of which turned a correct answer
    into an escalation and a fabricated message:

    * an empty result set for a listing ("aucune réunion à venir") IS the answer;
    * "les réunions passées" means before now, which the judge could not know
      because it was never given the current date.
    """
    actions = result.get("results") or []
    if not actions:
        return None
    records: list[dict[str, Any]] = []
    listing = False
    for action in actions:
        data = action.get("data") or {}
        if data.get("ok") is False or data.get("error"):
            return None
        payload = data.get("response")
        if isinstance(payload, list):
            listing = True
            records.extend(rec for rec in payload if isinstance(rec, dict))
        elif isinstance(payload, dict) and payload.get("answer"):
            return None  # content turn: the judge still decides
        elif payload not in (None, "", {}):
            return None  # a mutation: the judge still decides
    if not listing:
        return None

    scope = _event_scope(query)
    if not records:
        # A search that found nothing HAS answered a listing request.
        return True, "recherche exécutée, aucun résultat"
    if scope:
        now = datetime.datetime.now()
        starts = [d for d in (_parse_dt(rec.get("start")) for rec in records) if d]
        if starts:
            if scope == "upcoming" and any(start < now for start in starts):
                return False, "les événements renvoyés ne sont pas tous à venir"
            if scope == "past" and any(start > now for start in starts):
                return False, "les événements renvoyés ne sont pas tous passés"
        return True, f"lecture temporelle cohérente : {len(records)} résultat(s)"
    # Records exist and the query has no temporal scope: the judge must still
    # confirm the right objects were returned (name, amount, partner...).
    return None


def _semantic_judge(
    query: str,
    result: dict[str, Any],
    judge: Callable[[str, dict[str, Any]], tuple[bool, str]] | None = None,
) -> tuple[bool, str]:
    """Ask an LLM judge whether the results answer the user query.

    ``judge`` is injectable for tests. Default: OpenRouter chat completions
    with VALIDATOR_MODEL (fallback chain on 429/failure).

    Gated by ``VIITO_ENABLE_SEMANTIC_VALIDATION`` (default enabled). Tests set
    it to "0" so graph runs never fire live LLM calls.
    """
    enable = os.environ.get("VIITO_ENABLE_SEMANTIC_VALIDATION", "1")
    if str(enable).strip().lower() not in ("1", "true", "yes", "on"):
        return True, "semantic validation disabled"

    # Deterministic verdicts are applied by the caller (validator_node) so they
    # also hold for an injected judge.
    if judge is not None:
        return judge(query, result)

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return True, "no judge available (OPENROUTER_API_KEY absent); structural pass"
    if not query:
        return True, "no user query; nothing to judge"

    import requests
    import time

    results_compact = _compact_for_judge(result)
    user_content = (
        "Date du moment : "
        f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        "Demande de l'utilisateur :\n"
        f"{query}\n\n"
        "Résultat d'exécution :\n"
        f"{results_compact}"
    )

    candidates = []
    value = str(VALIDATOR_MODEL or "").strip()
    if value and value != "none":
        candidates.append(value)
    for model in _SEMANTIC_BACKUP_MODELS:
        if model not in candidates:
            candidates.append(model)

    for model in candidates:
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "X-Title": "activity-ai-engine",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_SEMANTIC},
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": 0.0,
                    "max_tokens": 200,
                    "response_format": {"type": "json_object"},
                },
                timeout=_SEMANTIC_SUBMIT_TIMEOUT,
            )
            if resp.status_code == 429:
                time.sleep(1.5)
                continue
            resp.raise_for_status()
            body = resp.json()
            content = str(body["choices"][0]["message"]["content"]).strip()
            parsed = json.loads(content)
            if isinstance(parsed, dict) and "match" in parsed:
                return bool(parsed["match"]), str(parsed.get("reason") or "")
            return True, "judge response unparsable; pass"
        except Exception:
            time.sleep(1.5)
    return True, "judge unavailable; pass"


def _compact_for_judge(result: dict[str, Any]) -> str:
    """Trim a result dict down to the essentials for the judge.

    Includes each action's requested ``params`` so the judge can correlate what
    was asked for with what came back (e.g. delete 'newayit' -> ids [105, 104]),
    instead of judging bare responses in a vacuum.
    """
    lines: list[str] = []
    for action in (result.get("results") or [])[:5]:
        data = action.get("data") or {}
        params = action.get("params") or {}
        asked = json.dumps(params, ensure_ascii=False, default=str)[:400]
        lines.append(
            f"- action={action.get('action')} asked={asked} status=ok "
            f"data={json.dumps(data, ensure_ascii=False, default=str)[:600]}"
        )
    if result.get("error"):
        lines.append(f"- error: {result['error'][:200]}")
    return "\n".join(lines) or "(no result data)"


def _aggregate_turn_results(state: dict[str, Any]) -> dict[str, Any]:
    """Combine every domain's ExecutorResult executed this turn into one dict
    for the end-of-pipeline semantic judge.

    Keeps only the LATEST attempt per domain (a transient error recovered by a
    retry must not taint the aggregate), mirroring the formatter's policy.
    """
    result = state.get("executor_result") or {}
    start = state.get("_log_start")
    log = state.get("executor_log")
    if start is None or not log:
        return result
    entries = list(log)[int(start):]
    if not entries:
        return result

    latest_by_domain: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if isinstance(entry, dict) and entry.get("domain"):
            latest_by_domain[str(entry["domain"])] = entry
    latest = list(latest_by_domain.values())

    results: list[dict[str, Any]] = []
    domains: list[str] = []
    status = "success"
    error = None
    for entry in latest:
        results.extend(entry.get("results") or [])
        if entry.get("status") != "success":
            status = entry.get("status") or "error"
            error = error or entry.get("error")
    return {
        "domain": "|".join(domains),
        "status": status,
        "results": results,
        "error": error,
        "attempts": 1,
    }


def validator_node_factory(
    judge: Callable[[str, dict[str, Any]], tuple[bool, str]] | None = None,
) -> Callable:
    """Build the validator node with an injectable semantic judge (tests)."""
    semantic = judge or _semantic_judge

    def validator_node(state: dict[str, Any]) -> dict[str, Any]:
        result = state.get("executor_result") or {}
        query = state.get("query") or state.get("intent") or ""
        queue = list(state.get("domain_queue") or [])

        struct_ok, struct_reason = _structural_pass(result)
        if not struct_ok:
            stage = "structural"
            reason = struct_reason
            passed = False
        elif queue:
            # More domains remain: judge structurally only and advance. The
            # full-intent semantic judge runs once, on the LAST domain, against
            # the aggregate of every domain's results.
            stage = "structural"
            passed = True
            reason = "deferred until all queued domains executed"
        else:
            # Last queued domain: judge the whole turn's results (all domains)
            # against the full user query.
            aggregate = _aggregate_turn_results(state)
            # Deterministic verdicts first: they hold whatever judge is in use
            # (live or injected) so a delivered mail is never re-litigated.
            settled = _deterministic_verdict(query, aggregate)
            if settled is not None:
                matched, reason = settled
            else:
                matched, reason = semantic(query, aggregate)
            if matched:
                stage = "structural"
                passed = True
                reason = None
            else:
                stage = "semantic"
                passed = False
                reason = reason or "results do not match the query"

        print(
            f"[VALIDATOR] passed={passed} stage={stage} reason={reason} "
            f"retry_count={state.get('retry_count')} router_retry_count={state.get('router_retry_count')}",
            flush=True,
        )

        patch: dict[str, Any] = {
            "validation_result": {
                "passed": passed,
                "stage": stage,
                "reason": reason,
            }
        }
        if not passed:
            if stage == "structural":
                patch["retry_count"] = int(state.get("retry_count") or 0) + 1
                patch["error_history"] = list(state.get("error_history") or []) + [
                    reason or "structural failure"
                ]
            else:
                patch["router_retry_count"] = int(state.get("router_retry_count") or 0) + 1
                patch["router_error_history"] = list(state.get("router_error_history") or []) + [
                    reason or "semantic mismatch"
                ]
                patch["_reroute_requested"] = True

        return patch

    return validator_node


validator_node = validator_node_factory()