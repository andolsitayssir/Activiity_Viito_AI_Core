from __future__ import annotations

import argparse
import ast
import ast
import csv
import json
import os
import statistics
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Callable
import re
from server.config import load_environment

load_environment()
os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")
mlflow = import_module("mlflow")

from server.agents.attributes_classifier import (
    AttributesClassifierAgent,
    build_attributes_classifier_client,
    FIELD_NAME_ALIASES,
)
from server.agents.domain_classifier2 import (
    normalize_domains,
)
from server.agents.action_classifier import (
    ACTION_IDS_BY_DOMAIN,
    normalize_actions,
)
from server.eval.domain_classification_eval import (
    _read_csv_rows,
    _read_json_rows,
    _read_xlsx_rows,
    discover_test_files,
)

DEFAULT_DATA_ROOT = Path(__file__).resolve().parent / "test_data" / "attribute_testing_data_fixed.xlsx"
DEFAULT_METRICS_ROOT = Path(__file__).resolve().parents[1] / "metrics"
DEFAULT_EXPERIMENT_PREFIX = "attribute-classifier-test"
_EMAIL_RECIPIENT_FIELDS = {"recipient", "recipients", "to", "cc", "bcc"}
_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


@dataclass(frozen=True)
class EvalSample:
    intent: str
    domains: list[str]
    actions: list[str]
    expected_fields: dict[str, dict[str, dict]]
    source_file: str
    row_index: int
    source_type: str | None = None


@dataclass(frozen=True)
class EvalPrediction:
    intent: str
    domains: list[str]
    actions: list[str]
    true_actions: list[str]
    fields: dict[str, dict[str, dict]]
    true_fields: dict[str, dict[str, dict]]
    fields_exact: bool
    fields_recall: bool
    field_precision: float
    field_recall: float
    field_f1: float
    path_recall: float
    value_accuracy: float
    normalized_value_accuracy: float
    faithfulness: float
    raw_response: str
    latency_seconds: float
    source_file: str
    row_index: int
    source_type: str | None = None


@dataclass(frozen=True)
class DomainFieldMetrics:
    precision: float
    recall: float
    f1: float
    exact_accuracy: float
    expected_pairs: int


@dataclass(frozen=True)
class EvaluationSummary:
    total_samples: int
    empty_field_samples: int
    fields_exact_count: int
    fields_exact_accuracy: float
    fields_recall_count: int
    fields_recall_accuracy: float
    field_precision: float
    field_recall: float
    field_f1: float
    path_recall: float
    value_accuracy: float
    normalized_value_accuracy: float
    faithfulness: float
    action_coverage: float
    extra_field_rate: float
    missing_field_rate: float
    sample_f1_mean: float
    expected_pairs: int
    predicted_pairs: int
    correct_pairs: int
    invalid_predictions: int
    invalid_rate: float
    average_latency_seconds: float
    throughput_samples_per_second: float
    runtime_seconds: float
    per_domain: dict[str, DomainFieldMetrics]
    run_id: str
    run_name: str
    experiment_name: str
    model_name: str
    data_root: str
    metrics_root: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["per_domain"] = {d: asdict(m) for d, m in self.per_domain.items()}
        return payload


def _select_text_column(fieldnames) -> str | None:
    preferred = ("question", "text", "query", "utterance", "prompt", "input", "message", "sentence", "intent")
    normalized = {name.lower().strip(): name for name in fieldnames}
    for candidate in preferred:
        actual = normalized.get(candidate)
        if actual is not None:
            return actual
    return None


def _select_column(fieldnames, preferred) -> str | None:
    normalized = {name.lower().strip(): name for name in fieldnames}
    for candidate in preferred:
        actual = normalized.get(candidate)
        if actual is not None:
            return actual
    return None


