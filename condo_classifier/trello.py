"""Work orders on a Trello board: one card per request in To Do, emergencies in Urgent.

Staff move cards along the board themselves. The bot only creates a card, keeps its
details current while the resident adds information, and moves it to Urgent once when
the request is escalated as an emergency. Emergencies and high-priority requests get a
due date; routine requests get none.
"""

import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx

from condo_classifier.chat import Conversation

API = "https://api.trello.com/1"
TIMEOUT_SECONDS = 15


class WorkOrders:
    def __init__(
        self,
        key: str,
        token: str,
        todo_list: str,
        urgent_list: str,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        emergency_hours: float = 1,
        high_hours: float = 24,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        # A header keeps the token out of request URLs and logs.
        self.headers = {"Authorization": f'OAuth oauth_consumer_key="{key}", oauth_token="{token}"'}
        self.todo_list = todo_list
        self.urgent_list = urgent_list
        self.transport = transport
        # How long staff have to act, from when the request first reached that priority.
        # 0 turns due dates off for that priority.
        self.due_hours = {"emergency": emergency_hours, "high": high_hours}
        self.now = now

    def due(self, conversation: Conversation, urgency: str | None) -> datetime | None:
        """The card's new due date, or None to leave it. Escalation only brings a due date
        forward: later updates never push it back, so staff keep the date they see."""
        hours = self.due_hours.get(urgency or "", 0)
        if not hours:
            return None
        due = self.now() + timedelta(hours=hours)
        current = conversation.work_order_due
        return due if current is None or due < current else None

    @classmethod
    def from_env(cls, transport: httpx.BaseTransport | None = None) -> "WorkOrders | None":
        """Look up the board's lists by name. None when TRELLO_BOARD_ID is not set."""
        board = os.getenv("TRELLO_BOARD_ID", "").strip()
        if not board:
            return None
        key = os.getenv("TRELLO_API_KEY", "").strip()
        token = os.getenv("TRELLO_TOKEN", "").strip()
        if not key or not token:
            raise ValueError("set TRELLO_API_KEY and TRELLO_TOKEN to use TRELLO_BOARD_ID.")
        names = {
            "TRELLO_TODO_LIST": os.getenv("TRELLO_TODO_LIST") or "To Do",
            "TRELLO_URGENT_LIST": os.getenv("TRELLO_URGENT_LIST") or "Urgent",
        }
        hours = {}
        for setting, default in (
            ("TRELLO_DUE_EMERGENCY_HOURS", "1"),
            ("TRELLO_DUE_HIGH_HOURS", "24"),
        ):
            try:
                hours[setting] = float(os.getenv(setting) or default)
            except ValueError:
                hours[setting] = -1
            if hours[setting] < 0:
                raise ValueError(f"{setting} must be a number of hours (0 for no due date).")
        orders = cls(
            key,
            token,
            "",
            "",
            emergency_hours=hours["TRELLO_DUE_EMERGENCY_HOURS"],
            high_hours=hours["TRELLO_DUE_HIGH_HOURS"],
        )
        try:
            with httpx.Client(
                headers=orders.headers, timeout=TIMEOUT_SECONDS, transport=transport
            ) as client:
                response = client.get(f"{API}/boards/{board}/lists", params={"fields": "name"})
        except httpx.HTTPError as exc:
            raise ValueError(f"could not reach Trello ({type(exc).__name__}).") from exc
        if response.status_code in (400, 401, 403):
            raise ValueError("Trello refused TRELLO_API_KEY or TRELLO_TOKEN, or the board.")
        if response.status_code == 404:
            raise ValueError(f"Trello board {board} was not found. Check TRELLO_BOARD_ID.")
        response.raise_for_status()
        lists = {item["name"].strip().casefold(): item["id"] for item in response.json()}
        ids = {}
        for setting, name in names.items():
            if name.strip().casefold() not in lists:
                raise ValueError(f'the Trello board has no "{name}" list. Check {setting}.')
            ids[setting] = lists[name.strip().casefold()]
        orders.todo_list = ids["TRELLO_TODO_LIST"]
        orders.urgent_list = ids["TRELLO_URGENT_LIST"]
        return orders

    async def save(
        self,
        conversation: Conversation,
        title: str,
        details: str,
        emergency: bool,
        high_priority: bool = False,
    ) -> None:
        """Create or update this request's card. Raises httpx.HTTPError on failure."""
        fields = {"name": title, "desc": details}
        move = emergency and not conversation.work_order_urgent
        if move:
            # Only an emergency moves a card, and only once, so staff keep control of it.
            fields |= {"idList": self.urgent_list, "pos": "top"}
        urgency = "emergency" if emergency else "high" if high_priority else None
        due = self.due(conversation, urgency)
        if due is not None:
            fields["due"] = trello_date(due)
        async with httpx.AsyncClient(
            base_url=API, headers=self.headers, timeout=TIMEOUT_SECONDS, transport=self.transport
        ) as client:
            response = None
            if conversation.work_order_id:
                response = await client.put(f"/cards/{conversation.work_order_id}", json=fields)
            if response is None or response.status_code == 404:
                # New request, or staff deleted its card: make one.
                fields["idList"] = self.urgent_list if emergency else self.todo_list
                fields["pos"] = "top"
                if "due" not in fields and conversation.work_order_due is not None:
                    fields["due"] = trello_date(conversation.work_order_due)
                response = await client.post("/cards", json=fields)
            response.raise_for_status()
            conversation.work_order_id = response.json()["id"]
        conversation.work_order_urgent = conversation.work_order_urgent or emergency
        conversation.work_order_due = due or conversation.work_order_due


def trello_date(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
