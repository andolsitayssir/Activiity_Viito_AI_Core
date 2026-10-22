from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

from server.config import load_environment

load_environment()
os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")
mlflow = import_module("mlflow")

from server.agents.router import (
    ROUTING_LABELS,
    ROUTING_SYSTEM_PROMPT,
    ROUTING_SYSTEM_PROMPT_FR,
    RouterAgent,
    build_openrouter_client,
    normalize_label,
)

DEFAULT_DATA_ROOT = Path(__file__).resolve().parent / "test_data/routing_data_balanced_fr.xlsx"
DEFAULT_METRICS_ROOT = Path(__file__).resolve().parents[1] / "metrics"
DEFAULT_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "prompt_router_fr.md"
DEFAULT_EXPERIMENT_PREFIX = "routing-agent-fr"


XLSX_NAMESPACE = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NAMESPACE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/relationships"


@dataclass(frozen=True)
class EvalSample:
    intent: str
    label: str
    source_file: str
    row_index: int
    source_type: str | None = None


@dataclass(frozen=True)
class EvalPrediction:
    intent: str
    predicted_label: str
    raw_response: str
    correct: bool
    latency_seconds: float
    source_file: str
    row_index: int
    true_label: str
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
    correct_samples: int
    incorrect_samples: int
    accuracy: float
    balanced_accuracy: float
    macro_precision: float
    macro_recall: float
    macro_f1: float
    weighted_precision: float
    weighted_recall: float
    weighted_f1: float
    invalid_predictions: int
    invalid_rate: float
    average_latency_seconds: float
    median_latency_seconds: float
    min_latency_seconds: float
    max_latency_seconds: float
    throughput_samples_per_second: float
    runtime_seconds: float
    per_label: dict[str, LabelMetrics]
    confusion_matrix: dict[str, dict[str, int]]
    run_id: str
    run_name: str
    experiment_name: str
    model_name: str
    prompt_path: str
    data_root: str
    metrics_root: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["per_label"] = {label: asdict(metrics) for label, metrics in self.per_label.items()}
        return payload


def _column_name(index: int) -> str:
    name = ""
    current = index
    while current:
        current, remainder = divmod(current - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _column_index(name: str) -> int:
    value = 0
    for character in name:
        if character.isalpha():
            value = value * 26 + (ord(character.upper()) - 64)
    return value


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []

    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    namespace = {"a": XLSX_NAMESPACE}
    values: list[str] = []
    for item in root.findall("a:si", namespace):
        text_parts = [node.text or "" for node in item.findall(".//a:t", namespace)]
        values.append("".join(text_parts))
    return values


def _resolve_first_sheet_path(archive: zipfile.ZipFile) -> str:
    workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
    rels_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))

    rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels_root.findall(f"{{{PKG_REL_NAMESPACE}}}Relationship")}
    namespace = {"a": XLSX_NAMESPACE, "r": REL_NAMESPACE}
    first_sheet = workbook_root.find("a:sheets/a:sheet", namespace)
    if first_sheet is None:
        raise ValueError("XLSX workbook contains no sheets")

    rel_id = first_sheet.attrib.get(f"{{{REL_NAMESPACE}}}id")
    if rel_id is None or rel_id not in rel_map:
        raise ValueError("XLSX workbook is missing the first sheet relationship")

    target = rel_map[rel_id]
    if target.startswith("/"):
        return target.lstrip("/")
    if not target.startswith("xl/"):
        return f"xl/{target}"
    return target


