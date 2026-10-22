import asyncio
import json

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
    return WorkOrders("key", "token", "todo", "urgent", httpx.MockTransport(trello))


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
