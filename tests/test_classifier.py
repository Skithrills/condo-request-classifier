import math

import pytest
from pydantic import ValidationError

from condo_classifier.backends import ProviderError
from condo_classifier.evaluation import EVALUATION_PATH, read_rows
from condo_classifier.schema import Category, Prediction, Priority, ResidentRequest
from condo_classifier.service import Classifier
from condo_classifier.signals import multiple_topics, safety_signal


class FixedBackend:
    def __init__(self, **changes):
        self.calls = 0
        self.prediction = Prediction(
            **{
                "category": Category.MAINTENANCE,
                "confidence": 0.95,
                "confidence_kind": "uncalibrated_probability",
                "backend": "test",
                "model": "test",
                **changes,
            }
        )

    def predict(self, request):
        self.calls += 1
        return self.prediction


@pytest.mark.parametrize("text", ["", "   ", "???!", "x" * 4001])
def test_invalid_input_rejected(text):
    with pytest.raises(ValidationError):
        ResidentRequest(text=text)


@pytest.mark.parametrize(
    "text",
    [
        "There is a gas leak in Block A.",
        "My child is trapped in the lift.",
        "The electrical cabinet is on fire.",
        "Smoke is pouring from the basement.",
        "Someone is attacking a resident.",
        "No gas leak, but my child is trapped in the lift.",
        "What happens during a fire drill? The lobby is on fire now!",
    ],
)
def test_emergency_does_not_wait_for_backend(text):
    backend = FixedBackend()
    result = Classifier(backend).classify(ResidentRequest(text=text))
    assert result.priority == Priority.EMERGENCY
    assert result.review_required
    assert result.backend == "safety_rules"
    assert result.confidence is None
    assert backend.calls == 0


@pytest.mark.parametrize(
    "text",
    [
        "What should I do if someone is trapped in the lift?",
        "There is no gas leak.",
        "The bin is not on fire.",
        "Where can I read the fire evacuation plan?",
        "The gas leak was resolved yesterday.",
        "What happens during a fire drill?",
    ],
)
def test_hypothetical_and_negated_reports_not_emergencies(text):
    assert safety_signal(text) is None


def test_missing_location_and_low_confidence_require_review():
    result = Classifier(FixedBackend(confidence=0.2)).classify(
        ResidentRequest(text="The light is broken.")
    )
    assert result.review_required
    assert result.missing_details
    assert any("threshold" in reason for reason in result.review_reasons)


def test_explicit_location_is_preserved():
    result = Classifier(FixedBackend()).classify(
        ResidentRequest(text="The light is broken.", location="Block D level 3")
    )
    assert result.location == "Block D level 3"
    assert not result.review_required
    assert result.suggested_team == "Maintenance team"


def test_self_reported_llm_score_never_bypasses_review():
    result = Classifier(FixedBackend(confidence_kind="self_reported")).classify(
        ResidentRequest(text="The lobby light is broken.")
    )
    assert result.review_required
    assert any("not calibrated" in reason for reason in result.review_reasons)


def test_multiple_requests_are_flagged():
    text = "The tap is leaking. Also please send my payment receipt."
    assert multiple_topics(text)
    result = Classifier(FixedBackend()).classify(ResidentRequest(text=text, location="Block D"))
    assert any("multiple" in reason for reason in result.review_reasons)


def test_provider_failure_is_not_silently_replaced_with_baseline():
    class FailedBackend:
        def predict(self, request):
            raise ProviderError("Provider is unavailable.")

    result = Classifier(FailedBackend()).classify(ResidentRequest(text="My tap is leaking."))
    assert result.status == "provider_error"
    assert result.category == Category.OTHER
    assert result.review_required
    assert result.confidence is None


def test_sensitive_request_still_needs_review():
    backend = FixedBackend(category=Category.BILLING)
    result = Classifier(backend).classify(ResidentRequest(text="Approve my deposit refund."))
    assert result.review_required
    assert any("authorised person" in reason for reason in result.review_reasons)


def test_baseline_probability_contract(baseline):
    prediction = baseline.predict(ResidentRequest(text="The toilet tap is leaking."))
    assert set(prediction.category_scores) == set(Category)
    assert math.isclose(sum(prediction.category_scores.values()), 1.0)
    assert prediction.category == Category.MAINTENANCE
    assert prediction.confidence_kind == "uncalibrated_probability"


def test_all_categories_have_held_out_examples():
    assert {row["category"] for row in read_rows(EVALUATION_PATH)} == set(Category)
