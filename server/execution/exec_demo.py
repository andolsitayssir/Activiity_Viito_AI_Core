"""CLI to smoke-test the per-backend executors live and print their results.

This exercises the exact code path the graph's ``executor`` node uses: the
executor registry + ``ExecutionContext`` -> ``execute()``. No LLM needed.

Usage (from the repo root):

    env\\Scripts\\python.exe -m server.execution.exec_demo list
    env\\Scripts\\python.exe -m server.execution.exec_demo                  # every backend
    env\\Scripts\\python.exe -m server.execution.exec_demo --backend odoo
    env\\Scripts\\python.exe -m server.execution.exec_demo --action crm.lead.create --field name=Acme --field expected_revenue=50000
    env\\Scripts\\python.exe -m server.execution.exec_demo --backend rag --deep   # also runs the slow LLM grounded-answer

     # end-to-end: raw NL query -> domain/action/attributes classifiers -> execution graph
    env\\Scripts\\python.exe -m server.execution.exec_demo --query "cree un lead pour Acme d'un montant de 12000"
    env\\Scripts\\python.exe -m server.execution.exec_demo --query "crée une tâche 'Relancer le client' dans le projet Chantier" --local
    env\\Scripts\\python.exe -m server.execution.exec_demo --query "envoie un email a demo@localhost disant bonjour" --dry-run

``--query`` routes the text through the domain classifier, the action
classifier and the attribute extractor (the same agents the production API
uses), builds the RouterOutput envelope, then runs it through the real
LangGraph execution graph (dispatcher -> executor -> validator -> formatter)
against the live backends. By default OpenRouter is used when
OPENROUTER_API_KEY is set; ``--local`` forces the deterministic rule-based
clients and ``--dry-run`` prints the parsed plan without executing anything.

`.env` must be present (load_environment() reads it from the repo root).
"""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any, Callable

from server.config import load_environment

load_environment()

from server.agents.action_classifier import (
    build_action_classifier_client,
    classify_actions,
)
from server.agents.attributes_classifier import (
    build_attributes_classifier_client,
    classify_attributes,
)
from server.agents.domain_classifier2 import (
    build_domain_classifier_client,
    classify_domain,
)
from server.cli import DEFAULT_MODEL, attribute_classifier_model
from server.execution.executors.base import ExecutionContext
from server.execution.executors.registry import get_registry

# ---------------------------------------------------------------------------
# Safe, non-destructive showcase actions per backend (verified live).
# ---------------------------------------------------------------------------


def _ts() -> str:
    return time.strftime("%m%d-%H%M%S")


def _showcase() -> dict[str, list[dict[str, Any]]]:
    """Safe, non-destructive showcase actions per backend (verified live).

    Names are computed once per call so cross-referenced actions (e.g. odoo
    create then read) share the same target.
    """
    ts = _ts()
    lead = f"Lead Demo {ts}"
    client = f"Client Demo {ts}"
    project = f"Projet CLI {ts}"
    task = f"Tache CLI {ts}"
    return {
        "chatwoot": [
            {"action": "messaging.contact.create", "fields": {"name": client, "email": f"client{ts}@example.com"}},
            {"action": "messaging.conversation.create", "fields": {"customer": client, "message": "Bonjour depuis le CLI Viito"}},
        ],
        "odoo": [
            {"action": "crm.lead.create", "fields": {"name": lead, "expected_revenue": 50000, "priority": "2"}},
            {"action": "crm.lead.search_read", "fields": {"lead": lead}},
        ],
        "stalwart": [
            {
                "action": "email.email_submission.set",
                "fields": {
                    "recipients": f"demo{ts}@localhost",
                    "subject": f"Test Viito CLI {ts}",
                    "content": "Email envoye depuis le CLI de test des executors.",
                },
            },
        ],
        "vikunja": [
            {"action": "tasking.project.create", "fields": {"title": project, "description": "projet de demo CLI"}},
            {"action": "tasking.task.create", "fields": {"task": task, "project": project, "description": "creer depuis le CLI", "priority": 2}},
            {"action": "tasking.comment.create", "fields": {"task": task, "content": "commentaire CLI"}},
        ],
        "onlyoffice": [
            {"action": "office.command.version", "fields": {}},
        ],
        "rag": [
            {"action": "kb.search.fulltext", "fields": {"search_query": "delegation", "space": "UA-1"}},
            {"action": "kb.space.read", "fields": {}},
            {"action": "kb.status.batch", "fields": {}},
        ],
    }

_DEEP: dict[str, list[dict[str, Any]]] = {
    "rag": [
        {"action": "kb.answer.grounded", "fields": {"question": "Quels sont les cinq principes d'une delegation efficace ?", "space": "medium"}},
    ],
}

_DOMAIN_ALIAS = {"email": "email_action", "kb": "knowledge_kb"}