def load_attributes_eval_samples(
    data_root: Path | str,
    text_column_override: str | None = None,
    domains_column_override: str | None = None,
    actions_column_override: str | None = None,
    fields_column_override: str | None = None,
) -> list[EvalSample]:
    root = Path(data_root)
    samples: list[EvalSample] = []
    skipped = 0

    for file_path in discover_test_files(root):
        suffix = file_path.suffix.lower()
        if suffix == ".csv":
            rows = _read_csv_rows(file_path)
        elif suffix == ".json":
            rows = _read_json_rows(file_path)
        else:
            rows = _read_xlsx_rows(file_path)
        if not rows:
            continue

        fieldnames = list(rows[0].keys())
        text_column = text_column_override or _select_text_column(fieldnames)
        domains_column = domains_column_override or _select_column(fieldnames, ("domains", "domain", "tools", "labels"))
        actions_column = actions_column_override or _select_column(
            fieldnames, ("actions", "action", "action_ids", "labels")
        )
        fields_column = fields_column_override or _select_column(
            fieldnames, ("fields", "expected_fields", "attributes", "field_values")
        )
        if text_column is None or domains_column is None or actions_column is None:
            skipped += len(rows)
            continue

        source_type_column = None
        normalized_fieldnames = {name.lower().strip() for name in fieldnames}
        for candidate in ("type", "source", "app", "system", "tool"):
            if candidate in normalized_fieldnames:
                source_type_column = next(name for name in fieldnames if name.lower().strip() == candidate)
                break

        for row_index, row in enumerate(rows, start=1):
            intent_text = row.get(text_column)
            if not intent_text:
                intent_text = row.get("query")
            if intent_text is None or not str(intent_text).strip():
                skipped += 1
                continue

            domains = normalize_domains(row.get(domains_column))
            actions = normalize_actions(row.get(actions_column))
            if not actions:
                skipped += 1
                continue

            expected_fields = _parse_expected_fields(row.get(fields_column))
            samples.append(
                EvalSample(
                    intent=str(intent_text).strip(),
                    domains=domains,
                    actions=actions,
                    expected_fields=expected_fields,
                    source_file=file_path.name,
                    row_index=row_index,
                    source_type=row.get(source_type_column) if source_type_column else None,
                )
            )

    if not samples:
        raise ValueError(f"No labeled attribute samples were loaded from {root}")

    if skipped:
        print(f"  Skipped {skipped} rows (null intent, missing domains/actions, or unparseable data)")

    return samples


def _parse_expected_fields(value: Any) -> dict[str, dict[str, dict]]:
    if value is None:
        return {}
    raw = str(value).strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _alias_field(action_id: str, field_name: str) -> str:
    aliases = FIELD_NAME_ALIASES.get(action_id)
    if aliases and field_name in aliases:
        return aliases[field_name]
    return field_name


def _fold_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value)
    return "".join(char for char in normalized if not unicodedata.combining(char))


