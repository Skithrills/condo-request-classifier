from datetime import date

import pytest

from condo_classifier.replies import INVALID, LIMIT_REACHED, UNAVAILABLE, resident_reply
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


def bot_with(classifier, chat=None, **options):
    pytest.importorskip("telegram")
    from condo_classifier.telegram_bot import ResidentBot

    return ResidentBot(classifier, chat, **options)


def test_fixed_replies_end_to_end(baseline):
    bot = bot_with(Classifier(baseline))
    assert "Parking" in bot.reply_for(1, "A car is blocking my lot in the basement carpark, B1-22")
    assert "emergency number" in bot.reply_for(2, "Smoke is coming from the bin chute on level 7!")
    assert bot.reply_for(3, "?!") == INVALID


def test_later_messages_fill_in_missing_location(baseline):
    bot = bot_with(Classifier(baseline))
    first = bot.reply_for(1, "The light keeps flickering")
    assert "Block, unit or shared-area location" in first
    bot.reply_for(1, "It's outside #05-12")
    assert bot.conversations[1].request_text() == (
        "The light keeps flickering\nIt's outside #05-12"
    )
    bot.conversations.clear()
    assert "#05-12" not in bot.reply_for(1, "The light keeps flickering")


class FakeChat:
    def __init__(self, reply="Got it! Which block is that in?"):
        self.calls = []
        self.answer = reply

    def reply(self, conversation, text, result):
        self.calls.append((text, result))
        return self.answer


def test_chat_writes_normal_replies(baseline):
    chat = FakeChat()
    bot = bot_with(Classifier(baseline), chat)
    assert bot.reply_for(1, "The light keeps flickering") == chat.answer
    assert chat.calls[0][1].category == Category.MAINTENANCE


def test_emergency_uses_fixed_text_then_chat(baseline):
    chat = FakeChat()
    bot = bot_with(Classifier(baseline), chat)
    assert "emergency number" in bot.reply_for(1, "Smoke is coming from the bin chute!")
    assert chat.calls == []
    assert bot.conversations[1].escalated
    assert bot.reply_for(1, "Block 7, level 3") == chat.answer


def test_chat_failure_falls_back_to_fixed_reply(baseline):
    bot = bot_with(Classifier(baseline), FakeChat(reply=None))
    reply = bot.reply_for(1, "A car is blocking my lot in the basement carpark, B1-22")
    assert "Parking" in reply
    assert bot.conversations[1].messages[-1] == {"role": "assistant", "content": reply}


def test_idle_conversation_resets(baseline, monkeypatch):
    bot = bot_with(Classifier(baseline))
    bot.reply_for(1, "The light keeps flickering")
    bot.conversations[1].last_seen -= 3 * 60 * 60
    bot.reply_for(1, "Loud music next door")
    assert bot.conversations[1].resident_texts == ["Loud music next door"]


def test_daily_limit_blocks_extra_messages_but_not_emergencies(baseline):
    bot = bot_with(Classifier(baseline), daily_limit=2)
    bot.reply_for(1, "The light keeps flickering")
    bot.reply_for(1, "It's outside #05-12")
    assert bot.reply_for(1, "Also the lift is slow") == LIMIT_REACHED
    assert bot.conversations[1].resident_texts == [
        "The light keeps flickering",
        "It's outside #05-12",
    ]
    assert "emergency number" in bot.reply_for(1, "Smoke is coming from the bin chute!")
    assert bot.reply_for(2, "Loud music next door") != LIMIT_REACHED


def test_daily_limit_resets_on_a_new_day(baseline):
    bot = bot_with(Classifier(baseline), daily_limit=1)
    bot.reply_for(1, "The light keeps flickering")
    assert bot.reply_for(1, "Still flickering") == LIMIT_REACHED
    bot.message_counts[1] = (date(2000, 1, 1), 99)
    assert bot.reply_for(1, "Still flickering") != LIMIT_REACHED


def test_unlimited_ids_and_zero_limit_skip_the_limit(baseline):
    bot = bot_with(Classifier(baseline), daily_limit=1, unlimited_ids={7})
    for _ in range(3):
        assert bot.reply_for(7, "The light keeps flickering") != LIMIT_REACHED
    bot = bot_with(Classifier(baseline), daily_limit=0)
    for _ in range(3):
        assert bot.reply_for(1, "The light keeps flickering") != LIMIT_REACHED


def test_staff_can_lift_and_restore_a_limit(baseline):
    bot = bot_with(Classifier(baseline), daily_limit=1)
    bot.reply_for(5, "The light keeps flickering")
    assert bot.reply_for(5, "Still flickering") == LIMIT_REACHED
    assert "Lifted" in bot.staff_limit_command("/unlimit 5")
    assert bot.reply_for(5, "Still flickering") != LIMIT_REACHED
    assert "back on the daily limit" in bot.staff_limit_command("/relimit@condo_bot 5")
    bot.reply_for(5, "Still flickering")
    assert bot.reply_for(5, "Still flickering") == LIMIT_REACHED
    assert bot.staff_limit_command("/unlimit").startswith("Use /unlimit")


@pytest.mark.parametrize(
    "env,message",
    [
        ({"BOT_REPLIES": "chat"}, "ANTHROPIC_API_KEY"),
        ({"BOT_REPLIES": "fixed", "CLASSIFIER_BACKEND": "claude"}, "ANTHROPIC_API_KEY"),
        ({"BOT_REPLIES": "loud"}, "chat or fixed"),
        ({"BOT_REPLIES": "fixed", "CLASSIFIER_BACKEND": "gpt"}, "Check .env"),
        ({"BOT_REPLIES": "fixed", "DAILY_MESSAGE_LIMIT": "lots"}, "DAILY_MESSAGE_LIMIT"),
        ({"BOT_REPLIES": "fixed", "BOT_UNLIMITED_IDS": "me"}, "BOT_UNLIMITED_IDS"),
    ],
)
def test_build_bot_explains_missing_setup(monkeypatch, env, message):
    pytest.importorskip("telegram")
    from condo_classifier.telegram_bot import build_bot

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLASSIFIER_BACKEND", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(SystemExit, match=message):
        build_bot()
