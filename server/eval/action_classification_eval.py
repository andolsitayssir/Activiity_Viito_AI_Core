from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Callable

from server.config import load_environment

load_environment()
os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")
mlflow = import_module("mlflow")

from server.agents.action_classifier import (
    ACTION_IDS_BY_DOMAIN,
    ACTION_LABELS,
    ACTION_SYSTEM_PROMPTS_BY_DOMAIN,
    ActionClassifierAgent,
    build_action_classifier_client,
    normalize_actions,
)
from server.agents.domain_classifier2 import DomainClassifierAgent, normalize_domains
from server.eval.domain_classification_eval import (
    _read_csv_rows,
    _read_json_rows,
    _read_xlsx_rows,
    discover_test_files,
)

DEFAULT_DATA_ROOT = Path(__file__).resolve().parent / "test_data" / "action_classification_data_v4.xlsx"
DEFAULT_METRICS_ROOT = Path(__file__).resolve().parents[1] / "metrics"
DEFAULT_EXPERIMENT_PREFIX = "action-classifier-test-zero-shot"


@dataclass(frozen=True)
class EvalSample:
    intent: str
    domains: list[str]
    actions: list[str]
    source_file: str
    row_index: int
    source_type: str | None = None


@dataclass(frozen=True)
class EvalPrediction:
    intent: str
    predicted_actions: list[str]
    true_actions: list[str]
    domains: list[str]
    raw_response: str
    exact_match: bool
    partial_match: bool
    latency_seconds: float
    source_file: str
    row_index: int
    source_type: str | None = None


@dataclass(frozen=True)
class LabelMetrics:
    precision: float
    recall: float
    f1: float
    support: int


@dataclass(frozen=True)
class EvaluationSummary:
    total_samples: int
    exact_match_count: int
    exact_match_accuracy: float
    partial_match_count: int
    partial_match_accuracy: float
    hamming_loss: float
    micro_precision: float
    micro_recall: float
    micro_f1: float
    macro_precision: float
    macro_recall: float
    macro_f1: float
    single_action_samples: int
    multi_action_samples: int
    single_action_exact_match: float
    multi_action_exact_match: float
    multi_action_partial_match: float
    overpredict_rate: float
    underpredict_rate: float
    avg_extra_actions: float
    invalid_predictions: int
    invalid_rate: float
    use_true_domains: bool
    pipeline_domain_accuracy: float
    average_latency_seconds: float
    throughput_samples_per_second: float
    runtime_seconds: float
    per_label: dict[str, LabelMetrics]
    confusion_matrix: dict[str, dict[str, int]]
    per_class_accuracy: dict[str, float]
    run_id: str
    run_name: str
    experiment_name: str
    model_name: str
    data_root: str
    metrics_root: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["per_label"] = {label: asdict(metrics) for label, metrics in self.per_label.items()}
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


