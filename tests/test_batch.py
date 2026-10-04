import csv
import io

import pytest

from condo_classifier.batch import classify_batch, export_csv, parse_batch
from condo_classifier.service import Classifier


@pytest.mark.parametrize(
    "content",
    [
        b"message\nhello",
        b"text\n",
        b"text,text\na,b",
        b"text\na,b",
        b"text\n\xff",
        b"text\n" + b"test\n" * 101,
    ],
)
def test_bad_csv_rejected(content):
    with pytest.raises(ValueError):
        parse_batch(content)


def test_csv_quotes_bom_and_optional_fields():
    rows = parse_batch('\ufefftext,channel\n"Please help, the lift is stuck",WhatsApp'.encode())
    assert rows[0]["channel"] == "WhatsApp"
    assert "," in rows[0]["text"]


def test_invalid_row_does_not_hide_valid_rows(baseline):
    rows = [
        {"text": ""},
        {"text": "The lobby tap is leaking."},
        {"text": "Help me", "channel": "Unsupported"},
    ]
    results = classify_batch(Classifier(baseline), rows)
    assert results[0]["status"] == "invalid_input"
    assert results[1]["category"] == "maintenance"
    assert results[2]["status"] == "invalid_input"


@pytest.mark.parametrize("text", ["=1+1", "+SUM(A1)", "@SUM(A1)", " \t=HYPERLINK(1)"])
def test_csv_formula_cells_are_escaped(text):
    output = export_csv([{"text": text}])
    assert list(csv.DictReader(io.StringIO(output)))[0]["text"] == "'" + text