def _read_xlsx_rows(file_path: Path) -> list[dict[str, str]]:
    with zipfile.ZipFile(file_path) as archive:
        shared_strings = _shared_strings(archive)
        sheet_path = _resolve_first_sheet_path(archive)
        sheet_root = ET.fromstring(archive.read(sheet_path))
        namespace = {"a": XLSX_NAMESPACE}

        raw_rows: list[dict[str, str]] = []
        for row in sheet_root.findall(".//a:sheetData/a:row", namespace):
            values_by_column: dict[str, str] = {}
            for cell in row.findall("a:c", namespace):
                reference = cell.attrib.get("r", "")
                column_name = "".join(ch for ch in reference if ch.isalpha())
                cell_type = cell.attrib.get("t")
                value_element = cell.find("a:v", namespace)
                inline_string = cell.find("a:is", namespace)

                value = ""
                if cell_type == "s" and value_element is not None and value_element.text is not None:
                    shared_index = int(value_element.text)
                    value = shared_strings[shared_index] if shared_index < len(shared_strings) else ""
                elif cell_type == "inlineStr" and inline_string is not None:
                    value = "".join(node.text or "" for node in inline_string.findall(".//a:t", namespace))
                elif value_element is not None and value_element.text is not None:
                    value = value_element.text

                values_by_column[column_name] = value.strip()
            raw_rows.append(values_by_column)

        if not raw_rows:
            return []

        header_row = raw_rows[0]
        ordered_headers: list[tuple[int, str]] = []
        for column_name, header_value in header_row.items():
            header_text = header_value.strip() if header_value else ""
            if not header_text:
                header_text = _column_name(_column_index(column_name))
            ordered_headers.append((_column_index(column_name), header_text))

        ordered_headers.sort(key=lambda item: item[0])
        headers = [header for _, header in ordered_headers]
        column_order = [_column_name(index) for index, _ in ordered_headers]

        parsed_rows: list[dict[str, str]] = []
        for raw_row in raw_rows[1:]:
            row_dict: dict[str, str] = {}
            for column_name, header_name in zip(column_order, headers):
                row_dict[header_name] = raw_row.get(column_name, "").strip()
            parsed_rows.append(row_dict)

        return parsed_rows


def _read_csv_rows(file_path: Path) -> list[dict[str, str]]:
    with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV file has no header row: {file_path}")
        return [
            {key.strip(): (value.strip() if isinstance(value, str) else "") for key, value in row.items() if key is not None}
            for row in reader
        ]


def discover_test_files(data_root: Path) -> list[Path]:
    if data_root.is_file():
        if data_root.suffix.lower() not in {".csv", ".xlsx"}:
            raise ValueError(f"Unsupported dataset file type: {data_root}")
        return [data_root]

    if not data_root.exists():
        raise FileNotFoundError(f"Dataset path does not exist: {data_root}")

    files = sorted(
        path
        for path in data_root.rglob("*")
        if path.is_file() and path.suffix.lower() in {".csv", ".xlsx"}
    )
    if not files:
        raise FileNotFoundError(f"No CSV or XLSX files were found under {data_root}")
    return files


def _first_text_value(row: dict[str, str], preferred_columns: Iterable[str], forbidden_columns: set[str]) -> str | None:
    for column in preferred_columns:
        value = row.get(column)
        if value:
            return value

    for key, value in row.items():
        if key in forbidden_columns:
            continue
        if value:
            return value
    return None


def _select_label_column(fieldnames: Iterable[str]) -> str | None:
    preferred = ("intent", "label", "target", "truth", "true_label", "expected", "route", "class", "intent_class", "category")
    normalized = {name.lower().strip(): name for name in fieldnames}
    for candidate in preferred:
        actual = normalized.get(candidate)
        if actual is not None:
            return actual
    return None


def _select_text_column(fieldnames: Iterable[str]) -> str | None:
    preferred = ("question", "text", "query", "utterance", "prompt", "input", "message", "sentence", "intent")
    normalized = {name.lower().strip(): name for name in fieldnames}
    for candidate in preferred:
        actual = normalized.get(candidate)
        if actual is not None:
            return actual
    return None


