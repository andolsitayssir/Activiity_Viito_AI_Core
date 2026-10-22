from __future__ import annotations

import argparse
import json
import os

from server.config import load_environment, model_defaults
from server.agents.attributes_classifier import (
    classify_attributes,
    build_attributes_classifier_client,
)
from server.agents.domain_classifier2 import (
    classify_domain,
    build_domain_classifier_client,
)
from server.agents.action_classifier import (
    classify_actions,
    build_action_classifier_client,
)

load_environment()

_MODELS = model_defaults()
DEFAULT_MODEL = _MODELS["CLASSIFIER_MODEL"]
attribute_classifier_model = _MODELS["CLASSIFIER_MODEL"]


def _print_verbose(prediction) -> None:
    print(f"    raw     : {prediction.raw_response}")
    print(f"    parsed  : {prediction}")


def _format_field_values(fields: dict) -> str:
    if not fields:
        return "(aucun champ)"
    return ", ".join(f"{name}={value!r}" for name, value in fields.items())


def _build_clients(args) -> tuple:
    if args.local:
        return None, None, None
    api_key = args.openrouter_api_key or os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY required (set it in .env or pass --openrouter-api-key), "
            "or use --local for the rule-based clients."
        )
    common = dict(
        model=args.model,
        api_key=api_key,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
    )
    attributes_common = dict(
        model=args.attributes_model,
        api_key=api_key,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
    )
    domain_client = build_domain_classifier_client(**common)
    action_client = build_action_classifier_client(**common)
    attributes_client = build_attributes_classifier_client(**attributes_common)
    return domain_client, action_client, attributes_client


def _run_once(intent: str, domain_client, action_client, attributes_client, verbose: bool) -> None:
    domain_prediction = classify_domain(intent, model_client=domain_client)
    domains = domain_prediction.predicted_domains

    action_prediction = classify_actions(
        intent, domains=domains, model_client=action_client, domain_client=domain_client
    )
    actions = action_prediction.predicted_actions

    attributes_prediction = classify_attributes(
        intent,
        domains=domains,
        actions=actions,
        model_client=attributes_client,
        domain_client=domain_client,
        action_client=action_client,
    )

    print(f"domaines  : {', '.join(domains) if domains else '(aucun)'}")
    print(f"actions   : {', '.join(actions) if actions else '(aucune)'}")
    if verbose:
        print("\n  [verbose]")
        _print_verbose(domain_prediction)
        _print_verbose(action_prediction)
        _print_verbose(attributes_prediction)

    print("\n  champs extraits par domaine")
    terminal = attributes_prediction.to_terminal()
    fields = terminal.get("fields", {})
    if not fields:
        print("    (aucun champ)")
    for domain in terminal.get("domains", []):
        domain_fields = fields.get(domain, {})
        print(f"  {domain}")
        if not domain_fields:
            print("    (aucun champ)")
        for action_id, payload in domain_fields.items():
            inner = payload.get("fields", {}) if isinstance(payload, dict) else {}
            print(f"    {action_id}")
            print(f"      {_format_field_values(inner)}")

    print("\n  enveloppe terminale (JSON)")
    print(json.dumps(terminal, indent=2, ensure_ascii=False))


def _repl(domain_client, action_client, attributes_client, verbose: bool) -> None:
    print(
        "Interactive classifier demo. Type a question in French, "
        "'quit'/'exit' to leave."
    )
    while True:
        try:
            intent = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not intent:
            continue
        if intent.lower() in ("quit", "exit", "q"):
            break
        try:
            _run_once(intent, domain_client, action_client, attributes_client, verbose)
        except Exception as exc:
            print(f"  ERROR: {exc}")


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Interactively test the domain, action, and attributes classifiers."
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Use rule-based clients (no API key needed). Default: OpenRouter.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=DEFAULT_MODEL,
        help=f"OpenRouter model name for the domain and action classifiers (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--attributes-model",
        type=str,
        default=attribute_classifier_model,
        help=f"OpenRouter model name for the attribute classifier (default: {attribute_classifier_model}).",
    )
    parser.add_argument(
        "--openrouter-api-key",
        type=str,
        default=None,
        help="OpenRouter API key (falls back to OPENROUTER_API_KEY env var).",
    )
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Also print raw model responses and parsed dataclasses.",
    )
    parser.add_argument(
        "--intent",
        type=str,
        default=None,
        help="Run a single question and exit instead of entering the REPL.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_argument_parser().parse_args(argv)
    domain_client, action_client, attributes_client = _build_clients(args)
    if args.intent:
        _run_once(args.intent, domain_client, action_client, attributes_client, args.verbose)
    else:
        _repl(domain_client, action_client, attributes_client, args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
