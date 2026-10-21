import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from condo_classifier.chat import ready_to_submit
from condo_classifier.replies import CLOSING, STALE, work_order_summary
from condo_classifier.service import Classifier
from condo_classifier.store import RequestRecord, RequestStore

pytest.importorskip("telegram")
from condo_classifier.telegram_bot import ResidentBot  # noqa: E402

USER = SimpleNamespace(full_name="Jane Tan", username="janet", id=42)
STAFF = -100


class FakeChat:
    def reply(self, conversation, text, result):
        return "Got it."


def make_bot(baseline, tmp_path, **kwargs):
    store = RequestStore(tmp_path / "requests.db")
    return ResidentBot(Classifier(baseline), FakeChat(), STAFF, store, **kwargs), store


def fake_bot():
    return SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=7)),
        edit_message_text=AsyncMock(),
        send_chat_action=AsyncMock(),
        set_message_reaction=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
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


def tap(bot, tg, data):
    reply = AsyncMock(return_value=SimpleNamespace(message_id=500))
    query = SimpleNamespace(
        data=data,
        message=SimpleNamespace(chat=SimpleNamespace(id=1, type="private"), reply_text=reply),
        from_user=USER,
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    asyncio.run(bot.button(SimpleNamespace(callback_query=query), SimpleNamespace(bot=tg)))
    return query, reply


def buttons(call):
    markup = call.kwargs.get("reply_markup")
    if markup is None:
        return []
    return [b.callback_data.split(":")[0] for row in markup.inline_keyboard for b in row]


def last_reaction(tg):
    return tg.set_message_reaction.call_args.args[2]


def test_ready_request_shows_summary_and_submit(baseline, tmp_path):
    bot, store = make_bot(baseline, tmp_path)
    tg = fake_bot()
    first = send(bot, tg, "The corridor light keeps flickering")
    # No buttons while the bot is still asking for details.
    assert buttons(first) == []
    tg.set_message_reaction.assert_not_awaited()  # nothing to flag yet
    ready = send(bot, tg, "It's outside #05-12", message_id=110)
    assert buttons(ready) == ["submit", "cancel", "human"]
    assert "📋 Maintenance" in ready.args[0]
    assert last_reaction(tg) == "👌"

    request_id = bot.conversations[1].request_id
    _, reply = tap(bot, tg, f"submit:{request_id}")
    assert request_id in reply.call_args.args[0]
    assert buttons(reply.call_args) == []
    assert store.get(request_id).status == "submitted"
    assert last_reaction(tg) == "🫡"
    assert "✅ Work order submitted" in tg.edit_message_text.call_args.args[0]
    assert 1 not in bot.conversations


def test_submit_needs_complete_details(baseline, tmp_path):
    bot, store = make_bot(baseline, tmp_path)
    tg = fake_bot()
    send(bot, tg, "The corridor light keeps flickering")
    request_id = bot.conversations[1].request_id
    query, reply = tap(bot, tg, f"submit:{request_id}")
    assert "missing" in query.answer.call_args.args[0]
    reply.assert_not_awaited()
    assert store.get(request_id).status == "open"


def test_cancel_and_stale_buttons(baseline, tmp_path):
    bot, store = make_bot(baseline, tmp_path)
    tg = fake_bot()
    send(bot, tg, "Loud music from #08-21 every night")
    assert bot.conversations[1].status == "ready"
    request_id = bot.conversations[1].request_id
    _, reply = tap(bot, tg, f"cancel:{request_id}")
    assert buttons(reply.call_args) == []
    assert store.get(request_id).status == "cancelled"
    query, _ = tap(bot, tg, f"submit:{request_id}")
    assert query.answer.call_args.args[0] == STALE


def command(bot, tg, handler, args=()):
    message = SimpleNamespace(
        message_id=599,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=600)),
    )
    update = SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=1, type="private"),
        effective_user=USER,
    )
    asyncio.run(handler(update, SimpleNamespace(bot=tg, args=list(args))))
    return message.reply_text.call_args.args[0]


def test_human_command_pings_staff_and_keeps_chatting(baseline, tmp_path):
    bot, store = make_bot(baseline, tmp_path)
    tg = fake_bot()
    send(bot, tg, "The corridor light keeps flickering")
    request_id = bot.conversations[1].request_id
    assert request_id in command(bot, tg, bot.human)
    assert store.get(request_id).status == "needs_human"
    assert tg.send_message.await_count == 2  # fresh card so staff get notified
    assert tg.send_message.call_args.args[1].startswith("🙋 Resident wants a person")
    assert last_reaction(tg) == "🙏"
    later = send(bot, tg, "It's outside #05-12", message_id=120)
    assert buttons(later) == []
    assert store.get(request_id).status == "needs_human"
    assert "already" in command(bot, tg, bot.human)


def test_human_command_before_any_request(baseline, tmp_path):
    bot, store = make_bot(baseline, tmp_path)
    tg = fake_bot()
    assert "first" in command(bot, tg, bot.human)
    assert store.all() == []


