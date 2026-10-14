from types import SimpleNamespace
from unittest.mock import Mock

import anthropic
import httpx2
import pytest

from condo_classifier.chat import MAX_MESSAGES, Conversation, ResidentChat, classifier_note
from condo_classifier.schema import Category, Classification, Priority


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
            "confidence_kind": "self_reported",
            "category_scores": {},
            "evidence": [],
            "backend": "test",
            "model": "test",
            "status": "needs_review",
            "elapsed_ms": 1.0,
            **changes,
        }
    )


def response(*texts, stop_reason="end_turn"):
    blocks = [SimpleNamespace(type="thinking", thinking="")]
    blocks += [SimpleNamespace(type="text", text=text) for text in texts]
    return SimpleNamespace(stop_reason=stop_reason, content=blocks)


@pytest.fixture
def chat(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    chat = ResidentChat()
    chat.client = Mock()
    return chat


def test_note_carries_classifier_output():
    note = classifier_note(result(), escalated=False)
    assert "category: Maintenance" in note
    assert "missing details: Block, unit or shared-area location" in note
    assert "location: not given" in note
    assert "emergency already flagged: no" in note


def test_reply_records_append_only_history(chat):
    chat.client.messages.create.return_value = response("Sure! ", "Which block is it?")
    conversation = Conversation()
    assert chat.reply(conversation, "Light is flickering", result()) == "Sure! Which block is it?"
    kwargs = chat.client.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-haiku-5-5"
    assert kwargs["output_config"] == {"effort": "low"}
    assert "temperature" not in kwargs
    assert [m["role"] for m in kwargs["messages"]] == ["user", "system"]
    assert [m["role"] for m in conversation.messages] == ["user", "system", "assistant"]

    chat.reply(conversation, "Block 4", result(location="Block 4", missing_details=[]))
    sent = chat.client.messages.create.call_args.kwargs["messages"]
    assert sent[:3] == conversation.messages[:3]
    assert sent[3]["content"] == "Block 4"


def test_long_conversations_drop_whole_old_turns(chat):
    chat.client.messages.create.return_value = response("ok")
    conversation = Conversation()
    for index in range(MAX_MESSAGES // 3 + 2):
        chat.reply(conversation, f"message {index}", result())
    assert len(conversation.messages) == MAX_MESSAGES
    assert conversation.messages[0]["role"] == "user"
    assert conversation.messages[0]["content"] == "message 2"


@pytest.mark.parametrize(
    "outcome",
    [
        response("nope", stop_reason="refusal"),
        response(),
        anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com")),
    ],
)
def test_failures_return_none_without_recording(chat, outcome):
    if isinstance(outcome, Exception):
        chat.client.messages.create.side_effect = outcome
    else:
        chat.client.messages.create.return_value = outcome
    conversation = Conversation()
    assert chat.reply(conversation, "Light is flickering", result()) is None
    assert conversation.messages == []


def test_request_text_keeps_the_newest_4000_characters():
    conversation = Conversation(resident_texts=["a" * 3000, "b" * 3000])
    assert conversation.request_text() == "a" * 999 + "\n" + "b" * 3000
