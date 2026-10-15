import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from condo_classifier.replies import NOT_FOR_YOU
from condo_classifier.service import Classifier
from condo_classifier.store import RequestStore

pytest.importorskip("telegram")
from telegram import Chat, Message, MessageEntity, Update, User  # noqa: E402

from condo_classifier.telegram_bot import ResidentBot, addressed_text  # noqa: E402

GROUP = -500
STAFF = -100
BOT_ID = 99
JANE = SimpleNamespace(id=10, full_name="Jane", username="jane", is_bot=False)
ALEX = SimpleNamespace(id=20, full_name="Alex", username="alex", is_bot=False)
OTHER_BOT = SimpleNamespace(id=77, full_name="Rival", username="rival_bot", is_bot=True)


class FakeChat:
    def reply(self, conversation, text, result):
        return "Got it."


def fake_tg():
    return SimpleNamespace(
        id=BOT_ID,
        username="condo_bot",
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=7)),
        send_photo=AsyncMock(),
        edit_message_text=AsyncMock(),
        send_chat_action=AsyncMock(),
        set_message_reaction=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )


def msg(text, sender=JANE, reply_to=None, message_id=100, topic=False):
    replied = None
    if reply_to is not None:
        replied = SimpleNamespace(
            from_user=SimpleNamespace(id=reply_to), forum_topic_created=topic or None
        )
    return SimpleNamespace(
        text=text,
        caption=None,
        photo=None,
        document=None,
        from_user=sender,
        reply_to_message=replied,
        is_topic_message=topic,
        message_id=message_id,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=message_id + 1)),
    )


def post(bot, tg, message, chat_id=GROUP):
    update = SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=chat_id, type="supergroup"),
        effective_user=message.from_user,
    )
    asyncio.run(bot.group(update, SimpleNamespace(bot=tg)))
    call = message.reply_text.call_args
    return call.args[0] if call else None


def make_bot(baseline, tmp_path, limit=3):
    store = RequestStore(tmp_path / "requests.db")
    bot = ResidentBot(Classifier(baseline), FakeChat(), STAFF, store, None, limit)
    return bot, store


@pytest.mark.parametrize(
    "message,expected",
    [
        (msg("@condo_bot the lift is broken"), "the lift is broken"),
        (msg("hey @CONDO_BOT, lift broken"), "hey , lift broken"),
        (msg("lift broken", reply_to=BOT_ID), "lift broken"),
        (msg("lift broken"), None),
        (msg("@condo_bot_fan lift broken"), None),
        (msg("@rival_bot lift broken"), None),
        (msg("lift broken", reply_to=77), None),
        (msg("lift broken", reply_to=BOT_ID, topic=True), None),
        (msg("lift broken", sender=OTHER_BOT, reply_to=BOT_ID), "lift broken"),
    ],
)
def test_only_mentions_and_replies_are_addressed(message, expected):
    assert addressed_text(message, fake_tg()) == expected


def test_normal_group_messages_get_no_reply(baseline, tmp_path):
    bot, store = make_bot(baseline, tmp_path)
    tg = fake_tg()
    assert post(bot, tg, msg("the lift in block 2 is broken")) is None
    assert bot.conversations == {}
    tg.send_chat_action.assert_not_awaited()


def test_each_person_in_a_group_has_their_own_request(baseline, tmp_path):
    bot, store = make_bot(baseline, tmp_path)
    tg = fake_tg()
    assert post(bot, tg, msg("@condo_bot the light keeps flickering")) == "Got it."
    assert post(bot, tg, msg("@condo_bot loud music from #08-21", sender=ALEX)).startswith(
        "Got it."
    )
    jane, alex = bot.conversations[(GROUP, 10)], bot.conversations[(GROUP, 20)]
    assert jane.resident_texts == ["the light keeps flickering"]
    assert alex.resident_texts == ["loud music from #08-21"]
    # Jane replies to the bot with her unit; Alex's request is untouched.
    post(bot, tg, msg("it's outside #05-12", reply_to=BOT_ID, message_id=200))
    assert jane.status == "ready"
    assert alex.resident_texts == ["loud music from #08-21"]
    assert {r.chat_id for r in store.all()} == {GROUP}


