import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from condo_classifier.chat import Conversation
from condo_classifier.schema import Priority
from condo_classifier.service import Classifier
from condo_classifier.staff import work_order_title
from condo_classifier.trello import WorkOrders
from tests.test_staff import STAFF, fake_bot, result, run

LISTS = [
    {"id": "backlog", "name": "Backlog"},
    {"id": "todo", "name": "To Do"},
    {"id": "progress", "name": "In Progress"},
    {"id": "urgent", "name": "Urgent"},
]


class FakeTrello:
    """Records calls and keeps cards like Trello does, so tests can read the board."""

    def __init__(self):
        self.cards: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert 'oauth_token="token"' in request.headers["Authorization"]
        assert "token" not in str(request.url)
        self.calls.append((request.method, request.url.path))
        if self.down:
            return httpx.Response(503)
        if request.url.path == "/1/boards/yZeVpykB/lists":
            return httpx.Response(200, json=LISTS)
        if request.url.path.startswith("/1/boards/"):
            return httpx.Response(404)
        fields = json.loads(request.content)
        if request.method == "POST":
            card_id = f"card{len(self.cards) + 1}"
            self.cards[card_id] = fields
            return httpx.Response(200, json={"id": card_id})
        card_id = request.url.path.rsplit("/", 1)[1]
        if card_id not in self.cards:
            return httpx.Response(404)
        self.cards[card_id] |= fields
        return httpx.Response(200, json={"id": card_id})


@pytest.fixture
def trello():
    return FakeTrello()


@pytest.fixture
def orders(trello):
    # Due dates have their own tests below.
    return WorkOrders(
        "key",
        "token",
        "todo",
        "urgent",
        httpx.MockTransport(trello),
        emergency_hours=0,
        high_hours=0,
    )


