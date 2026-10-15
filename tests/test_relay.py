import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from condo_classifier.service import Classifier
from condo_classifier.store import RequestStore

pytest.importorskip("telegram")
from telegram.error import Forbidden  # noqa: E402

from condo_classifier.telegram_bot import ResidentBot  # noqa: E402

STAFF = -100
BOT_ID = 99
RESIDENT = SimpleNamespace(id=42, full_name="Jane Tan", username="janet", is_bot=False)
SARAH = SimpleNamespace(id=7, first_name="Sarah", full_name="Sarah Lee", is_bot=False)


class FakeChat:
    def __init__(self):
        self.calls = 0

    def reply(self, conversation, text, result):
        self.calls += 1
        return "Got it."


def fake_tg():
    counter = iter(range(1000, 2000))
    sent = lambda *a, **k: SimpleNamespace(message_id=next(counter))  # noqa: E731
    return SimpleNamespace(
        id=BOT_ID,
        username="condo_bot",
        send_message=AsyncMock(side_effect=sent),
        send_photo=AsyncMock(side_effect=sent),
        copy_message=AsyncMock(side_effect=sent),
        edit_message_text=AsyncMock(),
        send_chat_action=AsyncMock(),
        set_message_reaction=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )


def make_bot(baseline, tmp_path):
    store = RequestStore(tmp_path / "requests.db")
    chat = FakeChat()
    return ResidentBot(Classifier(baseline), chat, STAFF, store), store, chat


def resident_says(bot, tg, text, message_id):
    message = SimpleNamespace(
        text=text,
        message_id=message_id,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=message_id + 1)),
    )
    update = SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=42, type="private"),
        effective_user=RESIDENT,
    )
    asyncio.run(bot.text(update, SimpleNamespace(bot=tg)))
    return message.reply_text


def staff_says(bot, tg, text, reply_to_id, reply_to_text="", handler=None, sender=BOT_ID):
    replied = SimpleNamespace(
        message_id=reply_to_id,
        from_user=SimpleNamespace(id=sender),
        text=reply_to_text,
        caption=None,
    )
    message = SimpleNamespace(
        text=text,
        message_id=5000,
        from_user=SARAH,
        reply_to_message=replied,
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=STAFF, type="supergroup"),
        effective_user=SARAH,
    )
    asyncio.run((handler or bot.staff_reply)(update, SimpleNamespace(bot=tg)))
    return message.reply_text


def human_command(bot, tg):
    message = SimpleNamespace(reply_text=AsyncMock(return_value=SimpleNamespace(message_id=600)))
    update = SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=42, type="private"),
        effective_user=RESIDENT,
    )
    asyncio.run(bot.human(update, SimpleNamespace(bot=tg)))


def card_id(bot):
    return bot.conversations[42].staff_message_id


def test_staff_reply_reaches_resident_and_takes_over(baseline, tmp_path):
    bot, store, chat = make_bot(baseline, tmp_path)
    tg = fake_tg()
    resident_says(bot, tg, "The corridor light keeps flickering", 100)
    human_command(bot, tg)
    request_id = bot.conversations[42].request_id

    staff_says(bot, tg, "Hi Jane, which floor is this?", card_id(bot))
    to_resident = tg.send_message.call_args
    assert to_resident.args == (42, "👤 Sarah (staff): Hi Jane, which floor is this?")
    assert tg.set_message_reaction.call_args.args == (STAFF, 5000, "👍")
    assert store.get(request_id).status == "with_staff"
    assert tg.edit_message_text.call_args.args[0].startswith("👤 With staff")

    # The AI stays quiet; the resident's message goes to staff under the card.
    calls = chat.calls
    reply = resident_says(bot, tg, "Level 5, near the lift", 110)
    reply.assert_not_awaited()
    assert chat.calls == calls
    forwarded = tg.send_message.call_args
    assert forwarded.args == (STAFF, f"💬 {request_id} · Jane Tan @janet: Level 5, near the lift")
    assert forwarded.kwargs["reply_to_message_id"] == card_id(bot)
    assert "Level 5, near the lift" in store.get(request_id).messages

    # Staff can also reply to the forwarded message, not just the card.
    forwarded_id = max(bot.staff_threads)
    staff_says(bot, tg, "Thanks, sending someone", forwarded_id)
    assert tg.send_message.call_args.args[1] == "👤 Sarah (staff): Thanks, sending someone"