def test_only_the_requester_can_tap_their_buttons(baseline, tmp_path):
    bot, store = make_bot(baseline, tmp_path)
    tg = fake_tg()
    post(bot, tg, msg("@condo_bot loud music from #08-21 every night"))
    request_id = bot.conversations[(GROUP, 10)].request_id

    def tap(user):
        query = SimpleNamespace(
            data=f"submit:{request_id}",
            message=SimpleNamespace(
                chat=SimpleNamespace(id=GROUP, type="supergroup"), reply_text=AsyncMock()
            ),
            from_user=user,
            answer=AsyncMock(),
            edit_message_reply_markup=AsyncMock(),
        )
        asyncio.run(bot.button(SimpleNamespace(callback_query=query), SimpleNamespace(bot=tg)))
        return query

    assert tap(ALEX).answer.call_args.args[0] == NOT_FOR_YOU
    assert store.get(request_id).status == "ready"
    tap(JANE)
    assert store.get(request_id).status == "submitted"


def test_bot_to_bot_replies_are_capped_and_reset_by_a_person(baseline, tmp_path):
    bot, _ = make_bot(baseline, tmp_path, limit=2)
    tg = fake_tg()
    replies = [
        post(bot, tg, msg("the lift is broken", sender=OTHER_BOT, reply_to=BOT_ID, message_id=n))
        for n in (300, 310, 320)
    ]
    assert replies[:2] == ["Got it.", "Got it."]
    assert replies[2] is None  # loop guard
    post(bot, tg, msg("@condo_bot hello again", message_id=330))
    reply = post(bot, tg, msg("block 3", sender=OTHER_BOT, reply_to=BOT_ID, message_id=340))
    assert reply.startswith("Got it.")


def test_zero_limit_ignores_other_bots(baseline, tmp_path):
    bot, _ = make_bot(baseline, tmp_path, limit=0)
    assert post(bot, fake_tg(), msg("lift broken", sender=OTHER_BOT, reply_to=BOT_ID)) is None


def test_ask_command(baseline, tmp_path):
    bot, _ = make_bot(baseline, tmp_path)
    tg = fake_tg()
    message = msg("/ask@condo_bot the lift is broken", sender=OTHER_BOT)
    update = SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=GROUP, type="supergroup"),
        effective_user=OTHER_BOT,
    )
    asyncio.run(bot.ask(update, SimpleNamespace(bot=tg, args=["the", "lift", "is", "broken"])))
    assert bot.conversations[(GROUP, 77)].resident_texts == ["the lift is broken"]


# Routing with real Telegram objects ----------------------------------------------------


def route(app, chat_type, chat_id, text, sender=None, reply_to_bot=False, kind="message"):
    sender = sender or User(10, "Jane", False)
    entities = None
    if text.startswith("/"):
        entities = [MessageEntity("bot_command", 0, len(text.split()[0]))]
    reply = None
    if reply_to_bot:
        reply = Message(5, datetime.now(), Chat(chat_id, chat_type), from_user=app.bot.bot)
    message = Message(
        1,
        datetime.now(),
        Chat(chat_id, chat_type),
        from_user=sender,
        text=text,
        entities=entities,
        reply_to_message=reply,
    )
    message.set_bot(app.bot)
    update = Update(1, **{kind: message})
    hits = [h.callback.__name__ for h in app.handlers[0] if h.check_update(update)]
    return hits[0] if hits else None


@pytest.fixture
def app(baseline):
    def build(group_replies=True):
        bot = ResidentBot(Classifier(baseline), staff_chat_id=STAFF)
        application = bot.application("123:abc", group_replies)
        application.bot._bot_user = User(BOT_ID, "Condo", True, username="condo_bot")
        return application

    return build


def test_group_routing(app):
    application = app()
    assert route(application, "supergroup", GROUP, "lift broken") == "group"
    assert route(application, "supergroup", GROUP, "/ask@condo_bot lift") == "ask"
    assert route(application, "supergroup", GROUP, "/ask@rival_bot lift") is None
    rival = User(77, "Rival", True, username="rival_bot")
    assert route(application, "supergroup", GROUP, "/ask@condo_bot lift", sender=rival) == "ask"
    assert route(application, "supergroup", GROUP, "x", rival, reply_to_bot=True) == "group"
    # The staff group never gets resident replies.
    assert route(application, "supergroup", STAFF, "@condo_bot lift broken") is None
    assert route(application, "supergroup", STAFF, "/chatid") == "chat_id"
    # Edited messages are ignored everywhere.
    assert route(application, "supergroup", GROUP, "lift", kind="edited_message") is None


def test_group_replies_are_off_by_default(app):
    application = app(group_replies=False)
    assert route(application, "supergroup", GROUP, "@condo_bot lift broken") is None
    assert route(application, "supergroup", GROUP, "/ask@condo_bot lift") is None
    assert route(application, "private", 10, "lift broken") == "text"