@pytest.fixture
def env(monkeypatch):
    for name, value in {
        "TRELLO_BOARD_ID": "yZeVpykB",
        "TRELLO_API_KEY": "key",
        "TRELLO_TOKEN": "token",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("TRELLO_TODO_LIST", raising=False)
    monkeypatch.delenv("TRELLO_URGENT_LIST", raising=False)


def test_lists_are_found_by_name(env, trello):
    orders = WorkOrders.from_env(httpx.MockTransport(trello))
    assert (orders.todo_list, orders.urgent_list) == ("todo", "urgent")


def test_off_without_a_board(monkeypatch):
    monkeypatch.delenv("TRELLO_BOARD_ID", raising=False)
    assert WorkOrders.from_env() is None


@pytest.mark.parametrize(
    "setting,value,message",
    [
        ("TRELLO_TOKEN", "", "set TRELLO_API_KEY and TRELLO_TOKEN"),
        ("TRELLO_URGENT_LIST", "Emergencies", 'no "Emergencies" list'),
        ("TRELLO_BOARD_ID", "missing", "was not found"),
    ],
)
def test_setup_mistakes_are_explained(env, trello, monkeypatch, setting, value, message):
    monkeypatch.setenv(setting, value)
    with pytest.raises(ValueError, match=message):
        WorkOrders.from_env(httpx.MockTransport(trello))


def test_new_request_goes_to_todo_and_is_updated_in_place(orders, trello):
    conversation = Conversation()
    asyncio.run(orders.save(conversation, "R-1 · Maintenance", "first", emergency=False))
    asyncio.run(orders.save(conversation, "R-1 · Maintenance · #05-12", "second", False))
    assert list(trello.cards) == ["card1"]
    card = trello.cards["card1"]
    assert card == {
        "name": "R-1 · Maintenance · #05-12",
        "desc": "second",
        "idList": "todo",
        "pos": "top",
    }


def test_emergency_moves_to_urgent_once(orders, trello):
    conversation = Conversation()
    asyncio.run(orders.save(conversation, "R-1", "bin chute door broken", emergency=False))
    asyncio.run(orders.save(conversation, "🚨 R-1", "smoke", emergency=True))
    assert trello.cards["card1"]["idList"] == "urgent"
    # Staff move it to In Progress; later updates must not pull it back.
    trello.cards["card1"]["idList"] = "progress"
    asyncio.run(orders.save(conversation, "🚨 R-1", "Block 3", emergency=True))
    assert trello.cards["card1"] == {
        "name": "🚨 R-1",
        "desc": "Block 3",
        "idList": "progress",
        "pos": "top",
    }


def test_first_message_emergency_starts_in_urgent(orders, trello):
    asyncio.run(orders.save(Conversation(), "🚨 R-1", "fire", emergency=True))
    assert trello.cards["card1"]["idList"] == "urgent"


def test_deleted_card_is_recreated(orders, trello):
    conversation = Conversation()
    asyncio.run(orders.save(conversation, "R-1", "first", emergency=False))
    trello.cards.clear()
    asyncio.run(orders.save(conversation, "R-1", "second", emergency=False))
    assert conversation.work_order_id == "card1"
    assert trello.cards["card1"]["desc"] == "second"


def test_title():
    assert work_order_title("R-1", result()) == "R-1 · Maintenance · location not given"
    emergency = result(status="safety_escalation", priority=Priority.EMERGENCY, location="Block 3")
    assert work_order_title("R-1", emergency) == "🚨 R-1 · Maintenance · Block 3"


def test_bot_files_work_orders(baseline, orders, trello):
    from condo_classifier.telegram_bot import ResidentBot

    bot = ResidentBot(Classifier(baseline), staff_chat_id=STAFF, work_orders=orders)
    staff_bot = fake_bot()
    run(bot, staff_bot, 1, "The bin chute door is broken on level 7")
    (card,) = trello.cards.values()
    assert card["idList"] == "todo"
    assert bot.conversations[1].request_id in card["name"]
    assert "> The bin chute door is broken on level 7" in card["desc"]

    run(bot, staff_bot, 1, "Now smoke is coming from the bin chute!")
    (card,) = trello.cards.values()
    assert card["idList"] == "urgent"
    assert card["name"].startswith("🚨")
    assert staff_bot.send_message.call_args.args[1].startswith("🚨 EMERGENCY")


def test_trello_failure_never_blocks_the_resident(baseline, orders, trello):
    from condo_classifier.telegram_bot import ResidentBot

    trello.down = True
    bot = ResidentBot(Classifier(baseline), work_orders=orders)
    assert "Maintenance" in run(bot, fake_bot(), 1, "The light keeps flickering")


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def timed(trello, clock):
    return WorkOrders("key", "token", "todo", "urgent", httpx.MockTransport(trello), now=clock)


def test_routine_requests_get_no_due_date(timed, trello):
    asyncio.run(timed.save(Conversation(), "R-1", "flickering light", emergency=False))
    assert "due" not in trello.cards["card1"]


def test_high_priority_is_due_in_a_day_and_not_pushed_back(timed, trello, clock):
    conversation = Conversation()
    asyncio.run(timed.save(conversation, "R-1", "no water", False, high_priority=True))
    assert trello.cards["card1"]["due"] == "2026-10-23T09:00:00.000Z"
    # A detail added later keeps the deadline staff already see.
    clock.now += timedelta(hours=3)
    asyncio.run(timed.save(conversation, "R-1", "no water, #04-11", False, high_priority=True))
    assert trello.cards["card1"]["due"] == "2026-10-23T09:00:00.000Z"


def test_escalation_brings_the_due_date_forward(timed, trello, clock):
    conversation = Conversation()
    asyncio.run(timed.save(conversation, "R-1", "bin chute door", False, high_priority=True))
    clock.now += timedelta(hours=2)
    asyncio.run(timed.save(conversation, "🚨 R-1", "smoke", emergency=True))
    assert trello.cards["card1"]["due"] == "2026-10-22T12:00:00.000Z"
    assert trello.cards["card1"]["idList"] == "urgent"


def test_recreated_card_keeps_its_due_date(timed, trello, clock):
    conversation = Conversation()
    asyncio.run(timed.save(conversation, "🚨 R-1", "fire", emergency=True))
    trello.cards.clear()
    clock.now += timedelta(minutes=30)
    asyncio.run(timed.save(conversation, "🚨 R-1", "fire, block 3", emergency=True))
    assert trello.cards["card1"]["due"] == "2026-10-22T10:00:00.000Z"


@pytest.mark.parametrize("value", ["soon", "-2"])
def test_bad_due_hours_are_explained(env, trello, monkeypatch, value):
    monkeypatch.setenv("TRELLO_DUE_HIGH_HOURS", value)
    with pytest.raises(ValueError, match="TRELLO_DUE_HIGH_HOURS must be a number"):
        WorkOrders.from_env(httpx.MockTransport(trello))


def test_zero_hours_turns_due_dates_off(env, trello, monkeypatch):
    monkeypatch.setenv("TRELLO_DUE_EMERGENCY_HOURS", "0")
    orders = WorkOrders.from_env(httpx.MockTransport(trello))
    orders.transport = httpx.MockTransport(trello)
    asyncio.run(orders.save(Conversation(), "🚨 R-1", "fire", emergency=True))
    assert "due" not in trello.cards["card1"]


def test_bot_sets_due_dates_by_priority(baseline, timed, trello):
    from condo_classifier.telegram_bot import ResidentBot

    bot = ResidentBot(Classifier(baseline), work_orders=timed)
    run(bot, fake_bot(), 1, "my tap has no water since this morning at #04-11")
    run(bot, fake_bot(), 2, "The corridor light keeps flickering at #05-12")
    run(bot, fake_bot(), 3, "There is smoke coming from the bin chute at block 3!")
    due = {card["desc"].splitlines()[-1]: card.get("due") for card in trello.cards.values()}
    assert due == {
        "> my tap has no water since this morning at #04-11": "2026-10-23T09:00:00.000Z",
        "> The corridor light keeps flickering at #05-12": None,
        "> There is smoke coming from the bin chute at block 3!": "2026-10-22T10:00:00.000Z",
    }
