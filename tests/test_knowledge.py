import csv
from pathlib import Path

import pytest

from condo_classifier.knowledge import (
    FAQ_PATH,
    KnowledgeBase,
    knowledge_note,
    looks_like_question,
)
from condo_classifier.service import Classifier
from condo_classifier.store import RequestStore

pytest.importorskip("telegram")
from condo_classifier.telegram_bot import ResidentBot  # noqa: E402
from tests.test_buttons import STAFF, fake_bot, send  # noqa: E402

EVAL_PATH = Path(__file__).resolve().parents[1] / "data" / "faq_eval.csv"


@pytest.fixture(scope="module")
def knowledge():
    return KnowledgeBase()


def eval_rows():
    with EVAL_PATH.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def test_faq_entries_are_unique_and_complete():
    with FAQ_PATH.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len({row["id"] for row in rows}) == len(rows) >= 25
    assert all(row["question"] and row["answer"] and row["keywords"] for row in rows)
    expected = {row["faq_id"] for row in eval_rows() if row["faq_id"]}
    assert expected <= {row["id"] for row in rows}


def test_retrieval_finds_the_right_entry_for_paraphrased_questions(knowledge):
    questions = [row for row in eval_rows() if row["faq_id"]]
    top1 = sum(knowledge.scores(row["text"])[0][0].id == row["faq_id"] for row in questions)
    assert top1 / len(questions) >= 0.9


def test_bot_answers_questions_but_not_reports(knowledge):
    """The question gate plus threshold the bot uses: most questions answered right,
    and no problem report is ever answered with an FAQ entry."""
    right = wrong = reports_answered = 0
    for row in eval_rows():
        facts = knowledge.search(row["text"]) if looks_like_question(row["text"]) else []
        if not row["faq_id"]:
            reports_answered += bool(facts)
        elif facts:
            right += facts[0].id == row["faq_id"]
            wrong += facts[0].id != row["faq_id"]
    assert reports_answered == 0
    assert right >= 30
    assert wrong <= 2


@pytest.mark.parametrize(
    "text,question",
    [
        ("What time does the gym open?", True),
        ("can my guests use the pool", True),
        ("how much is the bbq pit", True),
        ("The lift at Block C is stuck", False),
        ("Water is leaking from my ceiling at #05-12", False),
        ("I was charged twice for my maintenance fee", False),
    ],
)
def test_question_gate(text, question):
    assert looks_like_question(text) is question


def test_note_lists_retrieved_facts(knowledge):
    note = knowledge_note(knowledge.search("when is the gym open"))
    assert note.startswith("Building information")
    assert "gym" in note.lower()


def test_empty_faq_is_rejected(tmp_path):
    path = tmp_path / "faq.csv"
    path.write_text("id,question,answer,keywords\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no entries"):
        KnowledgeBase(path)


def make_bot(baseline, knowledge, tmp_path, chat=None):
    store = RequestStore(tmp_path / "requests.db")
    return ResidentBot(Classifier(baseline), chat, STAFF, store, knowledge=knowledge), store


def test_standalone_question_is_answered_logged_and_closed(baseline, knowledge, tmp_path):
    bot, store = make_bot(baseline, knowledge, tmp_path)
    tg = fake_bot()
    call = send(bot, tg, "What are the gym opening hours?")
    gym = next(fact for fact in knowledge.facts if fact.id == "gym-hours")
    # Without a chat model the FAQ answer is sent as written.
    assert call.args[0] == gym.answer
    assert call.kwargs.get("reply_markup") is None
    # Logged for the dashboard, but no staff card and no open conversation.
    assert [record.status for record in store.all()] == ["answered"]
    tg.send_message.assert_not_called()
    assert bot.conversations == {}


def test_chat_model_gets_the_facts(baseline, knowledge, tmp_path):
    seen = {}

    class Chat:
        def reply(self, conversation, text, result):
            seen["facts"] = [fact.id for fact in conversation.faq_facts]
            return "The gym is open 6am to 11pm."

    bot, _ = make_bot(baseline, knowledge, tmp_path, Chat())
    call = send(bot, fake_bot(), "when can i use the gym")
    assert call.args[0] == "The gym is open 6am to 11pm."
    assert seen["facts"][0] == "gym-hours"


def test_reports_and_emergencies_are_not_answered_from_the_faq(baseline, knowledge, tmp_path):
    bot, store = make_bot(baseline, knowledge, tmp_path)
    tg = fake_bot()
    send(bot, tg, "The gym treadmill at Block A is broken")
    assert store.all()[0].status != "answered"
    bot.conversations.clear()
    call = send(bot, tg, "Is the fire alarm supposed to ring? There's smoke in the lift lobby")
    assert "emergency number" in call.args[0]
    assert all(conversation.faq_facts == [] for conversation in bot.conversations.values())


def test_follow_up_question_keeps_the_faq_topic(baseline, knowledge, tmp_path):
    seen = []

    class Chat:
        def reply(self, conversation, text, result):
            seen.append((conversation.status, [f.id for f in conversation.faq_facts]))
            seen.append(list(conversation.messages))
            conversation.messages.append({"role": "assistant", "content": "ok"})
            return "ok"

    bot, store = make_bot(baseline, knowledge, tmp_path, Chat())
    tg = fake_bot()
    send(bot, tg, "when does the gym close?")
    # Matches no entry on its own; it means the gym just answered.
    call = send(bot, tg, "can my 15 year old son enter?")
    status, facts = seen[2]
    assert status == "answered" and "gym-hours" in facts
    assert seen[3], "the earlier gym exchange is kept as context"
    assert call.kwargs.get("reply_markup") is None
    assert {record.status for record in store.all()} == {"answered"}
    tg.send_message.assert_not_called()


def test_a_second_question_is_still_a_question(baseline, knowledge, tmp_path):
    bot, store = make_bot(baseline, knowledge, tmp_path)
    tg = fake_bot()
    send(bot, tg, "can my son come in?")
    call = send(bot, tg, "can my 15 year old son enter the condo's gym?")
    # The chat model gets both top entries; the gym one must be among them.
    facts = knowledge.search("can my 15 year old son enter the condo's gym?")
    assert "gym-hours" in [fact.id for fact in facts]
    assert call.kwargs.get("reply_markup") is None
    assert store.all()[-1].status == "answered"
