from types import SimpleNamespace
from unittest.mock import Mock

import anthropic
import httpx2
import pytest

from condo_classifier.backends import ClaudeBackend, ProviderError, backend_from_name
from condo_classifier.schema import Category, ModelDecision, ResidentRequest

REQUEST = ResidentRequest(text="The lobby light is broken.")
VALID = {
    "category": "maintenance",
    "priority": "normal",
    "confidence": 0.85,
    "evidence": ["light is broken"],
    "location": "lobby",
    "multiple_issues": False,
}


def reply(stop_reason="end_turn", **changes):
    decision = ModelDecision.model_validate({**VALID, **changes})
    return SimpleNamespace(stop_reason=stop_reason, parsed_output=decision)


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    backend = ClaudeBackend()
    backend.client = Mock()
    return backend


def test_structured_decision_becomes_self_reported_prediction(backend):
    backend.client.messages.parse.return_value = reply()
    prediction = backend.predict(REQUEST)
    assert prediction.category == Category.MAINTENANCE
    assert prediction.location == "lobby"
    assert prediction.confidence_kind == "self_reported"
    assert prediction.backend == "claude"
    kwargs = backend.client.messages.parse.call_args.kwargs
    assert kwargs["model"] == "claude-haiku-5-5"
    assert kwargs["output_format"] is ModelDecision
    assert kwargs["output_config"] == {"effort": "low"}
    assert "temperature" not in kwargs
    assert REQUEST.text in kwargs["messages"][0]["content"]


def test_ungrounded_evidence_is_retried_once(backend):
    backend.client.messages.parse.side_effect = [
        reply(evidence=["A gas pipe has burst"]),
        reply(),
    ]
    assert backend.predict(REQUEST).evidence == ["light is broken"]
    assert backend.client.messages.parse.call_count == 2
    assert "exact spans" in backend.client.messages.parse.call_args.kwargs["system"]


def test_invalid_output_twice_needs_review(backend):
    backend.client.messages.parse.return_value = reply(location="Block X")
    with pytest.raises(ProviderError, match="twice"):
        backend.predict(REQUEST)


def test_refusal_is_a_provider_error(backend):
    backend.client.messages.parse.return_value = SimpleNamespace(
        stop_reason="refusal", parsed_output=None
    )
    with pytest.raises(ProviderError, match="declined"):
        backend.predict(REQUEST)
    assert backend.client.messages.parse.call_count == 1


def test_api_errors_do_not_leak_details(backend):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    backend.client.messages.parse.side_effect = anthropic.APIConnectionError(request=request)
    with pytest.raises(ProviderError) as error:
        backend.predict(REQUEST)
    assert "API key" in str(error.value)
    assert REQUEST.text not in str(error.value)


@pytest.mark.parametrize(
    "env,message",
    [({"CLAUDE_MODEL": " "}, "model"), ({"CLAUDE_EFFORT": "max"}, "low, medium or high")],
)
def test_invalid_claude_settings_rejected(monkeypatch, env, message):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match=message):
        backend_from_name("claude")


def test_unknown_backend_rejected():
    with pytest.raises(ValueError, match="baseline, ollama, claude"):
        backend_from_name("gpt")