_BACKENDS = {
    "chatwoot": "messaging",
    "odoo": "crm",
    "stalwart": "email_action",
    "vikunja": "tasking",
    "onlyoffice": "office",
    "rag": "knowledge_kb",
}


def _coerce(value: str) -> Any:
    low = value.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(value)
    except ValueError:
        return value


def _domain_for_action(action: str) -> str:
    prefix = action.split(".", 1)[0]
    return _DOMAIN_ALIAS.get(prefix, prefix)


def _summarize(data: dict[str, Any]) -> str:
    """Compact, human-readable summary of one action record."""
    out: list[str] = []
    if data.get("deferred"):
        return f"DEFERRED ({data.get('reason', '')})"
    if data.get("ok") is False:
        return f"FAIL: {data.get('error', '?')}"
    resp = data.get("response", {})
    if isinstance(resp, dict):
        for key in ("id", "task_id", "project_id", "email_id", "submission_id", "label_id", "run_id", "duplicate_id"):
            if resp.get(key) is not None:
                out.append(f"{key}={resp[key]}")
        if resp.get("answer"):
            out.append(f"answer={str(resp['answer'])[:80]!r}")
        if isinstance(resp.get("contexts"), list) and resp["contexts"]:
            out.append(f"contexts={len(resp['contexts'])}")
        if isinstance(resp.get("hits"), list):
            out.append(f"hits={len(resp['hits'])}")
        if isinstance(resp.get("spaces"), list):
            out.append(f"spaces={','.join(s['id'] for s in resp['spaces'])}")
        if isinstance(resp.get("versions"), list):
            out.append(f"versions={resp['versions']}")
        if resp.get("version") is not None:
            out.append(f"version={resp['version']}")
    elif isinstance(resp, list) and resp:
        out.append(f"{len(resp)} items")
    return " | ".join(out) if out else "ok"


def _run_actions(domain: str, actions: dict[str, dict[str, Any]], json_mode: bool = False) -> None:
    executor = get_registry().executor_for(domain)
    ctx = ExecutionContext(domain=executor.domains[0], actions=actions)
    result = executor.execute(ctx)
    out: list[str] = []
    for entry in result["results"]:
        line = entry["action"]
        if json_mode:
            out.append(json.dumps(entry, ensure_ascii=False, default=str))
        else:
            out.append(f"  [{executor.backend}] {line}\n      {_summarize(entry['data'])}")
    print("\n".join(out))


def _cmd_list() -> None:
    registry = get_registry()
    print("registered executors (VIITO_ENABLE_* toggles, default on):")
    for domain in registry.registered_domains():
        executor = registry.executor_for(domain)
        print(f"  {domain:<14} -> {executor.backend}")


def _cmd_showcase(backend: str, deep: bool, json_mode: bool) -> None:
    showcase = _showcase()
    if backend not in showcase:
        raise SystemExit(f"unknown backend {backend!r}; choices: {', '.join(showcase)}")
    domain = _BACKENDS[backend]
    actions: dict[str, dict[str, Any]] = {}
    for item in showcase[backend]:
        actions[item["action"]] = {"fields": item["fields"]}
    if deep and backend in _DEEP:
        for item in _DEEP[backend]:
            actions[item["action"]] = {"fields": item["fields"]}
    print(f"\n=== {backend} (domain {domain!r}) ===")
    _run_actions(domain, actions, json_mode)


def _cmd_arbitrary(action: str, fields: list[str], domain: str | None, json_mode: bool) -> None:
    domain = domain or _domain_for_action(action)
    parsed: dict[str, Any] = {}
    for f in fields:
        if "=" not in f:
            raise SystemExit(f"--field must be key=value (got {f!r})")
        key, _, value = f.partition("=")
        parsed[key] = _coerce(value)
    print(f"=== {action} (domain {domain!r}) ===")
    print("  fields:", json.dumps(parsed, ensure_ascii=False))
    _run_actions(domain, {action: {"fields": parsed}}, json_mode)


def _build_classifier_clients(
    local: bool, api_key: str | None, model: str, attributes_model: str
) -> tuple[Any | None, Any | None, Any | None]:
    """Return (domain_client, action_client, attributes_client).

    ``local`` forces the deterministic rule-based clients. Otherwise OpenRouter
    is used when an API key is available (from --openrouter-api-key or env);
    missing key falls back to rule-based with a warning.
    """
    if local:
        return None, None, None
    resolved = api_key or os.environ.get("OPENROUTER_API_KEY")
    if not resolved:
        print("  [query] OPENROUTER_API_KEY absent — using rule-based classifiers (--local).", flush=True)
        return None, None, None
    common = dict(model=model, api_key=resolved, temperature=0.0, max_tokens=1024)
    attributes_common = dict(model=attributes_model, api_key=resolved, temperature=0.0, max_tokens=1024)
    return (
        build_domain_classifier_client(**common),
        build_action_classifier_client(**common),
        build_attributes_classifier_client(**attributes_common),
    )


