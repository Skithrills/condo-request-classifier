import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from condo_classifier.replies import (
    BUTTON_BUSY,
    BUTTON_EXPIRED,
    EMERGENCY,
    LIMIT_CHOICES,
    LIMIT_REACHED,
)
from condo_classifier.service import Classifier
from condo_classifier.store import RequestStore

pytest.importorskip("telegram")
from condo_classifier.telegram_bot import ResidentBot, limit_keyboard  # noqa: E402

USER = SimpleNamespace(full_name="Jane Tan", username="janet", id=42)
STAFF = -100


class FakeChat:
    def __init__(self):
        self.calls = 0

    def reply(self, conversation, text, result):
        self.calls += 1
        return "Got it."


def make_bot(baseline, tmp_path, **kwargs):
    store = RequestStore(tmp_path / "requests.db")
    chat = FakeChat()
    return ResidentBot(Classifier(baseline), chat, STAFF, store, **kwargs), store, chat


def fake_bot():
    return SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=7)),
        edit_message_text=AsyncMock(),
        send_chat_action=AsyncMock(),
        set_message_reaction=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
        get_user_profile_photos=AsyncMock(return_value=SimpleNamespace(total_count=0)),
    )


def send(bot, tg, text, message_id=100):
    message = SimpleNamespace(
        text=text,
        message_id=message_id,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=message_id + 1)),
    )
    update = SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=1, type="private"),
        effective_user=USER,
    )
    asyncio.run(bot.text(update, SimpleNamespace(bot=tg)))
    return message.reply_text.call_args


def tap(bot, tg, choice):
    reply = AsyncMock(return_value=SimpleNamespace(message_id=500))
    query = SimpleNamespace(
        data=f"limit:{choice}",
        message=SimpleNamespace(chat=SimpleNamespace(id=1, type="private"), reply_text=reply),
        from_user=USER,
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    asyncio.run(bot.limit_button(SimpleNamespace(callback_query=query), SimpleNamespace(bot=tg)))
    return query, reply


def test_limit_shows_buttons_without_calling_models(baseline, tmp_path):
    bot, _, chat = make_bot(baseline, tmp_path, daily_limit=1)
    tg = fake_bot()
    send(bot, tg, "The corridor light keeps flickering")
    calls = chat.calls
    blocked = send(bot, tg, "The gym treadmill is broken", message_id=200)
    assert blocked.args[0] == LIMIT_REACHED
    assert blocked.kwargs["reply_markup"] is not None
    assert chat.calls == calls
    assert [m.text for m in bot.held_messages[USER.id]] == ["The gym treadmill is broken"]


def test_emergencies_are_never_blocked(baseline, tmp_path):
    bot, _, _ = make_bot(baseline, tmp_path, daily_limit=1)
    tg = fake_bot()
    send(bot, tg, "The corridor light keeps flickering")
    reply = send(bot, tg, "Smoke is coming from the bin chute on level 7!", message_id=200)
    assert "emergency number" in reply.args[0]


def test_repair_button_submits_held_messages_to_staff_and_dashboard(baseline, tmp_path):
    bot, store, _ = make_bot(baseline, tmp_path, daily_limit=1)
    tg = fake_bot()
    send(bot, tg, "Hello can you help")
    send(bot, tg, "The gym treadmill is broken", message_id=200)
    send(bot, tg, "It's in the gym", message_id=300)
    query, reply = tap(bot, tg, "repair")
    reply.assert_awaited_once_with(LIMIT_CHOICES["repair"].reply)
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
    card = tg.send_message.call_args.args[1]
    assert "Maintenance" in card and "treadmill" in card and "It's in the gym" in card
    [record] = [r for r in store.all() if r.messages[0] == "The gym treadmill is broken"]
    assert record.status == "submitted" and record.location == "gym"
    assert record.id in card
    # One menu sends one request.
    query, reply = tap(bot, tg, "repair")
    assert query.answer.call_args.args[0] == BUTTON_EXPIRED
    reply.assert_not_awaited()


def test_emergency_button_is_never_capped(baseline, tmp_path):
    bot, _, _ = make_bot(baseline, tmp_path, daily_limit=1)
    tg = fake_bot()
    send(bot, tg, "Hello can you help")
    for n in range(3):
        send(bot, tg, "Please call me back", message_id=200 + n)
        assert tap(bot, tg, "contact")[1].await_count == 1
    send(bot, tg, "Please call me back", message_id=300)
    query, reply = tap(bot, tg, "question")
    assert query.answer.call_args.args[0] == BUTTON_BUSY
    _, reply = tap(bot, tg, "emergency")
    reply.assert_awaited_once_with(EMERGENCY)
    assert "🚨 EMERGENCY" in tg.send_message.call_args.args[1]


def test_unknown_or_stale_button_expires(baseline, tmp_path):
    bot, _, _ = make_bot(baseline, tmp_path, daily_limit=1)
    tg = fake_bot()
    assert tap(bot, tg, "repair")[0].answer.call_args.args[0] == BUTTON_EXPIRED
    send(bot, tg, "Hello can you help")
    send(bot, tg, "Another message", message_id=200)
    assert tap(bot, tg, "approve_everything")[0].answer.call_args.args[0] == BUTTON_EXPIRED


def test_limit_resets_on_a_new_day(baseline, tmp_path):
    bot, _, _ = make_bot(baseline, tmp_path, daily_limit=1)
    tg = fake_bot()
    send(bot, tg, "The corridor light keeps flickering")
    assert send(bot, tg, "Still flickering", message_id=200).args[0] == LIMIT_REACHED
    bot.message_counts[USER.id] = (date(2000, 1, 1), 99)
    assert send(bot, tg, "Still flickering", message_id=300).args[0] != LIMIT_REACHED


def test_unlimited_ids_zero_limit_and_staff_commands(baseline, tmp_path):
    tg = fake_bot()
    bot, _, _ = make_bot(baseline, tmp_path, daily_limit=1, unlimited_ids={USER.id})
    for n in range(3):
        assert send(bot, tg, "Still flickering", message_id=100 * n).args[0] != LIMIT_REACHED
    bot, _, _ = make_bot(baseline, tmp_path, daily_limit=0)
    for n in range(3):
        assert send(bot, tg, "Still flickering", message_id=100 * n).args[0] != LIMIT_REACHED

    bot, _, _ = make_bot(baseline, tmp_path, daily_limit=1)
    send(bot, tg, "The corridor light keeps flickering")
    assert send(bot, tg, "Still flickering", message_id=200).args[0] == LIMIT_REACHED
    assert "Lifted" in bot.staff_limit_command(f"/unlimit {USER.id}")
    assert send(bot, tg, "Still flickering", message_id=300).args[0] != LIMIT_REACHED
    assert "back on" in bot.staff_limit_command(f"/relimit@condo_bot {USER.id}")
    send(bot, tg, "Still flickering", message_id=400)
    assert send(bot, tg, "Still flickering", message_id=500).args[0] == LIMIT_REACHED
    assert bot.staff_limit_command("/unlimit").startswith("Use /unlimit")


def test_limit_keyboard_has_every_choice():
    rows = limit_keyboard().inline_keyboard
    assert [button.callback_data for row in rows for button in row] == [
        f"limit:{key}" for key in LIMIT_CHOICES
    ]
