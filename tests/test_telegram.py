import pytest

from condo_classifier.replies import INVALID, UNAVAILABLE, resident_reply
from condo_classifier.schema import Category, Classification, Priority
from condo_classifier.service import Classifier


def make_result(**changes):
    return Classification(
        **{
            "category": Category.MAINTENANCE,
            "priority": Priority.NORMAL,
            "suggested_team": "Maintenance team",
            "review_required": False,
            "review_reasons": [],
            "missing_details": [],
            "location": "#05-12",
            "confidence": 0.9,
            "confidence_kind": "uncalibrated_probability",
            "category_scores": {},
            "evidence": [],
            "backend": "test",
            "model": "test",
            "status": "classified",
            "elapsed_ms": 1.0,
            **changes,
        }
    )


def test_reply_names_category_team_and_location():
    reply = resident_reply(make_result())
    assert "Maintenance" in reply
    assert "Maintenance team" in reply
    assert "#05-12" in reply
    assert "approved" in reply


def test_reply_hides_internal_review_reasons_and_scores():
    reply = resident_reply(
        make_result(
            review_required=True,
            review_reasons=["Model score is below the review threshold."],
            missing_details=["Block, unit or shared-area location"],
            location=None,
            confidence=0.41,
        )
    )
    assert "threshold" not in reply
    assert "0.41" not in reply
    assert "- Block, unit or shared-area location" in reply
    assert "staff member will check" in reply


def test_emergency_reply_points_to_emergency_services():
    reply = resident_reply(make_result(priority=Priority.EMERGENCY, status="safety_escalation"))
    assert "emergency number" in reply
    assert "not an emergency service" in reply


def test_provider_error_reply():
    assert resident_reply(make_result(status="provider_error")) == UNAVAILABLE


def test_bot_classifies_text_end_to_end(baseline):
    pytest.importorskip("telegram")
    from condo_classifier.telegram_bot import ResidentBot

    bot = ResidentBot(Classifier(baseline))
    assert "Parking" in bot.reply_for("A car is blocking my lot in the basement carpark, B1-22")
    assert "emergency number" in bot.reply_for("Smoke is coming from the bin chute on level 7!")
    assert bot.reply_for("?!") == INVALID
