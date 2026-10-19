import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from condo_classifier.language import (
    GROQ_BASE_URL,
    ClaudeTranslator,
    GroqTranslator,
    OllamaTranslator,
    Translation,
    TranslationError,
    TranslationOutput,
    TranslatorChain,
    language_name,
    parse_translation,
    translator_from_env,
)


def mock_client(base_url, handler, headers=None):
    return httpx.Client(base_url=base_url, headers=headers, transport=httpx.MockTransport(handler))


def test_parse_translation_reads_json_and_fences():
    content = '```json\n{"language": "zh", "english": "The lift is broken at Blk 5"}\n```'
    assert parse_translation(content, "5座电梯坏了") == Translation(
        "zh", "The lift is broken at Blk 5"
    )
    assert parse_translation('{"language": "zh-Hans", "english": "Hi"}', "你好").language == "zh"


def test_english_is_kept_verbatim():
    # Even if the model tidies Singlish up, the classifier reads the resident's exact words.
    translation = parse_translation(
        '{"language": "en", "english": "The light is broken."}', "the light spoil already lah"
    )
    assert translation == Translation("en", "the light spoil already lah")
    assert not translation.translated


@pytest.mark.parametrize("content", ["not json", '{"language": "zh"}', '{"english": "x"}', ""])
def test_bad_translation_output_is_an_error(content):
    with pytest.raises(TranslationError):
        parse_translation(content, "电梯坏了")


def test_groq_request_and_reply():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        content = '{"language": "ms", "english": "The lift at Block 5 is broken"}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    translator = GroqTranslator("gsk-test")
    translator.client = mock_client(GROQ_BASE_URL, handler, translator.client.headers)
    assert translator.translate("Lif di Blok 5 rosak").english == "The lift at Block 5 is broken"
    assert seen["auth"] == "Bearer gsk-test"
    assert seen["body"]["model"] == "openai/gpt-oss-120b"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["body"]["messages"][1] == {"role": "user", "content": "Lif di Blok 5 rosak"}


@pytest.mark.parametrize(
    "response",
    [httpx.Response(429, json={"error": "rate limited"}), httpx.Response(200, json={})],
)
def test_groq_failures_raise_translation_error(response):
    translator = GroqTranslator("gsk-test")
    translator.client = mock_client(GROQ_BASE_URL, lambda request: response)
    with pytest.raises(TranslationError, match="Groq translation failed"):
        translator.translate("电梯坏了")


def test_ollama_request_and_reply():
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/api/chat"
        assert body["format"] == "json" and body["stream"] is False
        content = '{"language": "ta", "english": "The lift is broken"}'
        return httpx.Response(200, json={"message": {"content": content}})

    translator = OllamaTranslator("http://localhost:11434", "gemma3:4b")
    translator.client = mock_client("http://localhost:11434", handler)
    assert translator.translate("லிஃப்ட் பழுதாகிவிட்டது") == Translation("ta", "The lift is broken")


def test_claude_translator(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    translator = ClaudeTranslator()
    translator.client = Mock()
    translator.client.messages.parse.return_value = SimpleNamespace(
        stop_reason="end_turn",
        parsed_output=TranslationOutput(language="zh", english="The lift is broken"),
    )
    assert translator.translate("电梯坏了") == Translation("zh", "The lift is broken")
    translator.client.messages.parse.return_value = SimpleNamespace(
        stop_reason="refusal", parsed_output=None
    )
    with pytest.raises(TranslationError):
        translator.translate("电梯坏了")


class Failing:
    name = "failing"

    def translate(self, text):
        raise TranslationError("down")


class Working:
    name = "working"

    def translate(self, text):
        return Translation("zh", "The lift is broken")


def test_chain_falls_back_in_order():
    assert TranslatorChain([Failing(), Working()]).translate("电梯坏了").english == (
        "The lift is broken"
    )
    with pytest.raises(TranslationError, match="Every translator failed"):
        TranslatorChain([Failing(), Failing()]).translate("电梯坏了")


def test_translator_from_env(monkeypatch):
    monkeypatch.delenv("TRANSLATE_WITH", raising=False)
    assert translator_from_env() is None
    monkeypatch.setenv("TRANSLATE_WITH", "groq, ollama")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    chain = translator_from_env()
    assert [t.name for t in chain.translators] == ["groq", "ollama"]


@pytest.mark.parametrize(
    "env,message",
    [
        ({"TRANSLATE_WITH": "groq", "GROQ_API_KEY": ""}, "GROQ_API_KEY"),
        ({"TRANSLATE_WITH": "claude", "ANTHROPIC_API_KEY": ""}, "ANTHROPIC_API_KEY"),
        ({"TRANSLATE_WITH": "google"}, "groq, ollama, claude"),
    ],
)
def test_translator_from_env_explains_setup(monkeypatch, env, message):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match=message):
        translator_from_env()


def test_language_names():
    assert language_name("ta") == "Tamil"
    assert language_name("fr") == "fr"
