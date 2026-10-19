import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from condo_classifier.chat import ResidentMessage
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


def said(*texts):
    return [ResidentMessage(text, text) for text in texts]


def test_card_has_routing_details_and_resident_messages():
    card = staff_card(
        "R-ABC123", resident_label("Jane Tan", "janet", 42), result(), said("Hi", "Help")
    )
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
    assert staff_card("R-1", "Jane", result(**changes), said("x")).startswith(header)


def test_card_keeps_newest_text_within_telegram_limit():
    card = staff_card("R-1", "Jane", result(), said("old " * 1200, "newest message"))
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


def test_card_shows_language_translation_and_voice():
    messages = [
        ResidentMessage("5座电梯坏了", "The lift at Block 5 is broken", voice=True),
        ResidentMessage("#05-12", "#05-12"),
    ]
    card = staff_card("R-1", "Jane", result(), messages, language="zh")
    assert "Language: Chinese (translated to English below)" in card
    assert card.endswith("> 🎙️ 5座电梯坏了\n  EN: The lift at Block 5 is broken\n> #05-12")
    assert "could not be translated" in staff_card(
        "R-1", "Jane", result(), messages, translation_failed=True
    )


class FakeTranscriber:
    def __init__(self, text=None):
        self.text = text
        self.languages = []

    def transcribe(self, audio, language=None):
        from condo_classifier.voice import TranscriptionError

        if self.text is None:
            raise TranscriptionError("down")
        assert audio == b"ogg"
        self.languages.append(language)
        return self.text


def send_voice(bot, staff_bot, chat_id, duration=5):
    file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b"ogg")))
    voice = SimpleNamespace(
        duration=duration, file_id="voice-file-1", get_file=AsyncMock(return_value=file)
    )
    message = SimpleNamespace(voice=voice, reply_text=AsyncMock())
    update = SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=USER,
    )
    asyncio.run(bot.voice(update, SimpleNamespace(bot=staff_bot)))
    return [call.args[0] for call in message.reply_text.call_args_list]


def test_voice_note_is_echoed_classified_and_forwarded(baseline):
    bot = ResidentBot(
        Classifier(baseline),
        staff_chat_id=STAFF,
        transcriber=FakeTranscriber("The light keeps flickering outside #05-12"),
    )
    staff_bot = fake_bot()
    staff_bot.send_voice = AsyncMock()
    heard, reply = send_voice(bot, staff_bot, 1)
    assert heard.startswith("🎙️ I heard: The light keeps flickering outside #05-12")
    assert "/language" in heard
    assert "Maintenance" in reply
    assert bot.conversations[1].said[0].voice
    assert "> 🎙️ The light keeps flickering" in staff_bot.send_message.call_args.args[1]
    forwarded = staff_bot.send_voice.call_args
    assert forwarded.args == (STAFF, "voice-file-1")
    assert forwarded.kwargs["reply_parameters"].message_id == 7


def test_unclear_or_long_voice_notes_get_a_plain_answer(baseline):
    bot = ResidentBot(Classifier(baseline), staff_chat_id=STAFF, transcriber=FakeTranscriber())
    staff_bot = fake_bot()
    assert "couldn't make out" in send_voice(bot, staff_bot, 1)[0]
    assert "under 120 seconds" in send_voice(bot, staff_bot, 1, duration=300)[0]
    bot.transcriber = FakeTranscriber("should not be called")
    assert "couldn't make out" in send_voice(bot, staff_bot, 1, duration=0)[0]
    staff_bot.send_message.assert_not_awaited()
    assert 1 not in bot.conversations


def test_chosen_language_goes_to_whisper(baseline):
    transcriber = FakeTranscriber("Saya terperangkap dalam lif")
    bot = ResidentBot(Classifier(baseline), transcriber=transcriber)
    staff_bot = fake_bot()

    keyboard = SimpleNamespace(reply_text=AsyncMock())
    chat = SimpleNamespace(id=1)
    asyncio.run(
        bot.language(SimpleNamespace(effective_message=keyboard, effective_chat=chat), None)
    )
    buttons = keyboard.reply_text.call_args.kwargs["reply_markup"].inline_keyboard
    assert [row[0].callback_data for row in buttons] == [
        "lang:auto",
        "lang:en",
        "lang:zh",
        "lang:ms",
        "lang:ta",
    ]

    query = SimpleNamespace(data="lang:ms", answer=AsyncMock(), edit_message_text=AsyncMock())
    asyncio.run(bot.set_language(SimpleNamespace(callback_query=query, effective_chat=chat), None))
    assert "Bahasa Melayu" in query.edit_message_text.call_args.args[0]

    heard, _ = send_voice(bot, staff_bot, 1)
    assert transcriber.languages == ["ms"]
    assert "/language" not in heard

    query.data = "lang:auto"
    asyncio.run(bot.set_language(SimpleNamespace(callback_query=query, effective_chat=chat), None))
    send_voice(bot, staff_bot, 1)
    assert transcriber.languages == ["ms", None]
