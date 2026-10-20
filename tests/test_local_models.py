from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from condo_classifier.backends import (
    BaselineBackend,
    FallbackBackend,
    ProviderError,
    backend_with_fallback,
    parse_fallbacks,
)
from condo_classifier.chat import Conversation, FallbackChat, OllamaChat
from condo_classifier.knowledge import Fact
from condo_classifier.schema import ResidentRequest
from condo_classifier.vision import OllamaPhotoDescriber
from tests.test_chat import result
from tests.test_photos import jpeg


class Broken:
    def predict(self, request):
        raise ProviderError("Claude did not respond successfully.")


def test_fallback_backend_answers_when_the_main_one_fails(baseline):
    request = ResidentRequest(text="The corridor light outside #05-12 keeps flickering.")
    prediction = FallbackBackend(Broken(), baseline).predict(request)
    assert prediction.backend == "baseline"
    assert prediction.category.value == "maintenance"


def test_fallback_chain_is_built_in_order(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    backend = backend_with_fallback("claude", ["ollama", "baseline"])
    assert type(backend.primary).__name__ == "ClaudeBackend"
    assert type(backend.fallback.primary).__name__ == "OllamaBackend"
    assert isinstance(backend.fallback.fallback, BaselineBackend)
    assert isinstance(backend_with_fallback("baseline", ["baseline"]), BaselineBackend)


def test_fallback_names_are_checked():
    assert parse_fallbacks("ollama, baseline") == ["ollama", "baseline"]
    assert parse_fallbacks("none") == parse_fallbacks("") == []
    with pytest.raises(ValueError, match="CLASSIFIER_FALLBACK"):
        parse_fallbacks("gemini")


def ollama_response(text):
    return SimpleNamespace(message=SimpleNamespace(content=text))


@pytest.fixture
def ollama_chat():
    chat = OllamaChat("phi4-mini")
    chat.client = Mock()
    return chat


def test_ollama_chat_needs_no_key_and_keeps_one_system_message(ollama_chat):
    ollama_chat.client.chat.return_value = ollama_response("Which block is it?")
    conversation = Conversation()
    assert ollama_chat.reply(conversation, "Light is flickering", result()) == (
        "Which block is it?"
    )
    ollama_chat.client.chat.return_value = ollama_response("Thanks, it's ready.")
    conversation.faq_facts = [Fact("gym-hours", "Gym hours?", "6am to 11pm")]
    ollama_chat.reply(conversation, "Block 4", result(location="Block 4", missing_details=[]))
    sent = ollama_chat.client.chat.call_args.kwargs
    assert sent["model"] == "phi4-mini"
    roles = [m["role"] for m in sent["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    system = sent["messages"][0]["content"]
    assert "location: Block 4" in system and "6am to 11pm" in system
    # Stored history matches the Claude format, so providers can be swapped mid-chat.
    assert [m["role"] for m in conversation.messages] == ["user", "system", "assistant"] * 2


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("refused"), ConnectionError("Failed to connect to Ollama")]
)
def test_ollama_chat_failure_lets_the_bot_fall_back(ollama_chat, error):
    ollama_chat.client.chat.side_effect = error
    conversation = Conversation()
    assert ollama_chat.reply(conversation, "Light is flickering", result()) is None
    assert conversation.messages == []


def test_fallback_chat_tries_the_next_model():
    first, second = Mock(), Mock()
    first.reply.return_value = None
    second.reply.return_value = "From Ollama"
    assert FallbackChat(first, second).reply(Conversation(), "hi", result()) == "From Ollama"


def test_ollama_photos_send_the_image_to_a_vision_model():
    describer = OllamaPhotoDescriber("gemma3:4b")
    describer.client = Mock()
    describer.client.chat.return_value = ollama_response("Water stain on a ceiling.")
    assert describer.describe(jpeg(800, 600), "ceiling") == "Water stain on a ceiling."
    message = describer.client.chat.call_args.kwargs["messages"][1]
    assert message["images"][0][:2] == b"\xff\xd8"
    assert "ceiling" in message["content"]
    describer.client.chat.side_effect = httpx.ConnectError("refused")
    assert describer.describe(jpeg(800, 600)) is None


def test_ollama_photos_need_a_vision_model():
    with pytest.raises(ValueError, match="OLLAMA_VISION_MODEL"):
        OllamaPhotoDescriber("")


def local_env(monkeypatch, tmp_path, **env):
    pytest.importorskip("telegram")
    for key in ("ANTHROPIC_API_KEY", "STAFF_CHAT_ID", "OLLAMA_VISION_MODEL", "FAQ"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("REQUESTS_DB", str(tmp_path / "requests.db"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_local_only_setup_needs_no_api_key(monkeypatch, tmp_path):
    local_env(
        monkeypatch,
        tmp_path,
        CLASSIFIER_BACKEND="ollama",
        CLASSIFIER_FALLBACK="baseline",
        BOT_REPLIES="ollama",
        PHOTOS="ollama",
        OLLAMA_MODEL="phi4-mini",
    )
    from condo_classifier.telegram_bot import build_bot

    bot = build_bot()
    assert isinstance(bot.chat, OllamaChat)
    assert bot.chat.model == "phi4-mini"
    assert isinstance(bot.classifier.backend, FallbackBackend)
    # phi4-mini can't see images, so photos stay off until a vision model is set.
    assert bot.describer is None
    assert bot.knowledge is not None

    monkeypatch.setenv("OLLAMA_VISION_MODEL", "gemma3:4b")
    assert isinstance(build_bot().describer, OllamaPhotoDescriber)


def test_claude_with_local_fallbacks(monkeypatch, tmp_path):
    local_env(
        monkeypatch,
        tmp_path,
        ANTHROPIC_API_KEY="test-key",
        CLASSIFIER_BACKEND="claude",
        CLASSIFIER_FALLBACK="ollama,baseline",
        BOT_REPLIES="chat",
        CHAT_FALLBACK="ollama",
        FAQ="off",
    )
    from condo_classifier.telegram_bot import build_bot

    bot = build_bot()
    assert isinstance(bot.chat, FallbackChat)
    assert [type(c).__name__ for c in bot.chat.chats] == ["ResidentChat", "OllamaChat"]
    assert bot.knowledge is None


@pytest.mark.parametrize(
    "env,message",
    [
        ({"BOT_REPLIES": "claude", "CLASSIFIER_BACKEND": "ollama"}, "ANTHROPIC_API_KEY"),
        ({"BOT_REPLIES": "ollama", "CLASSIFIER_FALLBACK": "claude"}, "ANTHROPIC_API_KEY"),
        ({"BOT_REPLIES": "ollama", "CHAT_FALLBACK": "gemini"}, "CHAT_FALLBACK"),
        ({"BOT_REPLIES": "ollama", "PHOTOS": "maybe"}, "PHOTOS"),
        ({"BOT_REPLIES": "ollama", "FAQ": "yes"}, "FAQ must be on or off"),
    ],
)
def test_local_setup_errors_are_explained(monkeypatch, tmp_path, env, message):
    local_env(monkeypatch, tmp_path, **env)
    from condo_classifier.telegram_bot import build_bot

    with pytest.raises(SystemExit, match=message):
        build_bot()
