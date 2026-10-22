"""Check how the bot sorts a wide set of resident messages, offline.

Drives the real bot logic (ResidentBot.text and .button) with fake Telegram objects, the
offline baseline classifier, fixed replies and the building FAQ. Nothing is sent anywhere:
there is no Telegram token, staff chat or Trello board, and Trello is replaced by a fake
that only records which list each card would go to.

    uv run --extra telegram python scripts/classification_check.py --json out.json

Expected results follow the rules in reports/classification-check.md.
"""

import argparse
import asyncio
import csv
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Never reach a real service, even if the shell has these set.
for name in list(os.environ):
    if name.startswith("TRELLO_") or name in {
        "TELEGRAM_BOT_TOKEN",
        "STAFF_CHAT_ID",
        "ANTHROPIC_API_KEY",
    }:
        del os.environ[name]

from condo_classifier.backends import BaselineBackend  # noqa: E402
from condo_classifier.knowledge import KnowledgeBase  # noqa: E402
from condo_classifier.service import Classifier  # noqa: E402
from condo_classifier.staff import review_notes  # noqa: E402
from condo_classifier.store import RequestStore  # noqa: E402
from condo_classifier.telegram_bot import ResidentBot  # noqa: E402

URGENT, TODO, NONE = "Urgent", "To Do", "none"
SUBMIT = "<tap Submit>"
REFUSAL = re.compile(
    r"\b(?:can'?t|cannot|unable to|not able to|won'?t) (?:help|assist|do that|handle)|"
    r"\boutside (?:my|our|the) (?:scope|remit)|\bnot something (?:we|i) (?:can|do)",
    re.I,
)


@dataclass
class Case:
    group: str
    messages: list[str]
    kind: str  # work_order, emergency, not_emergency, faq, question, reject
    category: str | tuple[str, ...] | None = None
    priority: str | None = None
    trello: str | None = None
    status: str | tuple[str, ...] | None = None
    asks_location: bool | None = None
    trello_path: list[str] | None = None
    note: str = ""


@dataclass
class Outcome:
    category: str | None = None
    priority: str | None = None
    team: str | None = None
    location: str | None = None
    status: str | None = None
    notes: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    trello_path: list[str] = field(default_factory=list)
    replies: list[str] = field(default_factory=list)

    @property
    def trello(self) -> str:
        return self.trello_path[-1] if self.trello_path else NONE


class FakeWorkOrders:
    """Records the list a card would be created in or moved to, like trello.WorkOrders."""

    def __init__(self):
        self.lists: dict[str, list[str]] = {}

    async def save(self, conversation, title, details, emergency):
        path = self.lists.setdefault(conversation.request_id, [])
        if not path:
            path.append(URGENT if emergency else TODO)
        elif emergency and path[-1] != URGENT:
            path.append(URGENT)


def fake_telegram():
    return SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=7)),
        edit_message_text=AsyncMock(),
        send_chat_action=AsyncMock(),
        set_message_reaction=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
        get_user_profile_photos=AsyncMock(return_value=SimpleNamespace(total_count=0)),
    )