def _cmd_query(
    query: str,
    *,
    local: bool,
    api_key: str | None,
    model: str,
    attributes_model: str,
    dry_run: bool,
    json_mode: bool,
) -> None:
    """End-to-end: raw NL query -> classifiers -> RouterOutput -> LangGraph graph."""
    domain_client, action_client, attributes_client = _build_classifier_clients(
        local, api_key, model, attributes_model
    )
    print(f"\n=== query ===\n  {query}")

    domain_pred = classify_domain(query, model_client=domain_client)
    domains = domain_pred.predicted_domains
    action_pred = classify_actions(
        query,
        domains=domains,
        model_client=action_client,
        domain_client=domain_client,
    )
    actions = action_pred.predicted_actions
    attr_pred = classify_attributes(
        query,
        domains=domains,
        actions=actions,
        model_client=attributes_client,
        domain_client=domain_client,
        action_client=action_client,
    )

    envelope = attr_pred.to_terminal()
    envelope["intent"] = query
    print(f"\n  domains : {', '.join(envelope['domains']) if envelope['domains'] else '(aucun)'}")
    print(f"  actions : {', '.join(envelope['actions']) if envelope['actions'] else '(aucune)'}")
    for domain, per_action in (envelope.get("fields") or {}).items():
        for action_id, payload in (per_action or {}).items():
            fields = (payload or {}).get("fields", {})
            print(f"    [{domain}] {action_id}: {json.dumps(fields, ensure_ascii=False)}")

    if dry_run:
        print("\n[dry-run] classification done — nothing executed.")
        return

    from server.execution.graph import build_graph, run_turn

    app = build_graph()
    state = run_turn(
        app,
        router_output=envelope,
        thread_id=f"cli-query-{time.strftime('%m%d-%H%M%S')}",
        intent=query,
        user_id="cli",
    )
    final = state.get("final_response") or {}
    print(f"\n  outcome: {final.get('outcome')} | {final.get('message')}")
    for record in final.get("actions_attempted") or []:
        print(f"\n  [{record.get('domain')}] status={record.get('status')} attempts={record.get('attempts')}")
        for entry in record.get("results") or []:
            if json_mode:
                print("    " + json.dumps(entry, ensure_ascii=False, default=str))
            else:
                print(f"    {entry.get('action')}\n      {_summarize(entry.get('data'))}")
        if record.get("error"):
            print(f"    error: {record['error']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m server.execution.exec_demo",
        description="Live smoke-test the per-backend executors through the registry.",
    )
    parser.add_argument("list", nargs="?", help="list registered executors")
    parser.add_argument("--backend", type=str, default=None, help=f"only this backend: {', '.join(_BACKENDS)}")
    parser.add_argument("--action", type=str, default=None, help="run one arbitrary action, e.g. crm.lead.create")
    parser.add_argument("--field", action="append", default=[], help="key=value for --action (repeatable, ints/bools coerced)")
    parser.add_argument("--query", type=str, default=None, help="route a raw NL query through domain + action + attributes classifiers, then execute (real user path)")
    parser.add_argument("--local", action="store_true", help="with --query: use rule-based classifiers instead of OpenRouter")
    parser.add_argument("--openrouter-api-key", type=str, default=None, help="with --query: OpenRouter API key (falls back to OPENROUTER_API_KEY env)")
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL, help=f"with --query: OpenRouter model for domain/action classifiers (default: {DEFAULT_MODEL})")
    parser.add_argument("--attributes-model", type=str, default=attribute_classifier_model, help=f"with --query: OpenRouter model for attribute extraction (default: {attribute_classifier_model})")
    parser.add_argument("--dry-run", action="store_true", help="with --query: parse the plan but do not execute")
    parser.add_argument("--domain", type=str, default=None, help="override the executor domain for --action")
    parser.add_argument("--deep", action="store_true", help="still run slow LLM-backed actions (kb.answer.grounded)")
    parser.add_argument("--json", action="store_true", dest="json_mode", help="print raw JSON records")
    args = parser.parse_args(argv)

    if args.list == "list":
        _cmd_list()
        return 0
    if args.query:
        _cmd_query(
            args.query,
            local=args.local,
            api_key=args.openrouter_api_key,
            model=args.model,
            attributes_model=args.attributes_model,
            dry_run=args.dry_run,
            json_mode=args.json_mode,
        )
        return 0
    if args.action:
        _cmd_arbitrary(args.action, args.field, args.domain, args.json_mode)
        return 0
    if args.backend:
        _cmd_showcase(args.backend, args.deep, args.json_mode)
        return 0

    _cmd_list()
    backends = list(_BACKENDS)
    for backend in backends:
        _cmd_showcase(backend, args.deep, args.json_mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())