def load_eval_samples(data_root: Path | str) -> list[EvalSample]:
    root = Path(data_root)
    samples: list[EvalSample] = []

    for file_path in discover_test_files(root):
        rows = _read_csv_rows(file_path) if file_path.suffix.lower() == ".csv" else _read_xlsx_rows(file_path)
        if not rows:
            continue

        fieldnames = list(rows[0].keys())
        normalized_fieldnames = {name.lower().strip() for name in fieldnames}
        label_column = _select_label_column(fieldnames)
        text_column = _select_text_column(fieldnames)
        source_type_column = None
        for candidate in ("type", "domain", "source", "app", "system"):
            if candidate in normalized_fieldnames:
                source_type_column = next(name for name in fieldnames if name.lower().strip() == candidate)
                break

        for row_index, row in enumerate(rows, start=1):
            question_column = text_column or ("question" if "question" in normalized_fieldnames else None)
            intent_text = row.get(question_column) if question_column is not None else None
            if not intent_text:
                intent_text = _first_text_value(
                    row,
                    ("question", "text", "query", "utterance", "prompt", "input", "message", "sentence"),
                    {label_column or ""},
                )

            label_value = row.get(label_column) if label_column is not None else None
            if not label_value and "intent" in row:
                label_value = row.get("intent")

            normalized_label = normalize_label(label_value)
            if normalized_label is None:
                continue

            if intent_text is None or not str(intent_text).strip():
                intent_text = _first_text_value(row, tuple(row.keys()), {label_column or ""})
            if intent_text is None or not str(intent_text).strip():
                raise ValueError(f"Unable to find a question/intent text column in {file_path} row {row_index}")

            samples.append(
                EvalSample(
                    intent=str(intent_text).strip(),
                    label=normalized_label,
                    source_file=file_path.name,
                    row_index=row_index,
                    source_type=row.get(source_type_column) if source_type_column else None,
                )
            )

    if not samples:
        raise ValueError(f"No labeled samples were loaded from {root}")

    return samples


def _safe_divide(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _classification_metrics(true_labels: list[str], predicted_labels: list[str], labels: tuple[str, ...]) -> dict[str, LabelMetrics]:
    metrics: dict[str, LabelMetrics] = {}
    for label in labels:
        true_positive = sum(1 for truth, pred in zip(true_labels, predicted_labels) if truth == label and pred == label)
        false_positive = sum(1 for truth, pred in zip(true_labels, predicted_labels) if truth != label and pred == label)
        false_negative = sum(1 for truth, pred in zip(true_labels, predicted_labels) if truth == label and pred != label)
        support = sum(1 for truth in true_labels if truth == label)

        precision = _safe_divide(true_positive, true_positive + false_positive)
        recall = _safe_divide(true_positive, true_positive + false_negative)
        f1 = _safe_divide(2.0 * precision * recall, precision + recall)
        metrics[label] = LabelMetrics(precision=precision, recall=recall, f1=f1, support=support)
    return metrics


def _confusion_matrix(true_labels: list[str], predicted_labels: list[str], labels: tuple[str, ...]) -> dict[str, dict[str, int]]:
    all_predicted = sorted(set(predicted_labels) - set(labels))
    columns = list(labels) + all_predicted
    matrix = {label: {col: 0 for col in columns} for label in labels}
    for truth, pred in zip(true_labels, predicted_labels):
        matrix[truth][pred] += 1
    return matrix


def _macro_average(metrics: dict[str, LabelMetrics], labels: tuple[str, ...]) -> tuple[float, float, float]:
    if not labels:
        return 0.0, 0.0, 0.0
    precision = sum(metrics[label].precision for label in labels) / len(labels)
    recall = sum(metrics[label].recall for label in labels) / len(labels)
    f1 = sum(metrics[label].f1 for label in labels) / len(labels)
    return precision, recall, f1


def _weighted_average(metrics: dict[str, LabelMetrics], labels: tuple[str, ...]) -> tuple[float, float, float]:
    total_support = sum(metrics[label].support for label in labels)
    if total_support == 0:
        return 0.0, 0.0, 0.0
    precision = sum(metrics[label].precision * metrics[label].support for label in labels) / total_support
    recall = sum(metrics[label].recall * metrics[label].support for label in labels) / total_support
    f1 = sum(metrics[label].f1 * metrics[label].support for label in labels) / total_support
    return precision, recall, f1


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _write_predictions_csv(path: Path, predictions: list[EvalPrediction]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "row_index",
                "source_file",
                "source_type",
                "intent",
                "true_label",
                "predicted_label",
                "raw_response",
                "correct",
                "latency_seconds",
            ],
        )
        writer.writeheader()
        for prediction in predictions:
            writer.writerow(asdict(prediction))