def run_case(bot: ResidentBot, orders: FakeWorkOrders, case: Case, chat_id: int) -> Outcome:
    user = SimpleNamespace(full_name=f"Resident {chat_id}", username=None, id=chat_id)
    tg = fake_telegram()
    latest = {}
    save = bot.save

    def recording_save(key, conversation):
        latest["conversation"] = conversation
        save(key, conversation)

    bot.save = recording_save
    outcome = Outcome()
    try:
        for n, text in enumerate(case.messages):
            if text == SUBMIT:
                conversation = latest.get("conversation")
                request_id = conversation.request_id if conversation else "none"
                reply = AsyncMock(return_value=SimpleNamespace(message_id=900))
                query = SimpleNamespace(
                    data=f"submit:{request_id}",
                    message=SimpleNamespace(
                        chat=SimpleNamespace(id=chat_id, type="private"), reply_text=reply
                    ),
                    from_user=user,
                    answer=AsyncMock(),
                    edit_message_reply_markup=AsyncMock(),
                )
                asyncio.run(
                    bot.button(SimpleNamespace(callback_query=query), SimpleNamespace(bot=tg))
                )
                if reply.await_args:
                    outcome.replies.append(reply.await_args.args[0])
                elif query.answer.await_args:
                    outcome.replies.append(f"(pop-up) {query.answer.await_args.args[0]}")
                continue
            message = SimpleNamespace(
                text=text,
                message_id=100 + n,
                reply_text=AsyncMock(return_value=SimpleNamespace(message_id=500 + n)),
            )
            update = SimpleNamespace(
                effective_message=message,
                effective_chat=SimpleNamespace(id=chat_id, type="private"),
                effective_user=user,
            )
            asyncio.run(bot.text(update, SimpleNamespace(bot=tg)))
            for call in message.reply_text.await_args_list:
                outcome.replies.append(call.args[0])
    finally:
        bot.save = save
    conversation = latest.get("conversation")
    if conversation is not None and conversation.last_result is not None:
        result = conversation.last_result
        outcome.category = result.category.value
        outcome.priority = result.priority.value
        outcome.team = result.suggested_team
        outcome.location = result.location
        outcome.status = conversation.status
        outcome.notes = review_notes(result.review_reasons, conversation.status)
        outcome.missing = list(result.missing_details)
        outcome.trello_path = orders.lists.get(conversation.request_id, [])
    return outcome


def as_set(value) -> set:
    return {value} if isinstance(value, str) else set(value)


def judge(case: Case, out: Outcome) -> list[str]:
    """Reasons the outcome is wrong; empty means PASS."""
    problems = []
    emergency = out.status == "emergency" or out.trello == URGENT
    refused = [reply for reply in out.replies if REFUSAL.search(reply)]
    if case.kind == "emergency":
        if not emergency:
            problems.append("MISSED EMERGENCY")
    elif emergency:
        problems.append("FALSE EMERGENCY")
    if case.kind == "reject":
        tracked = out.trello != NONE
        if tracked and out.category not in {"other", "general_enquiry"}:
            problems.append(f"filed as a {out.category} work order, not for staff review")
        if tracked and not out.notes:
            problems.append("tracked without review notes")
        if out.status in {"answered", "cancelled"}:
            problems.append(f"bot closed it on its own ({out.status})")
        if refused:
            problems.append("bot refused the resident")
        return problems
    if case.kind == "question":
        if not (out.status == "answered" or out.category == "general_enquiry"):
            problems.append(f"not treated as a question ({out.category}, {out.status})")
    if case.kind == "faq":
        if out.status != "answered":
            problems.append(f"not answered from the FAQ ({out.category}, {out.status})")
    if case.kind == "work_order":
        if out.category in {None, "other", "general_enquiry"}:
            problems.append(f"not filed as a work order ({out.category})")
    if case.category and out.category not in as_set(case.category):
        problems.append(f"category {out.category}")
    if case.priority and out.priority != case.priority:
        problems.append(f"priority {out.priority}")
    if case.trello and out.trello != case.trello:
        problems.append(f"Trello {out.trello}")
    if case.trello_path and out.trello_path != case.trello_path:
        problems.append(f"Trello path {' -> '.join(out.trello_path) or NONE}")
    if case.status and out.status not in as_set(case.status):
        problems.append(f"status {out.status}")
    if case.asks_location is not None:
        asked = any("location" in detail.lower() for detail in out.missing)
        if asked != case.asks_location:
            problems.append("did not ask for location" if case.asks_location else "asked location")
    if refused:
        problems.append("bot refused the resident")
    return problems


def W(group, text, category, priority="normal", asks_location=False, **kw):
    return Case(
        group, [text] if isinstance(text, str) else text, "work_order", category, priority,
        TODO, asks_location=asks_location, **kw,
    )  # fmt: skip


def E(group, text, note=""):
    return Case(group, [text] if isinstance(text, str) else text, "emergency",
                trello=URGENT, status="emergency", note=note)  # fmt: skip


def N(group, text, note=""):
    return Case(group, [text], "not_emergency", note=note)


G1, G2, G3, G4 = "1 Work orders", "2 High priority", "3 Emergencies", "4 Look-alikes"
G5, G6, G7, G8 = "5 Multi-turn", "6 Other languages", "7 FAQ", "8 Nonsense and favours"