def test_done_hands_back_to_the_ai(baseline, tmp_path):
    bot, store, chat = make_bot(baseline, tmp_path)
    tg = fake_tg()
    resident_says(bot, tg, "Loud music from #08-21 every night", 100)
    request_id = bot.conversations[42].request_id
    card = card_id(bot)
    staff_says(bot, tg, "We'll speak to them tonight", card)
    staff_says(bot, tg, "/done", card, handler=bot.staff_done)
    assert store.get(request_id).status == "resolved"
    assert tg.send_message.call_args.args[0] == 42
    assert "finished with " + request_id in tg.send_message.call_args.args[1]
    assert 42 not in bot.conversations
    calls = chat.calls
    resident_says(bot, tg, "The lift in block 2 is broken", 120)
    assert chat.calls == calls + 1  # the AI answers again


def test_emergency_during_handover_still_gets_safety_text(baseline, tmp_path):
    bot, _, _ = make_bot(baseline, tmp_path)
    tg = fake_tg()
    resident_says(bot, tg, "Loud music from #08-21 every night", 100)
    staff_says(bot, tg, "Looking into it", card_id(bot))
    reply = resident_says(bot, tg, "now there's a fire in the corridor!", 130)
    assert "emergency number" in reply.call_args.args[0]
    assert "fire in the corridor" in tg.send_message.call_args.args[1]


def test_staff_chatter_and_unknown_cards(baseline, tmp_path):
    bot, _, _ = make_bot(baseline, tmp_path)
    tg = fake_tg()
    resident_says(bot, tg, "Loud music from #08-21 every night", 100)
    sent = tg.send_message.await_count
    # Replying to a colleague (not the bot) is ignored.
    staff_says(bot, tg, "I'll take this one", 777, sender=SARAH.id).assert_not_awaited()
    assert tg.send_message.await_count == sent
    # Replying to some other bot message with no request ID.
    assert "Reply to the request card" in staff_says(bot, tg, "hi", 888).call_args.args[0]
    assert tg.send_message.await_count == sent


def test_reply_by_request_id_after_restart(baseline, tmp_path):
    bot, store, _ = make_bot(baseline, tmp_path)
    tg = fake_tg()
    resident_says(bot, tg, "Loud music from #08-21 every night", 100)
    request_id = bot.conversations[42].request_id
    restarted = ResidentBot(bot.classifier, FakeChat(), STAFF, store)
    staff_says(restarted, tg, "Still happening?", 999, f"✅ Work order submitted · {request_id}")
    assert tg.send_message.call_args.args == (42, "👤 Sarah (staff): Still happening?")


def test_blocked_resident_is_reported_to_staff(baseline, tmp_path):
    bot, _, _ = make_bot(baseline, tmp_path)
    tg = fake_tg()
    resident_says(bot, tg, "Loud music from #08-21 every night", 100)
    tg.send_message.side_effect = Forbidden("bot was blocked by the user")
    reply = staff_says(bot, tg, "Hello?", card_id(bot))
    assert "Couldn't deliver" in reply.call_args.args[0]
    assert not bot.conversations[42].human_active


def test_photo_during_handover_is_copied_to_staff(baseline, tmp_path):
    bot, _, _ = make_bot(baseline, tmp_path)
    tg = fake_tg()
    resident_says(bot, tg, "Loud music from #08-21 every night", 100)
    staff_says(bot, tg, "Can you send a photo?", card_id(bot))
    message = SimpleNamespace(
        photo=[SimpleNamespace(file_size=100)],
        document=None,
        caption="here",
        message_id=140,
        reply_text=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=42, type="private"),
        effective_user=RESIDENT,
    )
    asyncio.run(bot.photo(update, SimpleNamespace(bot=tg)))
    copy = tg.copy_message.call_args
    assert copy.args == (STAFF, 42, 140)
    assert copy.kwargs["caption"].endswith(": here")
    message.reply_text.assert_not_awaited()
