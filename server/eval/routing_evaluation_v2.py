from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Iterable
from xml.etree import ElementTree as ET

from server.config import load_environment

load_environment()
os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")
mlflow = import_module("mlflow")

from server.agents.router_v2 import (
    ROUTING_LABELS,
    ROUTING_SYSTEM_PROMPT_FR_FEWSHOT,
    ROUTING_SYSTEM_PROMPT_FR_ZEROSHOT,
    RouterAgent,
    build_openrouter_client,
    normalize_labels,
)

DEFAULT_DATA_ROOT = Path(__file__).resolve().parent / "test_data" / "routing_data_v3_fr_balanced_v2.xlsx"
DEFAULT_METRICS_ROOT = Path(__file__).resolve().parents[1] / "metrics"
DEFAULT_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "prompt_router_v3_fr_zs.md"
DEFAULT_EXPERIMENT_PREFIX = "routing-agent-test-zero-shot"

XLSX_NAMESPACE = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NAMESPACE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/relationships"


@dataclass(frozen=True)
class EvalSample:
    intent: str
    labels: list[str]
    source_file: str
    row_index: int
    source_type: str | None = None


@dataclass(frozen=True)
class EvalPrediction:
    intent: str
    predicted_labels: list[str]
    true_labels: list[str]
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
    single_label_samples: int
    multi_label_samples: int
    single_label_exact_match: float
    multi_label_exact_match: float
    multi_label_partial_match: float
    overpredict_rate: float
    underpredict_rate: float
    avg_extra_labels: float

    invalid_predictions: int
    invalid_rate: float
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
    prompt_path: str
    prompt_version: str
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


def _read_json_rows(file_path: Path) -> list[dict[str, str]]:
    raw = json.loads(file_path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        for key in ("data", "rows", "samples", "items", "examples", "records", "train", "test", "validation"):
            if key in raw and isinstance(raw[key], list) and raw[key] and isinstance(raw[key][0], dict):
                raw = raw[key]
                break
        else:
            raise ValueError(f"JSON dict must contain a list-of-objects under a key like 'data', 'rows', 'samples', etc.: {file_path}")
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"JSON file must contain a non-empty list of objects: {file_path}")
    if not isinstance(raw[0], dict):
        raise ValueError(f"JSON list items must be objects, got {type(raw[0]).__name__}: {file_path}")
    rows: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        rows.append({str(k): str(v).strip() if v is not None else "" for k, v in item.items()})
    return rows


def discover_test_files(data_root: Path) -> list[Path]:
    if data_root.is_file():
        if data_root.suffix.lower() not in {".csv", ".xlsx", ".json"}:
            raise ValueError(f"Unsupported dataset file type: {data_root}")
        return [data_root]
    if not data_root.exists():
        raise FileNotFoundError(f"Dataset path does not exist: {data_root}")
    files = sorted(
        path for path in data_root.rglob("*")
        if path.is_file() and path.suffix.lower() in {".csv", ".xlsx", ".json"}
    )
    if not files:
        raise FileNotFoundError(f"No CSV, XLSX, or JSON files were found under {data_root}")
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
    preferred = ("class", "label", "target", "truth", "true_label", "expected", "route", "intent_class", "category")
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