def _slugify(value: str) -> str:
    cleaned = "".join(character.lower() if character.isalnum() else "-" for character in value)
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or "model"


def _resolve_experiment_name(requested: str | None, model_name: str, run_id: str) -> str:
    if requested:
        return requested
    return DEFAULT_EXPERIMENT_PREFIX


def evaluate_routing(
    data_root: Path | str = DEFAULT_DATA_ROOT,
    *,
    metrics_root: Path | str = DEFAULT_METRICS_ROOT,
    prompt_path: Path | str = DEFAULT_PROMPT_PATH,
    experiment_name: str | None = None,
    model_name: str = "rule-based",
    run_name: str | None = None,
    model_client: Any | None = None,
    show_progress: bool = True,
) -> EvaluationSummary:
    data_root_path = Path(data_root)
    metrics_root_path = Path(metrics_root)
    metrics_root_path.mkdir(parents=True, exist_ok=True)

    samples = load_eval_samples(data_root_path)
    agent = RouterAgent(model_client=model_client, prompt_path=prompt_path)

    run_name = run_name or f"routing-eval-{time.strftime('%Y%m%d-%H%M%S')}"
    run_id = uuid.uuid4().hex[:12]
    run_root = metrics_root_path / "runs" / f"{time.strftime('%Y%m%d-%H%M%S')}-{run_id}"
    run_root.mkdir(parents=True, exist_ok=True)

    resolved_experiment_name = _resolve_experiment_name(experiment_name, model_name, run_id)
    project_root = Path(__file__).resolve().parents[2]
    tracking_uri = f"sqlite:///{project_root / 'mlflow.db'}"
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(resolved_experiment_name)
    client = mlflow.MlflowClient()
    experiment = mlflow.get_experiment_by_name(resolved_experiment_name)
    existing_runs = client.search_runs(experiment_ids=[experiment.experiment_id])
    run_name = _slugify(model_name)
    for run in existing_runs:
        if (
            run.data.params.get("model_name") == model_name
            and run.data.params.get("prompt_path") == str(prompt_path)
            and run.data.params.get("data_root") == str(data_root_path)
        ):
            run_name = f"{_slugify(model_name)}-v2"
            version_counts = [
                r.info.run_name.split("-v")[-1]
                for r in existing_runs
                if r.info.run_name.startswith(f"{_slugify(model_name)}-v")
            ]
            if version_counts:
                run_name = f"{_slugify(model_name)}-v{max(int(v) for v in version_counts) + 1}"
            break

    predictions: list[EvalPrediction] = []
    true_labels: list[str] = []
    predicted_labels: list[str] = []
    latencies: list[float] = []

    started_at = time.perf_counter()
    total_samples = len(samples)

    resolved_experiment_name = _resolve_experiment_name(experiment_name, model_name, run_id)
    for index, sample in enumerate(samples, start=1):
        if index > 1:
            time.sleep(1)
        started_sample = time.perf_counter()
        model_prediction = agent.predict(sample.intent)
        latency_seconds = time.perf_counter() - started_sample
        predicted_label = model_prediction.predicted_label
        if predicted_label not in ROUTING_LABELS:
            predicted_label = "invalid"
        correct = predicted_label == sample.label

        predictions.append(
            EvalPrediction(
                intent=sample.intent,
                predicted_label=predicted_label,
                raw_response=model_prediction.raw_response,
                correct=correct,
                latency_seconds=latency_seconds,
                source_file=sample.source_file,
                row_index=sample.row_index,
                true_label=sample.label,
                source_type=sample.source_type,
            )
        )
        true_labels.append(sample.label)
        predicted_labels.append(predicted_label)
        latencies.append(latency_seconds)

        if show_progress:
            status = "✓" if correct else "✗"
            extra = f" | Raw: {model_prediction.raw_response[:80]}" if predicted_label == "invalid" else ""
            print(
                f"[{index}/{total_samples}] {status} Question: {sample.intent} | Prediction: {predicted_label} | Actual: {sample.label}{extra}",
                flush=True,
            )

    runtime_seconds = time.perf_counter() - started_at
    label_set = ROUTING_LABELS
    per_label = _classification_metrics(true_labels, predicted_labels, label_set)
    confusion = _confusion_matrix(true_labels, predicted_labels, label_set)
    macro_precision, macro_recall, macro_f1 = _macro_average(per_label, label_set)
    weighted_precision, weighted_recall, weighted_f1 = _weighted_average(per_label, label_set)
    accuracy = _safe_divide(sum(1 for prediction in predictions if prediction.correct), total_samples)
    invalid_predictions = sum(1 for prediction in predictions if prediction.predicted_label == "invalid")
    invalid_rate = _safe_divide(invalid_predictions, total_samples)
    balanced_accuracy = macro_recall
    average_latency = statistics.mean(latencies) if latencies else 0.0
    median_latency = statistics.median(latencies) if latencies else 0.0
    min_latency = min(latencies) if latencies else 0.0
    max_latency = max(latencies) if latencies else 0.0
    throughput = _safe_divide(total_samples, runtime_seconds)

    summary = EvaluationSummary(
        total_samples=total_samples,
        correct_samples=sum(1 for prediction in predictions if prediction.correct),
        incorrect_samples=sum(1 for prediction in predictions if not prediction.correct),
        accuracy=accuracy,
        balanced_accuracy=balanced_accuracy,
        macro_precision=macro_precision,
        macro_recall=macro_recall,
        macro_f1=macro_f1,
        weighted_precision=weighted_precision,
        weighted_recall=weighted_recall,
        weighted_f1=weighted_f1,
        invalid_predictions=invalid_predictions,
        invalid_rate=invalid_rate,
        average_latency_seconds=average_latency,
        median_latency_seconds=median_latency,
        min_latency_seconds=min_latency,
        max_latency_seconds=max_latency,
        throughput_samples_per_second=throughput,
        runtime_seconds=runtime_seconds,
        per_label=per_label,
        confusion_matrix=confusion,
        run_id=run_id,
        run_name=run_name,
        experiment_name=resolved_experiment_name,
        model_name=model_name,
        prompt_path=str(prompt_path),
        data_root=str(data_root_path),
        metrics_root=str(metrics_root_path),
    )

    _write_predictions_csv(run_root / "predictions.csv", predictions)
    _write_json(run_root / "summary.json", summary.to_dict())
    _write_json(run_root / "classification_report.json", {label: asdict(metrics) for label, metrics in per_label.items()})
    _write_json(run_root / "confusion_matrix.json", confusion)

    with mlflow.start_run(run_name=run_name):
        mlflow.set_tag("task", "routing-evaluation")
        mlflow.set_tag("model_name", model_name)
        mlflow.set_tag("experiment_name", resolved_experiment_name)
        mlflow.set_tag("data_root", str(data_root_path))
        mlflow.set_tag("prompt_path", str(prompt_path))
        mlflow.set_tag("run_root", str(run_root))
        mlflow.log_param("model_name", model_name)
        mlflow.log_param("experiment_name", resolved_experiment_name)
        mlflow.log_param("prompt_path", str(prompt_path))
        mlflow.log_param("data_root", str(data_root_path))
        mlflow.log_param("labels", ",".join(label_set))
        mlflow.log_metric("accuracy", accuracy)
        mlflow.log_metric("balanced_accuracy", balanced_accuracy)
        mlflow.log_metric("macro_precision", macro_precision)
        mlflow.log_metric("macro_recall", macro_recall)
        mlflow.log_metric("macro_f1", macro_f1)
        mlflow.log_metric("weighted_precision", weighted_precision)
        mlflow.log_metric("weighted_recall", weighted_recall)
        mlflow.log_metric("weighted_f1", weighted_f1)
        mlflow.log_metric("invalid_predictions", float(invalid_predictions))
        mlflow.log_metric("invalid_rate", invalid_rate)
        mlflow.log_metric("average_latency_seconds", average_latency)
        mlflow.log_metric("median_latency_seconds", median_latency)
        mlflow.log_metric("min_latency_seconds", min_latency)
        mlflow.log_metric("max_latency_seconds", max_latency)
        mlflow.log_metric("throughput_samples_per_second", throughput)
        mlflow.log_metric("runtime_seconds", runtime_seconds)
        for label, metrics in per_label.items():
            mlflow.log_metric(f"precision_{label}", metrics.precision)
            mlflow.log_metric(f"recall_{label}", metrics.recall)
            mlflow.log_metric(f"f1_{label}", metrics.f1)
            mlflow.log_metric(f"support_{label}", float(metrics.support))
        mlflow.log_artifacts(str(run_root))
        prompt_content = Path(prompt_path).read_text(encoding="utf-8")
        mlflow.log_text(prompt_content, "prompt/template.md")
        mlflow.log_text(ROUTING_SYSTEM_PROMPT, "prompt/system_prompt_en.md")
        mlflow.log_text(ROUTING_SYSTEM_PROMPT_FR, "prompt/system_prompt_fr.md")
        mlflow.log_artifacts(str(data_root_path), artifact_path="dataset")

    mlflow_db = project_root / 'mlflow.db'
    print(f"\n  Experiment : {resolved_experiment_name}")
    print(f"  Run        : {run_name}")
    print(f"  MLflow DB  : {mlflow_db}")
    print(f"  To view    : mlflow ui")

    return summary


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate the routing agent against the XLSX routing test data.")
    parser.add_argument("--data-root", type=str, default=str(DEFAULT_DATA_ROOT), help="Folder or file containing evaluation data.")
    parser.add_argument("--metrics-root", type=str, default=str(DEFAULT_METRICS_ROOT), help="Where to store metrics and MLflow artifacts.")
    parser.add_argument("--prompt-path", type=str, default=str(DEFAULT_PROMPT_PATH), help="Path to the routing prompt template.")
    parser.add_argument("--experiment-name", type=str, default=None, help="Optional MLflow experiment name. If omitted, a new experiment is created for each run.")
    parser.add_argument("--model-name", type=str, required=True, help="OpenRouter model name, for example qwen/qwen3-14b.")
    parser.add_argument("--run-name", type=str, default=None, help="Optional MLflow run name.")
    parser.add_argument("--provider", type=str, default="openrouter", choices=("openrouter", "local"), help="Use OpenRouter or the local rule-based router.")
    parser.add_argument("--openrouter-api-key", type=str, default=None, help="OpenRouter API key. If omitted, OPENROUTER_API_KEY is used.")
    parser.add_argument("--quiet", action="store_true", help="Disable per-row progress output.")
    parser.add_argument("--temperature", type=float, default=0.0, help="Sampling temperature.")
    parser.add_argument("--top-p", type=float, default=1.0, help="Top-p sampling parameter.")
    parser.add_argument("--max-tokens", type=int, default=64, help="Maximum tokens in the completion.")
    parser.add_argument("--frequency-penalty", type=float, default=0.0, help="Frequency penalty for the completion.")
    parser.add_argument("--presence-penalty", type=float, default=0.0, help="Presence penalty for the completion.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility.")
    parser.add_argument("--openrouter-base-url", type=str, default="https://openrouter.ai/api/v1", help="OpenRouter base URL.")
    parser.add_argument("--openrouter-site-url", type=str, default=None, help="OpenRouter site URL header.")
    parser.add_argument("--openrouter-app-name", type=str, default="activity-ai-engine", help="OpenRouter app name header.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    if args.provider == "local":
        model_client = None
        resolved_model_name = "rule-based"
    else:
        api_key = args.openrouter_api_key or os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENROUTER_API_KEY is required when provider=openrouter. Set it in .env or pass --openrouter-api-key."
            )
        model_client = build_openrouter_client(
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

    summary = evaluate_routing(
        data_root=args.data_root,
        metrics_root=args.metrics_root,
        prompt_path=args.prompt_path,
        experiment_name=args.experiment_name,
        model_name=resolved_model_name,
        run_name=args.run_name,
        model_client=model_client,
        show_progress=not args.quiet,
    )

    print(json.dumps(summary.to_dict(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())