CASES = [
    # 1. Normal work orders, with and without a location.
    W(G1, "The corridor light outside #07-14 keeps flickering", "maintenance"),
    W(G1, "my kitchen tap is leaking non stop", "maintenance", asks_location=True),
    W(G1, "The aircon in the gym is not cold at all", "maintenance"),
    W(G1, "I lost my access card, I'm at Block A #05-02", "security"),
    W(G1, "Someone keeps stealing parcels from outside my door", "security", asks_location=True),
    W(G1, "Loud music from #08-21 every night past midnight", "noise"),
    W(G1, "the dog upstairs keeps barking the whole day", "noise", asks_location=True),
    W(G1, "There are cockroaches all over the Block C rubbish chute", "cleanliness"),
    W(
        G1,
        "someone left rubbish bags in the corridor for 3 days, it stinks",
        "cleanliness",
        asks_location=True,
    ),  # fmt: skip
    W(G1, "Can I book the BBQ pit at block 2 for Saturday evening?", "facilities"),
    W(G1, "I'd like to reserve the function room on 12 November", "facilities"),
    W(G1, "A car is parked in my lot B42 again", "parking"),
    W(G1, "my visitor got a wheel clamp in the carpark, why??", "parking"),
    W(G1, "I was charged twice for this month's maintenance fee", "billing"),
    W(G1, "Can I get a receipt for my October payment?", "billing"),
    W(G1, "I'm planning to renovate my kitchen next month, what forms do I need?", "renovation"),
    W(G1, "Moving out on Saturday, need the service lift for the movers", "renovation"),
    # 2. High priority, but not emergencies.
    W(G2, "The lift at Block D keeps getting stuck between floors", "maintenance", "high"),
    W(G2, "burst pipe in the corridor outside #03-11, water everywhere", "maintenance", "high"),
    W(G2, "power outage in my whole unit #12-04 since this morning", "maintenance", "high"),
    W(G2, "No water coming out of any tap in block 6", "maintenance", "high"),
    W(
        G2,
        "a stranger keeps tailgating residents through the side gate at block 1, looks suspicious",
        "security",
        "high",
    ),  # fmt: skip
    W(
        G2,
        "someone's car is blocking my driveway at lot C12 and I can't get out",
        "parking",
        "high",
    ),  # fmt: skip
    W(G2, "the carpark at basement 2 is flooding after the rain", "maintenance", "high"),
    # 3. Emergencies.
    E(G3, "There's a fire at the bin centre near block 4!!"),
    E(G3, "smoke coming out of the electrical room on level 2"),
    E(G3, "i smell gas in the corridor at block 3 level 8"),
    E(G3, "sparks flying from the DB box outside my unit #05-06"),
    E(G3, "help my son is trapped in the lift at block A"),
    E(G3, "someone is banging and shouting inside the lift, it stopped at level 9"),
    E(G3, "lift stuck at block 2 with ppl inside", "shorthand"),
    E(G3, "An old man collapsed at the lobby and isn't responding"),
    E(G3, "my neighbour fell down the stairs and is bleeding from her head"),
    E(G3, "my helper slipped in the bathroom and can't get up"),
    E(G3, "guy with a knife threatening the security guard at the main gate"),
    E(G3, "a man is chasing people with a parang at the playground"),
    E(G3, "a toddler is standing on the parapet at block 7 level 10!!"),
    E(G3, "my cat is on the ledge outside the window on level 15, she might fall"),
    E(G3, "the basement is flooding and water is reaching the electrical panels, it's sparking"),
    E(
        G3,
        "water pouring into the switch room and the power sockets are underwater",
        "flood + electrics, no 'sparks'",
    ),  # fmt: skip
    E(G3, "flood in the electrical riser, live wires in the water", "flood + live wires"),
    E(G3, "yo theres a fire at level 5 lol come quick", "casual"),
    E(G3, "smel gas at my kitchen pls help", "misspelled"),
    E(G3, "ther is a fir in the lift lobby!!!", "misspelled"),
    E(G3, "someone fainted at the pool", "lowercase"),
    E(
        G3,
        "Hi, I wanted to ask about renewing my parking label, and the corridor light is "
        "broken. Oh and by the way there's smoke coming from #04-12 next door, smells like "
        "burning plastic.",
        "buried in a long message",
    ),  # fmt: skip
    E(G3, "kid hanging from the railing at the level 4 corridor"),
    E(G3, "my husband is having chest pains and can't breathe properly"),
    # 4. Look-alikes that are not emergencies.
    N(G4, "no fire, just the smoke alarm beeping in my unit"),
    N(G4, "What do I do if someone is stuck in the lift?"),
    N(G4, "There was a fire at block 5 last week, when will the lobby be repainted?"),
    N(G4, "Is the fire drill on Thursday compulsory?"),
    N(G4, "the lift door bangs really loudly when it closes"),
    N(G4, "Does the management office have a knife sharpener I can borrow?"),
    N(G4, "The guy at #06-02 has a knife sharpening business, is that allowed?"),
    N(G4, "pigeons keep sitting on my aircon ledge and pooping everywhere"),
    N(G4, "my dad slipped on the wet floor at the pool last month, please put up a sign"),
    N(G4, "My son is playing with a toy gun at the playground, is that allowed?"),
    N(G4, "the smoke detector in my kitchen keeps going off when I cook"),
    N(G4, "The fire extinguisher at level 3 has expired"),
    N(G4, "I'm dying to know when the pool reopens lol"),
    # 5. Multi-turn conversations.
    Case(
        G5,
        ["The light in the stairwell keeps flickering", "It's at Block B level 3"],
        "work_order",
        "maintenance",
        status="ready",
        trello=TODO,
    ),  # fmt: skip
    Case(
        G5,
        [
            "Water is dripping from my bathroom ceiling",
            "it's #09-03",
            "now there's smoke coming out of the light fitting!",
        ],
        "emergency",
        status="emergency",
        trello_path=[TODO, URGENT],
        note="normal report turns into an emergency",
    ),  # fmt: skip
    Case(
        G5,
        ["lift at block 3 not working", "wait someone is stuck inside, I can hear them shouting"],
        "emergency",
        status="emergency",
        trello_path=[TODO, URGENT],
        note="stuck lift, then someone inside",
    ),  # fmt: skip
    Case(
        G5,
        ["The tap in the common toilet at the gym is broken", SUBMIT],
        "work_order",
        "maintenance",
        status="submitted",
        trello=TODO,
    ),  # fmt: skip
    Case(
        G5,
        [
            "Loud drilling from upstairs every morning at 7am",
            "I'm at #11-05 and it's coming from #12-05",
            SUBMIT,
        ],
        "work_order",
        "noise",
        status="submitted",
        trello=TODO,
    ),  # fmt: skip
    # 6. Chinese, Malay and Tamil with no translation.
    E(G6, "我闻到煤气味，好浓", "zh: I smell gas"),
    E(G6, "楼下着火了，快来！", "zh: fire downstairs"),
    E(G6, "有人晕倒了，在大堂", "zh: someone fainted in the lobby"),
    E(G6, "电梯坏了，我妈妈被困在里面", "zh: lift broke, my mum is trapped inside"),
    E(G6, "Ada orang pengsan di lobi", "ms: someone fainted"),
    E(G6, "Tolong! Rumah jiran terbakar", "ms: neighbour's house on fire"),
    E(G6, "lif rosak, anak saya terperangkap di dalam", "ms: child trapped inside the lift"),
    E(G6, "Ada lelaki bawa pisau di tangga blok 3", "ms: man with a knife"),
    E(G6, "சமையலறையில் தீப்பிடித்துவிட்டது", "ta: kitchen caught fire"),
    E(G6, "யாரோ மயக்கம் அடைந்துவிட்டார்", "ta: someone fainted"),
    E(G6, "கத்தியுடன் ஒருவர் படிக்கட்டில் நிற்கிறார்", "ta: man with a knife on the stairs"),
    N(G6, "tiada kebakaran, cuma penggera berbunyi", "ms: no fire, just the alarm"),
    N(G6, "我想预订烧烤炉", "zh: I want to book the BBQ pit"),
    # 7. FAQ questions.
    Case(G7, ["what time does the gym open?"], "faq", status="answered", trello=NONE),
    Case(G7, ["When is the swimming pool open?"], "faq", status="answered", trello=NONE),
    Case(G7, ["are pets allowed here?"], "faq", status="answered", trello=NONE),
    Case(G7, ["Where can my visitors park?"], "faq", status="answered", trello=NONE),
    Case(G7, ["what are the quiet hours?"], "faq", status="answered", trello=NONE),
    # 8. Nonsense, jokes, favours and things management can't do.
    Case(G8, ["can someone walk my dog this afternoon"], "reject"),
    Case(G8, ["order me a pizza pls"], "reject"),
    Case(G8, ["asdfgh"], "reject"),
    Case(G8, ["make my neighbour at #05-01 move out, I hate him"], "reject"),
    Case(G8, ["tell me a joke"], "reject"),
    Case(G8, ["can you do my maths homework"], "reject"),
    Case(G8, ["please make it stop raining, I want to swim"], "reject"),
    Case(G8, ["lend me $50 till payday"], "reject"),
    Case(G8, ["will you be my girlfriend"], "reject"),
    Case(G8, ["the moon is too bright tonight, fix it"], "reject"),
]

