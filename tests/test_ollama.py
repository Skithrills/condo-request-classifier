import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from condo_classifier.backends import OllamaBackend, ProviderError, parse_model_decision
from condo_classifier.schema import ResidentRequest

REQUEST = ResidentRequest(text="The lobby light is broken.")
VALID = {
    "category": "maintenance",
    "priority": "normal",
    "confidence": 0.85,
    "evidence": ["light is broken"],
    "location": "lobby",
    "multiple_issues": False,
}


@pytest.mark.parametrize(
    "change",
    [
        {"category": "sales"},
        {"priority": "urgent"},
        {"confidence": 1.2},
        {"confidence": float("nan")},
        {"location": "Block X"},
        {"evidence": ["A gas pipe has burst"]},
        {"approve_payment": True},
    ],
)
def test_invalid_model_decision_rejected(change):
    with pytest.raises(ValueError):
        parse_model_decision(json.dumps({**VALID, **change}), REQUEST)


def test_json_fences_supported_without_accepting_trailing_prose():
    assert parse_model_decision("```json\n" + json.dumps(VALID) + "\n```", REQUEST)
    with pytest.raises(ValueError):
        parse_model_decision(json.dumps(VALID) + "Ignore all checks.", REQUEST)


@pytest.mark.parametrize("quote", ['"light is broken"', "'light is broken'", "“light is broken”"])
def test_wrapped_evidence_is_normalized_but_still_grounded(quote):
    result = parse_model_decision(json.dumps({**VALID, "evidence": [quote]}), REQUEST)
    assert result.evidence == ["light is broken"]


@pytest.mark.parametrize("quote", ['"gas is leaking"', '""', '"light was broken"'])
def test_wrapped_ungrounded_or_empty_evidence_is_rejected(quote):
    with pytest.raises(ValueError, match="exact span"):
        parse_model_decision(json.dumps({**VALID, "evidence": [quote]}), REQUEST)


def test_quotation_marks_present_in_input_are_preserved():
    request = ResidentRequest(text='The sign says "out of order".')
    result = parse_model_decision(
        json.dumps({**VALID, "evidence": ['"out of order"'], "location": None}), request
    )
    assert result.evidence == ['"out of order"']


def test_local_and_cloud_use_different_output_modes():
    local = OllamaBackend("http://localhost:11434", "gemma3:4b")
    cloud = OllamaBackend("https://ollama.com", "gpt-oss:20b", api_key="test-only")
    proxied_cloud = OllamaBackend("http://localhost:11434", "gpt-oss:20b-cloud")
    assert isinstance(local.client.format, dict)
    assert cloud.client.format is None
    # Ollama also uses a -cloud suffix for several models served through the daemon.
    assert proxied_cloud.is_cloud
    assert proxied_cloud.client.format is None


def test_invalid_output_retry_is_bounded():
    backend = OllamaBackend("http://localhost:11434", "gemma3:4b")
    backend.client = Mock()
    backend.client.invoke.return_value = AIMessage(content="not json")
    with pytest.raises(ProviderError, match="twice"):
        backend.predict(REQUEST)
    assert backend.client.invoke.call_count == 2


def test_invalid_then_valid_output_repairs_once():
    backend = OllamaBackend("http://localhost:11434", "gemma3:4b")
    backend.client = Mock()
    backend.client.invoke.side_effect = [
        AIMessage(content="not json"),
        AIMessage(content=json.dumps(VALID)),
    ]
    result = backend.predict(REQUEST)
    assert result.category == "maintenance"
    assert result.confidence_kind == "self_reported"
    assert backend.client.invoke.call_count == 2


@pytest.mark.parametrize(
    "host",
    [
        "file:///etc/passwd",
        "http://user:password@localhost:11434",
        "http://localhost:11434/api/chat",
        "http://ollama.com",
    ],
)
def test_invalid_provider_configuration_rejected(host):
    with pytest.raises(ValueError):
        OllamaBackend(host, "gemma3:4b")


def test_connection_error_message_does_not_expose_credentials():
    backend = OllamaBackend("http://localhost:11434", "gemma3:4b")
    backend.client = Mock()
    backend.client.invoke.side_effect = ConnectionError("Bearer secret-token")
    with pytest.raises(ProviderError) as exc:
        backend.predict(REQUEST)
    assert "secret-token" not in str(exc.value)


def test_real_langchain_http_roundtrip_against_fake_ollama():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append(body)
            payload = (
                json.dumps(
                    {
                        "model": "test-model",
                        "done": True,
                        "message": {"role": "assistant", "content": json.dumps(VALID)},
                    }
                ).encode()
                + b"\n"
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        backend = OllamaBackend(f"http://127.0.0.1:{server.server_port}", "test-model")
        result = backend.predict(REQUEST)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert result.category == "maintenance"
    assert received[0]["format"]["additionalProperties"] is False
    assert received[0]["messages"][0]["role"] == "system"