def _normalize_value(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        # Handle GT list-format strings like "['email@example.com']" or "['name1', 'name2']"
        # by normalizing to the same form the model outputs (plain string or sorted tuple)
        if stripped.startswith("[") and stripped.endswith("]"):
            inner = _parse_list_literal(stripped)
            if isinstance(inner, list):
                normalized = tuple(sorted(_normalize_value(item) for item in inner))
                return normalized if len(normalized) != 1 else normalized[0]
        return " ".join(_fold_accents(stripped).lower().split())
    if isinstance(value, dict):
        return tuple(
            sorted((_normalize_value(k), _normalize_value(v)) for k, v in value.items())
        )
    if isinstance(value, list):
        normalized = tuple(sorted(_normalize_value(item) for item in value))
        return normalized if len(normalized) != 1 else normalized[0]
    return _normalize_value(str(value))


def _strip_currency_tokens(value: str) -> str:
    text = value.strip()
    if not text:
        return text
    text = re.sub(
        r"\b(?:eur|euro|euros|usd|us\$|dollar|dollars|cad|aud|gbp|chf|jpy|yen|€|£|\$)\b",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return " ".join(text.split())


def _strip_generic_entity_prefixes(value: str) -> str:
    text = value.strip()
    if not text:
        return text
    patterns = (
        r"^(?:la|le|les|un|une|l'|de la|du|des|de)\s+",
        r"^(?:facture|facture de|contact|le contact|la facture|la note de frais|le pointage|le congé|la reunion|la réunion|le devis|devis|commande|commande de|email|l'email|document|le document|piece jointe|pièce jointe|tâche|tache|réunion|reunion)\s+(?:de\s+)?",
    )
    for pattern in patterns:
        candidate = re.sub(pattern, "", text, flags=re.IGNORECASE)
        if candidate != text:
            return " ".join(candidate.split())
    return " ".join(text.split())


def _parse_list_literal(value: str) -> list[Any] | None:
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, list):
        return parsed
    try:
        parsed = ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return None
    return parsed if isinstance(parsed, list) else None


def _normalize_email_recipients(value: Any) -> Any:
    if isinstance(value, list):
        normalized = tuple(sorted(_normalize_email_recipients(item) for item in value))
        return normalized if len(normalized) != 1 else normalized[0]
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            parsed = _parse_list_literal(stripped)
            if isinstance(parsed, list):
                return _normalize_email_recipients(parsed)

        matches = [_normalize_value(match.group(0)) for match in _EMAIL_PATTERN.finditer(stripped)]
        unique_matches = tuple(sorted(set(matches)))
        if len(unique_matches) > 1:
            return unique_matches
        if len(unique_matches) == 1 and "<" in stripped and ">" in stripped:
            return unique_matches[0]
    return _normalize_value(value)


def _normalize_field_value(action_id: str, field_name: str, value: Any) -> Any:
    canonical_field = _alias_field(action_id, field_name)
    if action_id.startswith("email.") and canonical_field in _EMAIL_RECIPIENT_FIELDS:
        return _normalize_email_recipients(value)
    return _normalize_value(value)


def _normalize_comparable_value(value: Any, action_id: str | None = None, field_name: str | None = None) -> Any:
    if action_id is not None and field_name is not None:
        normalized = _normalize_field_value(action_id, field_name, value)
    else:
        normalized = _normalize_value(value)
    if isinstance(normalized, str):
        return _strip_generic_entity_prefixes(_strip_currency_tokens(normalized))
    return normalized


def _values_equivalent(left: Any, right: Any, action_id: str | None = None, field_name: str | None = None) -> bool:
    left_norm = _normalize_comparable_value(left, action_id, field_name)
    right_norm = _normalize_comparable_value(right, action_id, field_name)
    if left_norm == right_norm:
        return True
    if isinstance(left_norm, str) and isinstance(right_norm, str):
        return left_norm.replace(" ", "") == right_norm.replace(" ", "")
    return False


def _flatten_fields(fields: dict[str, dict[str, dict]]) -> set[tuple[str, str, str, Any]]:
    pairs: set[tuple[str, str, str, Any]] = set()
    for domain, actions in (fields or {}).items():
        if not isinstance(actions, dict):
            continue
        for action, payload in actions.items():
            inner = payload.get("fields") if isinstance(payload, dict) and isinstance(payload.get("fields"), dict) else {}
            if not isinstance(inner, dict):
                continue
            for field, value in inner.items():
                canonical_field = _alias_field(action, field)
                pairs.add((domain, action, canonical_field, _normalize_field_value(action, canonical_field, value)))
    return pairs


def _f1(precision: float, recall: float) -> float:
    return 2.0 * precision * recall / (precision + recall) if (precision + recall) else 0.0


def _safe_divide(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _score_fields(expected: dict, predicted: dict) -> tuple[bool, bool, float, float, float]:
    expected_pairs = _flatten_fields(expected)
    predicted_pairs = _flatten_fields(predicted)
    exact = predicted_pairs == expected_pairs
    recall_flag = expected_pairs.issubset(predicted_pairs)
    if not expected_pairs and not predicted_pairs:
        return exact, recall_flag, 1.0, 1.0, 1.0
    correct = expected_pairs & predicted_pairs
    precision = _safe_divide(len(correct), len(predicted_pairs))
    recall = _safe_divide(len(correct), len(expected_pairs))
    f1 = _f1(precision, recall)
    return exact, recall_flag, precision, recall, f1


def _tokenize(text: str) -> list[str]:
    return str(_normalize_value(text)).split()


def _token_f1(pred_tokens: list[str], gold_tokens: list[str]) -> float:
    if not pred_tokens and not gold_tokens:
        return 1.0
    if not pred_tokens or not gold_tokens:
        return 0.0
    common = len(set(pred_tokens) & set(gold_tokens))
    prec = common / len(pred_tokens)
    rec = common / len(gold_tokens)
    return _f1(prec, rec)


def _value_metrics(
    expected: dict, predicted: dict
) -> tuple[float, float, float, float]:
    """Return (path_recall, value_accuracy, normalized_value_accuracy, faithfulness)."""
    exp_items: list[tuple[tuple[str, str, str], Any]] = []
    for domain, actions in (expected or {}).items():
        if not isinstance(actions, dict):
            continue
        for action, payload in actions.items():
            inner = payload.get("fields") if isinstance(payload, dict) and isinstance(payload.get("fields"), dict) else {}
            for field, value in (inner or {}).items():
                canonical_field = _alias_field(action, field)
                exp_items.append(((domain, action, canonical_field), value))

    pred_map: dict[tuple[str, str, str], Any] = {}
    for domain, actions in (predicted or {}).items():
        if not isinstance(actions, dict):
            continue
        for action, payload in actions.items():
            inner = payload.get("fields") if isinstance(payload, dict) and isinstance(payload.get("fields"), dict) else {}
            for field, value in (inner or {}).items():
                canonical_field = _alias_field(action, field)
                pred_map[(domain, action, canonical_field)] = value

    total = len(exp_items)
    if total == 0:
        return 1.0, 1.0, 1.0, 1.0

    path_recall_count = 0
    value_accuracy_count = 0
    normalized_value_accuracy_count = 0
    faithfulness_sum = 0.0
    for key, gold_val in exp_items:
        if key in pred_map:
            path_recall_count += 1
            pred_val = pred_map[key]
            if _values_equivalent(pred_val, gold_val, key[1], key[2]):
                value_accuracy_count += 1
            if _normalize_comparable_value(pred_val, key[1], key[2]) == _normalize_comparable_value(gold_val, key[1], key[2]):
                normalized_value_accuracy_count += 1
            faithfulness_sum += _token_f1(_tokenize(str(pred_val)), _tokenize(str(gold_val)))

    return (
        path_recall_count / total,
        value_accuracy_count / total,
        normalized_value_accuracy_count / total,
        faithfulness_sum / total,
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _write_predictions_csv(path: Path, predictions: list[EvalPrediction]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "row_index", "source_file", "source_type", "intent",
                "domains", "true_actions", "predicted_actions",
                "true_fields", "predicted_fields",
                "fields_exact", "fields_recall",
                "field_precision", "field_recall", "field_f1",
                "path_recall", "value_accuracy", "normalized_value_accuracy", "faithfulness",
                "raw_response", "latency_seconds",
            ],
        )
        writer.writeheader()
        for prediction in predictions:
            writer.writerow({
                "row_index": prediction.row_index,
                "source_file": prediction.source_file,
                "source_type": prediction.source_type,
                "intent": prediction.intent,
                "domains": ",".join(prediction.domains),
                "true_actions": ",".join(prediction.true_actions),
                "predicted_actions": ",".join(prediction.actions),
                "true_fields": json.dumps(prediction.true_fields, ensure_ascii=False),
                "predicted_fields": json.dumps(prediction.fields, ensure_ascii=False),
                "fields_exact": prediction.fields_exact,
                "fields_recall": prediction.fields_recall,
                "field_precision": f"{prediction.field_precision:.4f}",
                "field_recall": f"{prediction.field_recall:.4f}",
                "field_f1": f"{prediction.field_f1:.4f}",
                "path_recall": f"{prediction.path_recall:.4f}",
                "value_accuracy": f"{prediction.value_accuracy:.4f}",
                "normalized_value_accuracy": f"{prediction.normalized_value_accuracy:.4f}",
                "faithfulness": f"{prediction.faithfulness:.4f}",
                "raw_response": prediction.raw_response,
                "latency_seconds": f"{prediction.latency_seconds:.3f}",
            })


def _slugify(value: str) -> str:
    cleaned = "".join(character.lower() if character.isalnum() else "-" for character in value)
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or "model"


def evaluate_attribute_classification(
    data_root: Path | str = DEFAULT_DATA_ROOT,
    *,
    metrics_root: Path | str = DEFAULT_METRICS_ROOT,
    experiment_name: str | None = None,
    model_name: str = "rule-based",
    run_name: str | None = None,
    model_client: Any | None = None,
    show_progress: bool = True,
    prompt_version: str = "zeroshot",
    request_delay: float = 0.3,
    max_workers: int = 8,
    on_progress: Callable[[int, int, float], None] | None = None,
) -> EvaluationSummary:
    data_root_path = Path(data_root)
    metrics_root_path = Path(metrics_root)
    metrics_root_path.mkdir(parents=True, exist_ok=True)

    samples = load_attributes_eval_samples(data_root_path)
    agent = AttributesClassifierAgent(model_client=model_client)

    run_id = uuid.uuid4().hex[:12]
    run_root = metrics_root_path / "runs" / f"{time.strftime('%Y%m%d-%H%M%S')}-{run_id}"
    run_root.mkdir(parents=True, exist_ok=True)

    resolved_experiment_name = experiment_name or DEFAULT_EXPERIMENT_PREFIX
    project_root = Path(__file__).resolve().parents[2]
    tracking_uri = f"sqlite:///{project_root / 'mlflow.db'}"
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(resolved_experiment_name)
    mlflow_client = mlflow.MlflowClient()
    experiment = mlflow.get_experiment_by_name(resolved_experiment_name)
    existing_runs = mlflow_client.search_runs(experiment_ids=[experiment.experiment_id])
    run_name_resolved = _slugify(model_name)
    for run in existing_runs:
        if (
            run.data.params.get("model_name") == model_name
            and run.data.params.get("data_root") == str(data_root_path)
            and run.data.params.get("prompt_version") == prompt_version
        ):
            run_name_resolved = f"{_slugify(model_name)}-{prompt_version}"
            version_counts = [
                r.info.run_name.split("-v")[-1]
                for r in existing_runs
                if r.info.run_name.startswith(f"{_slugify(model_name)}-{prompt_version}-v")
            ]
            if version_counts:
                run_name_resolved = f"{_slugify(model_name)}-{prompt_version}-v{max(int(v) for v in version_counts) + 1}"
            break

    total_samples = len(samples)
    started_at = time.perf_counter()

    def _predict_one(index: int, sample: EvalSample) -> EvalPrediction:
        if request_delay > 0:
            time.sleep(request_delay * (index % max_workers) / max_workers)

        domains = sample.domains
        actions = sample.actions

        started_sample = time.perf_counter()
        model_prediction = agent.predict(sample.intent, domains=domains, actions=actions)
        latency_seconds = time.perf_counter() - started_sample

        exact, recall_flag, precision, recall, f1 = _score_fields(
            sample.expected_fields, model_prediction.fields
        )
        path_recall, value_accuracy, normalized_value_accuracy, faithfulness = _value_metrics(
            sample.expected_fields, model_prediction.fields
        )
        return EvalPrediction(
            intent=sample.intent,
            domains=model_prediction.domains,
            actions=model_prediction.actions,
            true_actions=sample.actions,
            fields=model_prediction.fields,
            true_fields=sample.expected_fields,
            fields_exact=exact,
            fields_recall=recall_flag,
            field_precision=precision,
            field_recall=recall,
            field_f1=f1,
            path_recall=path_recall,
            value_accuracy=value_accuracy,
            normalized_value_accuracy=normalized_value_accuracy,
            faithfulness=faithfulness,
            raw_response=model_prediction.raw_response,
            latency_seconds=latency_seconds,
            source_file=sample.source_file,
            row_index=sample.row_index,
            source_type=sample.source_type,
        )

    predictions: list[EvalPrediction] = []
    completed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_predict_one, idx, sample): (idx, sample)
            for idx, sample in enumerate(samples)
        }
        for future in as_completed(futures):
            idx, sample = futures[future]
            try:
                pred = future.result()
                predictions.append(pred)
            except Exception as exc:
                print(f"[{idx+1}/{total_samples}] ERROR: {sample.intent[:60]} -> {exc}", flush=True)
                predictions.append(EvalPrediction(
                    intent=sample.intent,
                    domains=sample.domains,
                    actions=[],
                    true_actions=sample.actions,
                    fields={},
                    true_fields=sample.expected_fields,
                    fields_exact=False,
                    fields_recall=False,
                    field_precision=0.0,
                    field_recall=0.0,
                    field_f1=0.0,
                    path_recall=0.0,
                    value_accuracy=0.0,
                    normalized_value_accuracy=0.0,
                    faithfulness=0.0,
                    raw_response=f"ERROR: {exc}",
                    latency_seconds=0.0,
                    source_file=sample.source_file,
                    row_index=sample.row_index,
                    source_type=sample.source_type,
                ))
            completed += 1
            if on_progress:
                elapsed = time.perf_counter() - started_at
                rate = completed / elapsed if elapsed > 0 else 0
                on_progress(completed, total_samples, rate)
            if show_progress and completed % 10 == 0:
                elapsed = time.perf_counter() - started_at
                rate = completed / elapsed if elapsed > 0 else 0
                eta = (total_samples - completed) / rate if rate > 0 else 0
                print(
                    f"[{completed}/{total_samples}] {rate:.1f} samples/s | "
                    f"ETA: {eta/60:.1f}min",
                    flush=True,
                )

    predictions.sort(key=lambda p: p.row_index)
    runtime_seconds = time.perf_counter() - started_at

    empty_samples = [p for p in predictions if not _flatten_fields(p.true_fields)]
    non_empty = [p for p in predictions if _flatten_fields(p.true_fields)]

    total_expected = sum(len(_flatten_fields(p.true_fields)) for p in predictions)
    total_predicted = sum(len(_flatten_fields(p.fields)) for p in predictions)
    total_correct = sum(
        len(_flatten_fields(p.true_fields) & _flatten_fields(p.fields)) for p in predictions
    )
    field_precision = _safe_divide(total_correct, total_predicted)
    field_recall = _safe_divide(total_correct, total_expected)
    field_f1 = _f1(field_precision, field_recall)

    exact_count = sum(1 for p in predictions if p.fields_exact)
    recall_count = sum(1 for p in predictions if p.fields_recall)
    exact_accuracy = _safe_divide(exact_count, total_samples)
    recall_accuracy = _safe_divide(recall_count, total_samples)

    sample_f1s = [p.field_f1 for p in predictions]
    sample_f1_mean = statistics.mean(sample_f1s) if sample_f1s else 0.0

    mean_path_recall = statistics.mean([p.path_recall for p in predictions]) if predictions else 0.0
    mean_value_accuracy = statistics.mean([p.value_accuracy for p in predictions]) if predictions else 0.0
    mean_normalized_value_accuracy = statistics.mean([p.normalized_value_accuracy for p in predictions]) if predictions else 0.0
    mean_faithfulness = statistics.mean([p.faithfulness for p in predictions]) if predictions else 0.0

    invalid_predictions = sum(1 for p in predictions if not p.actions)
    invalid_rate = _safe_divide(invalid_predictions, total_samples)
    extra_field_total = sum(
        len(_flatten_fields(p.fields) - _flatten_fields(p.true_fields)) for p in predictions
    )
    missing_field_total = sum(
        len(_flatten_fields(p.true_fields) - _flatten_fields(p.fields)) for p in predictions
    )
    extra_field_rate = _safe_divide(extra_field_total, total_predicted or 1)
    missing_field_rate = _safe_divide(missing_field_total, total_expected or 1)
    action_coverage = 1.0 - invalid_rate

    latencies = [p.latency_seconds for p in predictions]
    average_latency = statistics.mean(latencies) if latencies else 0.0
    throughput = _safe_divide(total_samples, runtime_seconds)

    per_domain: dict[str, DomainFieldMetrics] = {}
    for domain in ACTION_IDS_BY_DOMAIN:
        domain_predictions = [p for p in predictions if any(d in p.true_fields for d in (domain,))]
        if not domain_predictions:
            continue
        exp = sum(len(_flatten_fields({d: p.true_fields[d]})) for p in domain_predictions for d in p.true_fields if d == domain)
        prd = sum(len(_flatten_fields({d: p.fields[d]})) for p in domain_predictions for d in p.fields if d == domain)
        cor = sum(
            len(_flatten_fields({d: p.true_fields[d]}) & _flatten_fields({d: p.fields[d]}))
            for p in domain_predictions for d in p.true_fields if d == domain and d in p.fields
        )
        prec = _safe_divide(cor, prd)
        rec = _safe_divide(cor, exp)
        domain_exact = _safe_divide(
            sum(1 for p in domain_predictions if p.fields_exact), len(domain_predictions)
        )
        per_domain[domain] = DomainFieldMetrics(
            precision=prec, recall=rec, f1=_f1(prec, rec),
            exact_accuracy=domain_exact, expected_pairs=exp,
        )

    summary = EvaluationSummary(
        total_samples=total_samples,
        empty_field_samples=len(empty_samples),
        fields_exact_count=exact_count,
        fields_exact_accuracy=exact_accuracy,
        fields_recall_count=recall_count,
        fields_recall_accuracy=recall_accuracy,
        field_precision=field_precision,
        field_recall=field_recall,
        field_f1=field_f1,
        path_recall=mean_path_recall,
        value_accuracy=mean_value_accuracy,
        normalized_value_accuracy=mean_normalized_value_accuracy,
        faithfulness=mean_faithfulness,
        action_coverage=action_coverage,
        extra_field_rate=extra_field_rate,
        missing_field_rate=missing_field_rate,
        sample_f1_mean=sample_f1_mean,
        expected_pairs=total_expected,
        predicted_pairs=total_predicted,
        correct_pairs=total_correct,
        invalid_predictions=invalid_predictions,
        invalid_rate=invalid_rate,
        average_latency_seconds=average_latency,
        throughput_samples_per_second=throughput,
        runtime_seconds=runtime_seconds,
        per_domain=per_domain,
        run_id=run_id,
        run_name=run_name_resolved,
        experiment_name=resolved_experiment_name,
        model_name=model_name,
        data_root=str(data_root_path),
        metrics_root=str(metrics_root_path),
    )

    _write_predictions_csv(run_root / "predictions.csv", predictions)
    _write_json(run_root / "summary.json", summary.to_dict())
    _write_json(run_root / "per_domain.json", {d: asdict(m) for d, m in per_domain.items()})

    with mlflow.start_run(run_name=run_name_resolved):
        mlflow.set_tag("task", "attribute-classifier-evaluation")
        mlflow.set_tag("model_name", model_name)
        mlflow.set_tag("prompt_version", prompt_version)
        mlflow.set_tag("experiment_name", resolved_experiment_name)
        mlflow.set_tag("data_root", str(data_root_path))
        mlflow.set_tag("run_root", str(run_root))
        mlflow.set_tag("catalog_mode", "oracle")
        mlflow.set_tag("run_root", str(run_root))

        mlflow.log_param("model_name", model_name)
        mlflow.log_param("prompt_version", prompt_version)
        mlflow.log_param("experiment_name", resolved_experiment_name)
        mlflow.log_param("data_root", str(data_root_path))
        mlflow.log_param("action_count", sum(len(a) for a in ACTION_IDS_BY_DOMAIN.values()))
        mlflow.log_param("total_samples", total_samples)
        mlflow.log_param("empty_field_samples", len(empty_samples))
        mlflow.log_param("non_empty_samples", len(non_empty))

        mlflow.log_metric("fields_exact_accuracy", exact_accuracy)
        mlflow.log_metric("fields_recall_accuracy", recall_accuracy)
        mlflow.log_metric("field_precision", field_precision)
        mlflow.log_metric("field_recall", field_recall)
        mlflow.log_metric("field_f1", field_f1)
        mlflow.log_metric("path_recall", mean_path_recall)
        mlflow.log_metric("value_accuracy", mean_value_accuracy)
        mlflow.log_metric("normalized_value_accuracy", mean_normalized_value_accuracy)
        mlflow.log_metric("faithfulness", mean_faithfulness)
        mlflow.log_metric("action_coverage", action_coverage)
        mlflow.log_metric("extra_field_rate", extra_field_rate)
        mlflow.log_metric("missing_field_rate", missing_field_rate)
        mlflow.log_metric("sample_f1_mean", sample_f1_mean)
        mlflow.log_metric("expected_pairs", float(total_expected))
        mlflow.log_metric("predicted_pairs", float(total_predicted))
        mlflow.log_metric("correct_pairs", float(total_correct))
        mlflow.log_metric("invalid_predictions", float(invalid_predictions))
        mlflow.log_metric("invalid_rate", invalid_rate)
        mlflow.log_metric("average_latency_seconds", average_latency)
        mlflow.log_metric("throughput_samples_per_second", throughput)
        mlflow.log_metric("runtime_seconds", runtime_seconds)

        for domain, metrics_obj in per_domain.items():
            mlflow.log_metric(f"precision_{domain}", metrics_obj.precision)
            mlflow.log_metric(f"recall_{domain}", metrics_obj.recall)
            mlflow.log_metric(f"f1_{domain}", metrics_obj.f1)
            mlflow.log_metric(f"exact_accuracy_{domain}", metrics_obj.exact_accuracy)
            mlflow.log_metric(f"expected_pairs_{domain}", float(metrics_obj.expected_pairs))

        mlflow.log_artifacts(str(run_root))

        if data_root_path.is_file():
            mlflow.log_artifact(str(data_root_path), artifact_path="dataset")
        else:
            mlflow.log_artifacts(str(data_root_path), artifact_path="dataset")

    print(f"\n  Experiment     : {resolved_experiment_name}")
    print(f"  Run            : {run_name_resolved}")
    print(f"  Prompt version : {prompt_version}")
    print(f"  Catalog mode   : oracle (ground-truth domains + actions)")
    print(f"  MLflow DB      : {project_root / 'mlflow.db'}")
    print(f"  To view        : mlflow ui")
    print(f"\n  === Overall ===")
    print(f"  Strict exact   : {exact_accuracy:.1%} ({exact_count}/{total_samples})")
    print(f"  Fields recall  : {recall_accuracy:.1%} ({recall_count}/{total_samples})")
    print(f"  Field P/R/F1   : {field_precision:.1%} / {field_recall:.1%} / {field_f1:.1%}")
    print(f"  Path recall    : {mean_path_recall:.1%}")
    print(f"  Value accuracy : {mean_value_accuracy:.1%}")
    print(f"  Normalized value: {mean_normalized_value_accuracy:.1%}")
    print(f"  Action coverage: {action_coverage:.1%}")
    print(f"  Extra field rate: {extra_field_rate:.1%}")
    print(f"  Missing field rate: {missing_field_rate:.1%}")
    print(f"  Faithfulness   : {mean_faithfulness:.1%}")
    print(f"  Sample F1 mean : {sample_f1_mean:.1%}")

    return summary


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate the attribute classifier against attribute test data.")
    parser.add_argument("--data-root", type=str, default=str(DEFAULT_DATA_ROOT), help="Folder or file containing evaluation data.")
    parser.add_argument("--metrics-root", type=str, default=str(DEFAULT_METRICS_ROOT), help="Where to store metrics and MLflow artifacts.")
    parser.add_argument("--experiment-name", type=str, default=None, help="Optional MLflow experiment name.")
    parser.add_argument("--model-name", type=str, required=True, help="OpenRouter model name for the attribute (extraction) classifier.")
    parser.add_argument("--run-name", type=str, default=None, help="Optional MLflow run name.")
    parser.add_argument("--provider", type=str, default="openrouter", choices=("openrouter", "local"), help="Use OpenRouter or the local rule-based classifier.")
    parser.add_argument("--prompt-version", type=str, default="zeroshot", choices=("zeroshot",), help="System prompt version.")
    parser.add_argument("--openrouter-api-key", type=str, default=None, help="OpenRouter API key.")
    parser.add_argument("--quiet", action="store_true", help="Disable per-row progress output.")
    parser.add_argument("--temperature", type=float, default=0.0, help="Sampling temperature.")
    parser.add_argument("--top-p", type=float, default=1.0, help="Top-p sampling parameter.")
    parser.add_argument("--max-tokens", type=int, default=1024, help="Maximum tokens in the completion.")
    parser.add_argument("--frequency-penalty", type=float, default=0.0, help="Frequency penalty.")
    parser.add_argument("--presence-penalty", type=float, default=0.0, help="Presence penalty.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed.")
    parser.add_argument("--openrouter-base-url", type=str, default="https://openrouter.ai/api/v1", help="OpenRouter base URL.")
    parser.add_argument("--openrouter-site-url", type=str, default=None, help="OpenRouter site URL header.")
    parser.add_argument("--openrouter-app-name", type=str, default="activity-ai-engine", help="OpenRouter app name header.")
    parser.add_argument("--max-workers", type=int, default=8, help="Number of parallel API workers.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    is_local = args.provider == "local"
    if is_local:
        model_client = None
        resolved_model_name = "rule-based"
    else:
        api_key = args.openrouter_api_key or os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENROUTER_API_KEY is required when provider=openrouter. Set it in .env or pass --openrouter-api-key."
            )
        model_client = build_attributes_classifier_client(
            model=args.model_name,
            api_key=api_key,
            temperature=args.temperature,
            top_p=args.top_p,
            max_tokens=args.max_tokens,
            frequency_penalty=args.frequency_penalty,
            presence_penalty=args.presence_penalty,
            seed=args.seed,
            base_url=args.openrouter_base_url,
            site_url=args.openrouter_site_url,
            app_name=args.openrouter_app_name,
        )
        resolved_model_name = args.model_name

    summary = evaluate_attribute_classification(
        data_root=args.data_root,
        metrics_root=args.metrics_root,
        experiment_name=args.experiment_name,
        model_name=resolved_model_name,
        run_name=args.run_name,
        model_client=model_client,
        show_progress=not args.quiet,
        prompt_version=args.prompt_version,
        request_delay=0.0 if is_local else 0.1,
        max_workers=args.max_workers,
    )

    print(json.dumps(summary.to_dict(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