def test_talk_to_a_person_button_on_ready_request(baseline, tmp_path):
    bot, store = make_bot(baseline, tmp_path)
    tg = fake_bot()
    ready = send(bot, tg, "Loud music from #08-21 every night")
    assert buttons(ready) == ["submit", "cancel", "human"]
    request_id = bot.conversations[1].request_id
    _, reply = tap(bot, tg, f"human:{request_id}")
    assert buttons(reply.call_args) == []
    assert store.get(request_id).status == "needs_human"
    tg.edit_message_reply_markup.assert_awaited_with(1, 101, reply_markup=None)


def test_emergency_has_no_buttons(baseline, tmp_path):
    bot, store = make_bot(baseline, tmp_path)
    tg = fake_bot()
    call = send(bot, tg, "Smoke is coming from the bin chute on level 7!")
    assert buttons(call) == []
    assert last_reaction(tg) == "😱"
    assert store.all()[0].status == "emergency"


def test_thanks_in_a_fresh_chat_closes_without_a_request(baseline, tmp_path):
    bot, store = make_bot(baseline, tmp_path)
    tg = fake_bot()
    call = send(bot, tg, "Thanks!")
    assert call.args[0] == CLOSING
    assert store.all() == []
    tg.send_message.assert_not_awaited()


@pytest.mark.parametrize("text", ["hi", "Hello!", "hey there", "good morning"])
def test_greetings_get_a_prompt_not_an_error(baseline, tmp_path, text):
    bot, store = make_bot(baseline, tmp_path)
    tg = fake_bot()
    call = send(bot, tg, text)
    assert call.args[0].startswith("Hi! 👋")
    assert store.all() == []
    assert 1 not in bot.conversations


def test_short_ok_mid_request_is_acknowledged_not_rejected(baseline, tmp_path):
    bot, _ = make_bot(baseline, tmp_path)
    tg = fake_bot()
    send(bot, tg, "The corridor light keeps flickering")
    call = send(bot, tg, "ok", message_id=150)
    assert call.args[0].startswith("👍")
    assert bot.conversations[1].resident_texts == ["The corridor light keeps flickering"]


def test_thanks_mid_request_is_part_of_the_conversation(baseline, tmp_path):
    bot, _ = make_bot(baseline, tmp_path)
    tg = fake_bot()
    send(bot, tg, "The corridor light keeps flickering")
    call = send(bot, tg, "thanks", message_id=140)
    assert call.args[0] == "Got it."
    assert bot.conversations[1].resident_texts[-1] == "thanks"


def test_greetings_are_not_sent_to_staff_until_understood(baseline, tmp_path):
    bot, store = make_bot(baseline, tmp_path)
    tg = fake_bot()
    send(bot, tg, "hello there")
    assert store.all() == []
    tg.send_message.assert_not_awaited()
    send(bot, tg, "the lift on block 2 is broken", message_id=130)
    assert len(store.all()) == 1
    tg.send_message.assert_awaited_once()


def test_invalid_message_gets_thinking_reaction(baseline, tmp_path):
    bot, _ = make_bot(baseline, tmp_path)
    tg = fake_bot()
    send(bot, tg, "?!")
    assert last_reaction(tg) == "🤔"


def test_store_round_trip(tmp_path):
    store = RequestStore(tmp_path / "nested" / "requests.db")
    record = RequestRecord(
        id="R-1",
        chat_id=1,
        resident="Jane",
        status="open",
        category="maintenance",
        priority="normal",
        team="Maintenance team",
        location=None,
        messages=["héllo"],
    )
    store.save(record)
    record.status = "submitted"
    store.save(record)
    saved = store.get("R-1")
    assert saved.status == "submitted"
    assert saved.messages == ["héllo"]
    assert len(store.all()) == 1
    record.status = "lost"
    with pytest.raises(ValueError):
        store.save(record)


def test_ready_and_summary_helpers(baseline):
    classifier = Classifier(baseline)
    from condo_classifier.schema import ResidentRequest

    vague = classifier.classify(ResidentRequest(text="The light keeps flickering"))
    located = classifier.classify(ResidentRequest(text="The light outside #05-12 flickers"))
    assert not ready_to_submit(vague)
    assert ready_to_submit(located)
    assert work_order_summary(located).startswith("📋 Maintenance · #05-12")


def test_deleted_requests_are_not_saved_again(baseline, tmp_path):
    bot, store = make_bot(baseline, tmp_path)
    tg = fake_bot()
    send(bot, tg, "The corridor light keeps flickering")
    request_id = bot.conversations[1].request_id
    cards = tg.send_message.await_count
    store.delete(request_id)  # staff delete it on the dashboard
    send(bot, tg, "It's outside #05-12", message_id=160)
    assert store.get(request_id) is None
    assert tg.send_message.await_count == cards
    tg.edit_message_text.assert_not_awaited()
    # A fresh request after /new is tracked normally.
    bot.conversations.pop(1)
    send(bot, tg, "Loud music from #08-21 every night", message_id=170)
    assert len(store.all()) == 1
