import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from condo_classifier.schema import Category, Classification, Priority
from condo_classifier.service import Classifier
from condo_classifier.staff import MAX_CARD_CHARS, resident_label, staff_card

telegram = pytest.importorskip("telegram")
from telegram.error import BadRequest, Forbidden  # noqa: E402

from condo_classifier.telegram_bot import ResidentBot  # noqa: E402

STAFF = -1001234
USER = SimpleNamespace(full_name="Jane Tan", username="janet", id=42)


def result(**changes):
    return Classification(
        **{
            "category": Category.MAINTENANCE,
            "priority": Priority.NORMAL,
            "suggested_team": "Maintenance team",
            "review_required": True,
            "review_reasons": ["Location is missing."],
            "missing_details": ["Block, unit or shared-area location"],
            "location": None,
            "confidence": 0.8,
            "confidence_kind": "uncalibrated_probability",
            "category_scores": {Category.MAINTENANCE: 0.8},
            "evidence": [],
            "backend": "test",
            "model": "test",
            "status": "needs_review",
            "elapsed_ms": 1.0,
            **changes,
        }
    )


def test_card_has_routing_details_and_resident_messages():
    card = staff_card("R-ABC123", resident_label("Jane Tan", "janet", 42), result(), ["Hi", "Help"])
    assert card.startswith("🆕 Request · R-ABC123")
    assert "From: Jane Tan @janet (Telegram ID 42)" in card
    assert "Team: Maintenance team" in card
    assert "Still missing: Block, unit or shared-area location" in card
    assert "- Location is missing." in card
    assert card.endswith("> Hi\n> Help")
    assert "0.8" not in card


@pytest.mark.parametrize(
    "changes,header",
    [
        ({"status": "safety_escalation", "priority": Priority.EMERGENCY}, "🚨 EMERGENCY"),
        ({"status": "provider_error"}, "⚠️ Could not classify"),
        ({"priority": Priority.HIGH}, "🔴 High priority"),
    ],
)
def test_card_headers(changes, header):
    assert staff_card("R-1", "Jane", result(**changes), ["x"]).startswith(header)


def test_card_keeps_newest_text_within_telegram_limit():
    card = staff_card("R-1", "Jane", result(), ["old " * 1200, "newest message"])
    assert len(card) <= MAX_CARD_CHARS
    assert card.endswith("> newest message")
    assert "…" in card


def fake_bot():
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=7)),
        edit_message_text=AsyncMock(),
        send_chat_action=AsyncMock(),
    )
    return bot


def run(bot, staff_bot, chat_id, text):
    message = SimpleNamespace(text=text, reply_text=AsyncMock())
    update = SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=USER,
    )
    asyncio.run(bot.text(update, SimpleNamespace(bot=staff_bot)))
    return message.reply_text.call_args.args[0]


def test_one_card_per_request_edited_as_details_arrive(baseline):
    bot = ResidentBot(Classifier(baseline), staff_chat_id=STAFF)
    staff_bot = fake_bot()
    run(bot, staff_bot, 1, "The light keeps flickering")
    staff_bot.send_message.assert_awaited_once()
    assert staff_bot.send_message.call_args.args[0] == STAFF
    request_id = bot.conversations[1].request_id
    assert request_id in staff_bot.send_message.call_args.args[1]

    run(bot, staff_bot, 1, "It's outside #05-12")
    staff_bot.send_message.assert_awaited_once()
    edit = staff_bot.edit_message_text.call_args
    assert edit.kwargs == {"chat_id": STAFF, "message_id": 7}
    assert "> It's outside #05-12" in edit.args[0]

    bot.conversations.pop(1)  # /new
    run(bot, staff_bot, 1, "Loud music next door at #08-21")
    assert staff_bot.send_message.await_count == 2
    assert bot.conversations[1].request_id != request_id


def test_invalid_message_does_not_touch_staff_card(baseline):
    bot = ResidentBot(Classifier(baseline), staff_chat_id=STAFF)
    staff_bot = fake_bot()
    run(bot, staff_bot, 1, "The light keeps flickering")
    run(bot, staff_bot, 1, "?!")
    staff_bot.send_message.assert_awaited_once()
    staff_bot.edit_message_text.assert_not_awaited()


def test_emergency_posts_a_fresh_alert_once(baseline):
    bot = ResidentBot(Classifier(baseline), staff_chat_id=STAFF)
    staff_bot = fake_bot()
    run(bot, staff_bot, 1, "The bin chute door is broken on level 7")
    run(bot, staff_bot, 1, "Now smoke is coming from the bin chute!")
    assert staff_bot.send_message.await_count == 2
    assert staff_bot.send_message.call_args.args[1].startswith("🚨 EMERGENCY")
    run(bot, staff_bot, 1, "Block 3")
    assert staff_bot.send_message.await_count == 2
    staff_bot.edit_message_text.assert_awaited_once()


def test_unchanged_or_deleted_card(baseline):
    bot = ResidentBot(Classifier(baseline), staff_chat_id=STAFF)
    staff_bot = fake_bot()
    run(bot, staff_bot, 1, "The light keeps flickering")
    staff_bot.edit_message_text.side_effect = BadRequest("Message is not modified")
    run(bot, staff_bot, 1, "The light keeps flickering")
    assert staff_bot.send_message.await_count == 1
    staff_bot.edit_message_text.side_effect = BadRequest("Message to edit not found")
    run(bot, staff_bot, 1, "Block 2")
    assert staff_bot.send_message.await_count == 2


def test_staff_chat_failure_never_blocks_the_resident(baseline):
    bot = ResidentBot(Classifier(baseline), staff_chat_id=STAFF)
    staff_bot = fake_bot()
    staff_bot.send_message.side_effect = Forbidden("bot was kicked")
    assert "Maintenance" in run(bot, staff_bot, 1, "The light keeps flickering")


def test_no_staff_chat_configured(baseline):
    bot = ResidentBot(Classifier(baseline))
    staff_bot = fake_bot()
    run(bot, staff_bot, 1, "The light keeps flickering")
    staff_bot.send_message.assert_not_awaited()


def test_invalid_staff_chat_id_rejected(monkeypatch):
    from condo_classifier.telegram_bot import build_bot

    monkeypatch.setenv("BOT_REPLIES", "fixed")
    monkeypatch.setenv("CLASSIFIER_BACKEND", "baseline")
    monkeypatch.setenv("STAFF_CHAT_ID", "@staffgroup")
    with pytest.raises(SystemExit, match="STAFF_CHAT_ID must be a number"):
        build_bot()