SILLY = "Silly requests (data/silly_requests.csv)"
BUCKET_KIND = {
    "work_order": "work_order",
    "emergency": "emergency",
    "question": "question",
    "rejection": "reject",
}


def silly_cases() -> list[Case]:
    with (ROOT / "data" / "silly_requests.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    cases = []
    for row in rows:
        kind = BUCKET_KIND[row["bucket"]]
        cases.append(
            Case(
                SILLY,
                [row["text"]],
                kind,
                trello=URGENT if kind == "emergency" else (TODO if kind == "work_order" else None),
                note=row["why_its_tricky"],
            )
        )
    return cases


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path, help="write every result to this JSON file")
    args = parser.parse_args()
    orders = FakeWorkOrders()
    with tempfile.TemporaryDirectory() as tmp:
        bot = ResidentBot(
            Classifier(BaselineBackend(), review_threshold=0.60),
            None,  # BOT_REPLIES=fixed
            None,  # no staff chat
            RequestStore(Path(tmp) / "requests.db"),
            knowledge=KnowledgeBase(),
            daily_limit=0,
            work_orders=orders,
        )
        rows = []
        for n, case in enumerate([*CASES, *silly_cases()], start=1):
            out = run_case(bot, orders, case, chat_id=10_000 + n)
            problems = judge(case, out)
            rows.append(
                {
                    "group": case.group,
                    "messages": case.messages,
                    "kind": case.kind,
                    "expected": expected_text(case),
                    "note": case.note,
                    "outcome": out.__dict__ | {"trello": out.trello},
                    "problems": problems,
                    "pass": not problems,
                }
            )
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["group"], []).append(row)
    for group, items in groups.items():
        passed = sum(row["pass"] for row in items)
        print(f"{group}: {passed}/{len(items)}")
        for row in items:
            if not row["pass"]:
                print(f"   FAIL {row['messages'][-1][:70]!r}: {'; '.join(row['problems'])}")
    total = sum(row["pass"] for row in rows)
    print(f"Total: {total}/{len(rows)}")
    if args.json:
        args.json.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


def expected_text(case: Case) -> str:
    if case.kind == "emergency":
        return "Urgent" + (f" ({' -> '.join(case.trello_path)})" if case.trello_path else "")
    if case.kind == "not_emergency":
        return "not an emergency"
    if case.kind == "faq":
        return "FAQ answer, no card"
    if case.kind == "question":
        return "question: FAQ answer or general enquiry"
    if case.kind == "reject":
        return "staff decides: no card, or other/general with review notes"
    parts = [str(case.category)] if case.category else ["work order"]
    if case.priority == "high":
        parts.append("high")
    if case.asks_location:
        parts.append("asks location")
    if case.status:
        parts.append(str(case.status))
    return ", ".join(parts) + (f" → {case.trello}" if case.trello else "")


if __name__ == "__main__":
    main()
