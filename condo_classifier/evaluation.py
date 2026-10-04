import csv
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

from sklearn.metrics import accuracy_score, classification_report, confusion_matrix, f1_score

from condo_classifier.backends import ROOT, TRAIN_PATH
from condo_classifier.schema import Category, ResidentRequest
from condo_classifier.service import Classifier

EVALUATION_PATH = ROOT / "data" / "evaluation.csv"


def read_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def evaluate(classifier: Classifier, path: Path = EVALUATION_PATH) -> dict:
    rows = read_rows(path)
    training_texts = {row["text"].strip().casefold() for row in read_rows(TRAIN_PATH)}
    if any(row["text"].strip().casefold() in training_texts for row in rows):
        raise ValueError("Evaluation text overlaps with the training split.")
    if not rows:
        raise ValueError("Evaluation data is empty.")
    results = [classifier.classify(ResidentRequest(text=row["text"])) for row in rows]
    labels = [category.value for category in Category]
    expected = [Category(row["category"]).value for row in rows]
    predicted = [result.category.value for result in results]
    emergency_indices = [i for i, row in enumerate(rows) if row.get("priority") == "emergency"]
    accepted_indices = [i for i, result in enumerate(results) if not result.review_required]
    probabilities = [result for result in results if result.category_scores]
    failures = sum(result.status == "provider_error" for result in results)
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "dataset": str(path.name),
        "dataset_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "training_sha256": hashlib.sha256(TRAIN_PATH.read_bytes()).hexdigest(),
        "data_provenance": "Hand-authored synthetic English examples; not production evidence.",
        "rows": len(rows),
        "backends": dict(Counter(result.backend for result in results)),
        "models": sorted({result.model for result in results}),
        "review_threshold": classifier.review_threshold,
        "category_accuracy": accuracy_score(expected, predicted),
        "macro_f1": f1_score(expected, predicted, labels=labels, average="macro", zero_division=0),
        "priority_accuracy": mean(
            row["priority"] == result.priority.value
            for row, result in zip(rows, results, strict=True)
        ),
        "review_rate": mean(result.review_required for result in results),
        "accepted_count": len(accepted_indices),
        "accepted_accuracy": (
            mean(expected[i] == predicted[i] for i in accepted_indices)
            if accepted_indices
            else None
        ),
        "emergency_recall": (
            mean(results[i].priority.value == "emergency" for i in emergency_indices)
            if emergency_indices
            else None
        ),
        "provider_errors": failures,
        "mean_latency_ms": mean(result.elapsed_ms for result in results),
        "probability_rows": len(probabilities),
        "labels": labels,
        "confusion_matrix": confusion_matrix(expected, predicted, labels=labels).tolist(),
        "per_category": classification_report(
            expected,
            predicted,
            labels=labels,
            output_dict=True,
            zero_division=0,
        ),
        "predictions": [
            {
                "text": row["text"],
                "expected_category": row["category"],
                "expected_priority": row["priority"],
                **result.model_dump(mode="json"),
            }
            for row, result in zip(rows, results, strict=True)
        ],
    }
    return report


def save_report(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