def load_action_eval_samples(
    data_root: Path | str,
    text_column_override: str | None = None,
    domains_column_override: str | None = None,
    actions_column_override: str | None = None,
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
        if domains_column is None or actions_column is None:
            skipped += len(rows)
            continue

        source_type_column = None
        normalized_fieldnames = {name.lower().strip() for name in fieldnames}
        for candidate in ("type", "source", "app", "system", "tool"):
            if candidate in normalized_fieldnames:
                source_type_column = next(name for name in fieldnames if name.lower().strip() == candidate)
                break

        for row_index, row in enumerate(rows, start=1):
            intent_text = row.get(text_column) if text_column is not None else None
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

            samples.append(
                EvalSample(
                    intent=str(intent_text).strip(),
                    domains=domains,
                    actions=actions,
                    source_file=file_path.name,
                    row_index=row_index,
                    source_type=row.get(source_type_column) if source_type_column else None,
                )
            )

    if not samples:
        raise ValueError(f"No labeled action samples were loaded from {root}")

    if skipped:
        print(f"  Skipped {skipped} rows (null intent, missing domains/actions, or unparseable actions)")

    return samples


def _safe_divide(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _multilabel_classification_metrics(
    true_labels_list: list[list[str]],
    predicted_labels_list: list[list[str]],
    labels: tuple[str, ...],
) -> dict[str, LabelMetrics]:
    metrics: dict[str, LabelMetrics] = {}
    for label in labels:
        tp = sum(1 for true_set, pred_set in zip(true_labels_list, predicted_labels_list) if label in true_set and label in pred_set)
        fp = sum(1 for true_set, pred_set in zip(true_labels_list, predicted_labels_list) if label not in true_set and label in pred_set)
        fn = sum(1 for true_set, pred_set in zip(true_labels_list, predicted_labels_list) if label in true_set and label not in pred_set)
        support = sum(1 for true_set in true_labels_list if label in true_set)
        precision = _safe_divide(tp, tp + fp)
        recall = _safe_divide(tp, tp + fn)
        f1 = _safe_divide(2.0 * precision * recall, precision + recall)
        metrics[label] = LabelMetrics(precision=precision, recall=recall, f1=f1, support=support)
    return metrics


def _multilabel_confusion_matrix(
    true_labels_list: list[list[str]],
    predicted_labels_list: list[list[str]],
    labels: tuple[str, ...],
) -> dict[str, dict[str, int]]:
    matrix = {label: {col: 0 for col in labels} for label in labels}
    for true_set, pred_set in zip(true_labels_list, predicted_labels_list):
        for t in true_set:
            if t not in matrix:
                continue
            for p in pred_set:
                if p in matrix[t]:
                    matrix[t][p] += 1
    return matrix


def _micro_average(metrics: dict[str, LabelMetrics], labels: tuple[str, ...]) -> tuple[float, float, float]:
    total_tp = sum(int(m.support * m.recall) for m in (metrics[l] for l in labels) if m.support > 0)
    total_fp = sum(int(m.support * (1 - m.precision) / m.precision) for m in (metrics[l] for l in labels) if m.precision > 0 and m.support > 0)
    total_fn = sum(int(m.support * (1 - m.recall)) for m in (metrics[l] for l in labels) if m.support > 0)
    precision = _safe_divide(total_tp, total_tp + total_fp)
    recall = _safe_divide(total_tp, total_tp + total_fn)
    f1 = _safe_divide(2.0 * precision * recall, precision + recall)
    return precision, recall, f1


def _macro_average(metrics: dict[str, LabelMetrics], labels: tuple[str, ...]) -> tuple[float, float, float]:
    if not labels:
        return 0.0, 0.0, 0.0
    precision = sum(metrics[label].precision for label in labels) / len(labels)
    recall = sum(metrics[label].recall for label in labels) / len(labels)
    f1 = sum(metrics[label].f1 for label in labels) / len(labels)
    return precision, recall, f1


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _write_predictions_csv(path: Path, predictions: list[EvalPrediction]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "row_index", "source_file", "source_type", "intent",
                "domains", "true_actions", "predicted_actions",
                "exact_match", "partial_match",
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
                "predicted_actions": ",".join(prediction.predicted_actions),
                "exact_match": prediction.exact_match,
                "partial_match": prediction.partial_match,
                "raw_response": prediction.raw_response,
                "latency_seconds": f"{prediction.latency_seconds:.3f}",
            })


def _slugify(value: str) -> str:
    cleaned = "".join(character.lower() if character.isalnum() else "-" for character in value)
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or "model"


def evaluate_action_classification(
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
    use_true_domains: bool = True,
    on_progress: Callable[[int, int, float], None] | None = None,
) -> EvaluationSummary:
    data_root_path = Path(data_root)
    metrics_root_path = Path(metrics_root)
    metrics_root_path.mkdir(parents=True, exist_ok=True)

    samples = load_action_eval_samples(data_root_path)
    agent = ActionClassifierAgent(model_client=model_client)
    domain_agent = DomainClassifierAgent(model_client=model_client)

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
            and run.data.params.get("use_true_domains") == str(use_true_domains)
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

        if use_true_domains:
            domains = sample.domains
        else:
            domains = domain_agent.predict(sample.intent).predicted_domains

        started_sample = time.perf_counter()
        model_prediction = agent.predict(sample.intent, domains=domains)
        latency_seconds = time.perf_counter() - started_sample

        pred_set = set(model_prediction.predicted_actions)
        true_set = set(sample.actions)
        return EvalPrediction(
            intent=sample.intent,
            predicted_actions=model_prediction.predicted_actions,
            true_actions=sample.actions,
            domains=model_prediction.domains,
            raw_response=model_prediction.raw_response,
            exact_match=(pred_set == true_set),
            partial_match=(true_set.issubset(pred_set)),
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
                    predicted_actions=[],
                    true_actions=sample.actions,
                    domains=sample.domains,
                    raw_response=f"ERROR: {exc}",
                    exact_match=False,
                    partial_match=False,
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
    true_labels_list = [p.true_actions for p in predictions]
    predicted_labels_list = [p.predicted_actions for p in predictions]
    latencies = [p.latency_seconds for p in predictions]
    per_label = _multilabel_classification_metrics(true_labels_list, predicted_labels_list, ACTION_LABELS)
    confusion = _multilabel_confusion_matrix(true_labels_list, predicted_labels_list, ACTION_LABELS)
    per_class_accuracy = {}
    for cls in ACTION_LABELS:
        tp = sum(1 for t, p in zip(true_labels_list, predicted_labels_list) if cls in t and cls in p)
        support = sum(1 for t in true_labels_list if cls in t)
        per_class_accuracy[cls] = _safe_divide(tp, support)
    macro_precision, macro_recall, macro_f1 = _macro_average(per_label, ACTION_LABELS)
    micro_precision, micro_recall, micro_f1 = _micro_average(per_label, ACTION_LABELS)

    exact_match_count = sum(1 for p in predictions if p.exact_match)
    partial_match_count = sum(1 for p in predictions if p.partial_match)
    exact_match_accuracy = _safe_divide(exact_match_count, total_samples)
    partial_match_accuracy = _safe_divide(partial_match_count, total_samples)

    total_labels = 0
    wrong_labels = 0
    for true_list, pred_list in zip(true_labels_list, predicted_labels_list):
        true_set = set(true_list)
        pred_set = set(pred_list)
        all_labels = true_set | pred_set
        total_labels += len(all_labels)
        wrong_labels += len(all_labels - (true_set & pred_set))
    hamming_loss = _safe_divide(wrong_labels, total_labels) if total_labels else 0.0

    single_samples = [p for p in predictions if len(p.true_actions) == 1]
    multi_samples = [p for p in predictions if len(p.true_actions) > 1]
    single_exact = _safe_divide(sum(1 for p in single_samples if p.exact_match), len(single_samples)) if single_samples else 0.0
    multi_exact = _safe_divide(sum(1 for p in multi_samples if p.exact_match), len(multi_samples)) if multi_samples else 0.0
    multi_partial = _safe_divide(sum(1 for p in multi_samples if p.partial_match), len(multi_samples)) if multi_samples else 0.0

    overpredict_count = 0
    underpredict_count = 0
    total_extra_labels = 0
    for true_list, pred_list in zip(true_labels_list, predicted_labels_list):
        true_set = set(true_list)
        pred_set = set(pred_list)
        extra = pred_set - true_set
        missing = true_set - pred_set
        if extra:
            overpredict_count += 1
            total_extra_labels += len(extra)
        if missing:
            underpredict_count += 1
    overpredict_rate = _safe_divide(overpredict_count, total_samples)
    underpredict_rate = _safe_divide(underpredict_count, total_samples)
    avg_extra_labels = _safe_divide(total_extra_labels, overpredict_count) if overpredict_count else 0.0

    invalid_predictions = sum(1 for p in predictions if not p.predicted_actions)
    invalid_rate = _safe_divide(invalid_predictions, total_samples)

    pipeline_domain_accuracy = 1.0
    if not use_true_domains:
        correct_domains = 0
        for sample, prediction in zip(samples, predictions):
            if set(sample.domains) == set(prediction.domains):
                correct_domains += 1
        pipeline_domain_accuracy = _safe_divide(correct_domains, total_samples)

    average_latency = statistics.mean(latencies) if latencies else 0.0
    throughput = _safe_divide(total_samples, runtime_seconds)

    summary = EvaluationSummary(
        total_samples=total_samples,
        exact_match_count=exact_match_count,
        exact_match_accuracy=exact_match_accuracy,
        partial_match_count=partial_match_count,
        partial_match_accuracy=partial_match_accuracy,
        hamming_loss=hamming_loss,
        micro_precision=micro_precision,
        micro_recall=micro_recall,
        micro_f1=micro_f1,
        macro_precision=macro_precision,
        macro_recall=macro_recall,
        macro_f1=macro_f1,
        single_action_samples=len(single_samples),
        multi_action_samples=len(multi_samples),
        single_action_exact_match=single_exact,
        multi_action_exact_match=multi_exact,
        multi_action_partial_match=multi_partial,
        overpredict_rate=overpredict_rate,
        underpredict_rate=underpredict_rate,
        avg_extra_actions=avg_extra_labels,
        invalid_predictions=invalid_predictions,
        invalid_rate=invalid_rate,
        use_true_domains=use_true_domains,
        pipeline_domain_accuracy=pipeline_domain_accuracy,
        average_latency_seconds=average_latency,
        throughput_samples_per_second=throughput,
        runtime_seconds=runtime_seconds,
        per_label=per_label,
        confusion_matrix=confusion,
        per_class_accuracy=per_class_accuracy,
        run_id=run_id,
        run_name=run_name_resolved,
        experiment_name=resolved_experiment_name,
        model_name=model_name,
        data_root=str(data_root_path),
        metrics_root=str(metrics_root_path),
    )

    _write_predictions_csv(run_root / "predictions.csv", predictions)
    _write_json(run_root / "summary.json", summary.to_dict())
    _write_json(run_root / "classification_report.json", {label: asdict(m) for label, m in per_label.items()})
    _write_json(run_root / "confusion_matrix.json", confusion)

    with mlflow.start_run(run_name=run_name_resolved):
        mlflow.set_tag("task", "action-classifier-evaluation")
        mlflow.set_tag("model_name", model_name)
        mlflow.set_tag("prompt_version", prompt_version)
        mlflow.set_tag("experiment_name", resolved_experiment_name)
        mlflow.set_tag("data_root", str(data_root_path))
        mlflow.set_tag("run_root", str(run_root))
        mlflow.set_tag("catalog_mode", "true-domains" if use_true_domains else "pipeline")

        mlflow.log_param("model_name", model_name)
        mlflow.log_param("prompt_version", prompt_version)
        mlflow.log_param("experiment_name", resolved_experiment_name)
        mlflow.log_param("data_root", str(data_root_path))
        mlflow.log_param("use_true_domains", use_true_domains)
        mlflow.log_param("domains", ",".join(ACTION_IDS_BY_DOMAIN.keys()))
        mlflow.log_param("action_count", len(ACTION_LABELS))
        mlflow.log_param("total_samples", total_samples)
        mlflow.log_param("single_action_samples", len(single_samples))
        mlflow.log_param("multi_action_samples", len(multi_samples))

        mlflow.log_metric("accuracy", exact_match_accuracy)
        mlflow.log_metric("precision", micro_precision)
        mlflow.log_metric("recall", micro_recall)
        mlflow.log_metric("f1", micro_f1)
        mlflow.log_metric("hamming_loss", hamming_loss)
        mlflow.log_metric("partial_match_accuracy", partial_match_accuracy)
        mlflow.log_metric("single_action_exact_match", single_exact)
        mlflow.log_metric("multi_action_exact_match", multi_exact)
        mlflow.log_metric("multi_action_partial_match", multi_partial)
        mlflow.log_metric("overpredict_rate", overpredict_rate)
        mlflow.log_metric("underpredict_rate", underpredict_rate)
        mlflow.log_metric("avg_extra_actions", avg_extra_labels)
        mlflow.log_metric("invalid_predictions", float(invalid_predictions))
        mlflow.log_metric("invalid_rate", invalid_rate)
        mlflow.log_metric("pipeline_domain_accuracy", pipeline_domain_accuracy)
        mlflow.log_metric("average_latency_seconds", average_latency)
        mlflow.log_metric("throughput_samples_per_second", throughput)
        mlflow.log_metric("runtime_seconds", runtime_seconds)

        for cls, acc in per_class_accuracy.items():
            if _is_plain_id(cls):
                mlflow.log_metric(f"accuracy_{cls}", acc)

        for label, metrics_obj in per_label.items():
            if not _is_plain_id(label):
                continue
            mlflow.log_metric(f"precision_{label}", metrics_obj.precision)
            mlflow.log_metric(f"recall_{label}", metrics_obj.recall)
            mlflow.log_metric(f"f1_{label}", metrics_obj.f1)
            mlflow.log_metric(f"support_{label}", float(metrics_obj.support))

        mlflow.log_artifacts(str(run_root))
        mlflow.log_dict(ACTION_SYSTEM_PROMPTS_BY_DOMAIN, "prompt/action_system_prompts_by_domain.json")

        if data_root_path.is_file():
            mlflow.log_artifact(str(data_root_path), artifact_path="dataset")
        else:
            mlflow.log_artifacts(str(data_root_path), artifact_path="dataset")

    print(f"\n  Experiment     : {resolved_experiment_name}")
    print(f"  Run            : {run_name_resolved}")
    print(f"  Prompt version : {prompt_version}")
    print(f"  Catalog mode   : {'true domains (oracle)' if use_true_domains else 'predicted domains (pipeline)'}")
    print(f"  MLflow DB      : {project_root / 'mlflow.db'}")
    print(f"  To view        : mlflow ui")
    print(f"\n  === Overall ===")
    print(f"  Accuracy       : {exact_match_accuracy:.1%}")
    print(f"  Precision      : {micro_precision:.1%}")
    print(f"  Recall         : {micro_recall:.1%}")
    print(f"  F1             : {micro_f1:.1%}")

    return summary


def _is_plain_id(value: str) -> bool:
    import re
    return bool(re.fullmatch(r"[a-z0-9]+(?:\.[a-z0-9_]+)+", value))


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate the action classifier against action test data.")
    parser.add_argument("--data-root", type=str, default=str(DEFAULT_DATA_ROOT), help="Folder or file containing evaluation data.")
    parser.add_argument("--metrics-root", type=str, default=str(DEFAULT_METRICS_ROOT), help="Where to store metrics and MLflow artifacts.")
    parser.add_argument("--experiment-name", type=str, default=None, help="Optional MLflow experiment name.")
    parser.add_argument("--model-name", type=str, required=True, help="OpenRouter model name.")
    parser.add_argument("--run-name", type=str, default=None, help="Optional MLflow run name.")
    parser.add_argument("--provider", type=str, default="openrouter", choices=("openrouter", "local"), help="Use OpenRouter or the local rule-based classifier.")
    parser.add_argument("--prompt-version", type=str, default="zeroshot", choices=("zeroshot",), help="System prompt version.")
    parser.add_argument("--pipeline", action="store_true", help="Run the full pipeline: predict domains first, then constrain actions to the predicted domains. Default: oracle (ground-truth domains).")
    parser.add_argument("--openrouter-api-key", type=str, default=None, help="OpenRouter API key.")
    parser.add_argument("--quiet", action="store_true", help="Disable per-row progress output.")
    parser.add_argument("--temperature", type=float, default=0.0, help="Sampling temperature.")
    parser.add_argument("--top-p", type=float, default=1.0, help="Top-p sampling parameter.")
    parser.add_argument("--max-tokens", type=int, default=256, help="Maximum tokens in the completion.")
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
        model_client = build_action_classifier_client(
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

    if args.pipeline:
        use_true_domains = False
    else:
        use_true_domains = True

    summary = evaluate_action_classification(
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
        use_true_domains=use_true_domains,
    )

    print(json.dumps(summary.to_dict(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
