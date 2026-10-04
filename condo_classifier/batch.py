import csv
import io
import json

from pydantic import ValidationError

from condo_classifier.schema import ResidentRequest
from condo_classifier.service import Classifier

MAX_BATCH_ROWS = 100


def parse_batch(content: bytes) -> list[dict[str, str]]:
    if len(content) > 2_000_000:
        raise ValueError("Use a CSV smaller than 2 MB.")
    try:
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")), strict=True)
        if not reader.fieldnames or "text" not in reader.fieldnames:
            raise ValueError("CSV must contain a text column; channel and location are optional.")
        if len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError("CSV has duplicate column names.")
        rows = []
        for row in reader:
            if None in row:
                raise ValueError(
                    "A CSV row has extra fields. Put messages containing commas in quotes."
                )
            rows.append(row)
            if len(rows) > MAX_BATCH_ROWS:
                raise ValueError(f"Use at most {MAX_BATCH_ROWS} requests per batch.")
        if not rows:
            raise ValueError("The CSV contains no requests.")
        return rows
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ValueError("Could not read the CSV. Save it as UTF-8 with a header row.") from exc


def classify_batch(classifier: Classifier, rows: list[dict]) -> list[dict]:
    if len(rows) > MAX_BATCH_ROWS:
        raise ValueError(f"Use at most {MAX_BATCH_ROWS} requests per batch.")
    results = []
    for index, row in enumerate(rows, 1):
        try:
            request = ResidentRequest(
                text=row.get("text") or "",
                channel=row.get("channel") or "Web",
                location=row.get("location") or None,
            )
        except ValidationError:
            results.append(
                {
                    "row": index,
                    "text": row.get("text", ""),
                    "status": "invalid_input",
                    "review_required": True,
                    "review_reasons": [
                        "Check message length (3-4000), channel and location (up to 120)."
                    ],
                }
            )
            continue
        results.append(
            {
                "row": index,
                "text": request.text,
                **classifier.classify(request).model_dump(mode="json"),
            }
        )
    return results


def spreadsheet_cell(value):
    """Escape formula-like text for both our export and Streamlit's table download."""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def export_csv(rows: list[dict]) -> str:
    fields = list(dict.fromkeys(key for row in rows for key in row))
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: spreadsheet_cell(value) for key, value in row.items()})
    return output.getvalue()