def load_eval_samples(data_root: Path | str, text_column_override: str | None = None, label_column_override: str | None = None) -> list[EvalSample]:
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
        normalized_fieldnames = {name.lower().strip() for name in fieldnames}

        if text_column_override and text_column_override in fieldnames:
            text_column = text_column_override
        else:
            text_column = _select_text_column(fieldnames)

        if label_column_override and label_column_override in fieldnames:
            label_column = label_column_override
        else:
            label_column = _select_label_column(fieldnames)
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

            normalized = normalize_labels(label_value)
            if not normalized:
                skipped += 1
                continue

            if intent_text is None or not str(intent_text).strip():
                intent_text = _first_text_value(row, tuple(row.keys()), {label_column or ""})
            if intent_text is None or not str(intent_text).strip():
                skipped += 1
                continue

            samples.append(
                EvalSample(
                    intent=str(intent_text).strip(),
                    labels=normalized,
                    source_file=file_path.name,
                    row_index=row_index,
                    source_type=row.get(source_type_column) if source_type_column else None,
                )
            )

    if not samples:
        raise ValueError(f"No labeled samples were loaded from {root}")

    if skipped:
        print(f"  Skipped {skipped} rows (null intent or unparseable labels)")

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
                "true_labels", "predicted_labels",
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
                "true_labels": ",".join(prediction.true_labels),
                "predicted_labels": ",".join(prediction.predicted_labels),
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
    prompt_version: str = "fewshot",
    request_delay: float = 0.3,
    max_workers: int = 8,
    text_column: str | None = None,
    label_column: str | None = None,
    on_progress: Callable[[int, int, float], None] | None = None,
) -> EvaluationSummary:
    data_root_path = Path(data_root)
    metrics_root_path = Path(metrics_root)
    metrics_root_path.mkdir(parents=True, exist_ok=True)

    samples = load_eval_samples(data_root_path, text_column, label_column)
    agent = RouterAgent(model_client=model_client, prompt_path=prompt_path)

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
            and run.data.params.get("data_root") == str(data_root_path)
            and run.data.params.get("prompt_version") == prompt_version
        ):
            run_name = f"{_slugify(model_name)}-{prompt_version}"
            version_counts = [
                r.info.run_name.split("-v")[-1]
                for r in existing_runs
                if r.info.run_name.startswith(f"{_slugify(model_name)}-{prompt_version}-v")
            ]
            if version_counts:
                run_name = f"{_slugify(model_name)}-{prompt_version}-v{max(int(v) for v in version_counts) + 1}"
            break

    total_samples = len(samples)
    started_at = time.perf_counter()

    def _predict_one(index: int, sample: EvalSample) -> EvalPrediction:
        if request_delay > 0:
            time.sleep(request_delay * (index % max_workers) / max_workers)
        started_sample = time.perf_counter()
        model_prediction = agent.predict(sample.intent)
        latency_seconds = time.perf_counter() - started_sample
        pred_set = set(model_prediction.predicted_labels)
        true_set = set(sample.labels)
        return EvalPrediction(
            intent=sample.intent,
            predicted_labels=model_prediction.predicted_labels,
            true_labels=sample.labels,
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
                    predicted_labels=["general_question"],
                    true_labels=sample.labels,
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
    true_labels_list = [p.true_labels for p in predictions]
    predicted_labels_list = [p.predicted_labels for p in predictions]
    latencies = [p.latency_seconds for p in predictions]
    label_set = ROUTING_LABELS
    per_label = _multilabel_classification_metrics(true_labels_list, predicted_labels_list, label_set)
    confusion = _multilabel_confusion_matrix(true_labels_list, predicted_labels_list, label_set)
    per_class_accuracy = {}
    for cls in label_set:
        tp = sum(1 for t, p in zip(true_labels_list, predicted_labels_list) if cls in t and cls in p)
        support = sum(1 for t in true_labels_list if cls in t)
        per_class_accuracy[cls] = _safe_divide(tp, support)
    macro_precision, macro_recall, macro_f1 = _macro_average(per_label, label_set)
    micro_precision, micro_recall, micro_f1 = _micro_average(per_label, label_set)

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

    single_samples = [p for p in predictions if len(p.true_labels) == 1]
    multi_samples = [p for p in predictions if len(p.true_labels) > 1]
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

    invalid_predictions = sum(1 for p in predictions if p.predicted_labels == ["invalid"])
    invalid_rate = _safe_divide(invalid_predictions, total_samples)
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
        single_label_samples=len(single_samples),
        multi_label_samples=len(multi_samples),
        single_label_exact_match=single_exact,
        multi_label_exact_match=multi_exact,
        multi_label_partial_match=multi_partial,
        overpredict_rate=overpredict_rate,
        underpredict_rate=underpredict_rate,
        avg_extra_labels=avg_extra_labels,
        invalid_predictions=invalid_predictions,
        invalid_rate=invalid_rate,
        average_latency_seconds=average_latency,
        throughput_samples_per_second=throughput,
        runtime_seconds=runtime_seconds,
        per_label=per_label,
        confusion_matrix=confusion,
        per_class_accuracy=per_class_accuracy,
        run_id=run_id,
        run_name=run_name,
        experiment_name=resolved_experiment_name,
        model_name=model_name,
        prompt_path=str(prompt_path),
        prompt_version=prompt_version,
        data_root=str(data_root_path),
        metrics_root=str(metrics_root_path),
    )

    _write_predictions_csv(run_root / "predictions.csv", predictions)
    _write_json(run_root / "summary.json", summary.to_dict())
    _write_json(run_root / "classification_report.json", {label: asdict(m) for label, m in per_label.items()})
    _write_json(run_root / "confusion_matrix.json", confusion)

    with mlflow.start_run(run_name=run_name):
        mlflow.set_tag("task", "routing-evaluation-v3")
        mlflow.set_tag("model_name", model_name)
        mlflow.set_tag("prompt_version", prompt_version)
        mlflow.set_tag("experiment_name", resolved_experiment_name)
        mlflow.set_tag("data_root", str(data_root_path))
        mlflow.set_tag("run_root", str(run_root))

        mlflow.log_param("model_name", model_name)
        mlflow.log_param("prompt_version", prompt_version)
        mlflow.log_param("experiment_name", resolved_experiment_name)
        mlflow.log_param("prompt_path", str(prompt_path))
        mlflow.log_param("data_root", str(data_root_path))
        mlflow.log_param("labels", ",".join(label_set))
        mlflow.log_param("total_samples", total_samples)
        mlflow.log_param("single_label_samples", len(single_samples))
        mlflow.log_param("multi_label_samples", len(multi_samples))

        # === Overall metrics ===
        mlflow.log_metric("accuracy", exact_match_accuracy)
        mlflow.log_metric("precision", micro_precision)
        mlflow.log_metric("recall", micro_recall)

        # === Multi-label metrics ===
        mlflow.log_metric("f1", micro_f1)
        mlflow.log_metric("hamming_loss", hamming_loss)
        mlflow.log_metric("partial_match_accuracy", partial_match_accuracy)

        # === Single/multi label breakdown ===
        mlflow.log_metric("single_label_exact_match", single_exact)
        mlflow.log_metric("multi_label_exact_match", multi_exact)
        mlflow.log_metric("multi_label_partial_match", multi_partial)

        # === Over/under prediction ===
        mlflow.log_metric("overpredict_rate", overpredict_rate)
        mlflow.log_metric("underpredict_rate", underpredict_rate)
        mlflow.log_metric("avg_extra_labels", avg_extra_labels)

        # === Invalid predictions ===
        mlflow.log_metric("invalid_predictions", float(invalid_predictions))
        mlflow.log_metric("invalid_rate", invalid_rate)

        # === Latency ===
        mlflow.log_metric("average_latency_seconds", average_latency)
        mlflow.log_metric("throughput_samples_per_second", throughput)
        mlflow.log_metric("runtime_seconds", runtime_seconds)

        # === Per-class exact match accuracy ===
        for cls, acc in per_class_accuracy.items():
            mlflow.log_metric(f"exact_match_accuracy_{cls}", acc)

        # === Per-label precision, recall, f1, support ===
        for label, metrics in per_label.items():
            mlflow.log_metric(f"precision_{label}", metrics.precision)
            mlflow.log_metric(f"recall_{label}", metrics.recall)
            mlflow.log_metric(f"f1_{label}", metrics.f1)
            mlflow.log_metric(f"support_{label}", float(metrics.support))

        # === Per-class accuracy ===
        for cls, acc in per_class_accuracy.items():
            mlflow.log_metric(f"accuracy_{cls}", acc)
        avg_acc = sum(per_class_accuracy.values()) / len(per_class_accuracy) if per_class_accuracy else 0.0
        mlflow.log_metric("average_accuracy", avg_acc)

        mlflow.log_artifacts(str(run_root))

        prompt_content = Path(prompt_path).read_text(encoding="utf-8")
        mlflow.log_text(prompt_content, "prompt/template.md")
        mlflow.log_text(ROUTING_SYSTEM_PROMPT_FR_ZEROSHOT, "prompt/system_prompt_fr_zs.md")
        mlflow.log_text(ROUTING_SYSTEM_PROMPT_FR_FEWSHOT, "prompt/system_prompt_fr_fs.md")

        if data_root_path.is_file():
            mlflow.log_artifact(str(data_root_path), artifact_path="dataset")
        else:
            mlflow.log_artifacts(str(data_root_path), artifact_path="dataset")

    print(f"\n  Experiment     : {resolved_experiment_name}")
    print(f"  Run            : {run_name}")
    print(f"  Prompt version : {prompt_version}")
    print(f"  MLflow DB      : {project_root / 'mlflow.db'}")
    print(f"  To view        : mlflow ui")
    print(f"\n  === Overall ===")
    print(f"  Accuracy       : {exact_match_accuracy:.1%}")
    print(f"  Precision      : {micro_precision:.1%}")
    print(f"  Recall         : {micro_recall:.1%}")
    print(f"  F1             : {micro_f1:.1%}")
    print(f"\n  === Per-class accuracy ===")
    for cls in ROUTING_LABELS:
        acc = per_class_accuracy.get(cls, 0.0)
        print(f"    {cls:20s} {acc:.1%}")
    avg_acc = sum(per_class_accuracy.values()) / len(per_class_accuracy) if per_class_accuracy else 0.0
    print(f"    {'AVERAGE':20s} {avg_acc:.1%}")

    return summary


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate the routing agent against the XLSX routing test data (v3 multi-label).")
    parser.add_argument("--data-root", type=str, default=str(DEFAULT_DATA_ROOT), help="Folder or file containing evaluation data.")
    parser.add_argument("--metrics-root", type=str, default=str(DEFAULT_METRICS_ROOT), help="Where to store metrics and MLflow artifacts.")
    parser.add_argument("--prompt-path", type=str, default=str(DEFAULT_PROMPT_PATH), help="Path to the routing prompt template.")
    parser.add_argument("--experiment-name", type=str, default=None, help="Optional MLflow experiment name.")
    parser.add_argument("--model-name", type=str, required=True, help="OpenRouter model name.")
    parser.add_argument("--run-name", type=str, default=None, help="Optional MLflow run name.")
    parser.add_argument("--provider", type=str, default="openrouter", choices=("openrouter", "local"), help="Use OpenRouter or the local rule-based router.")
    parser.add_argument("--prompt-version", type=str, default="fewshot", choices=("zeroshot", "fewshot"), help="System prompt version.")
    parser.add_argument("--openrouter-api-key", type=str, default=None, help="OpenRouter API key.")
    parser.add_argument("--quiet", action="store_true", help="Disable per-row progress output.")
    parser.add_argument("--temperature", type=float, default=0.0, help="Sampling temperature.")
    parser.add_argument("--top-p", type=float, default=1.0, help="Top-p sampling parameter.")
    parser.add_argument("--max-tokens", type=int, default=128, help="Maximum tokens in the completion.")
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
            prompt_version=args.prompt_version,
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
        prompt_version=args.prompt_version,
        request_delay=0.0 if is_local else 1.0,
        max_workers=args.max_workers,
    )

    print(json.dumps(summary.to_dict(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
