import io
import math
import wave
from types import SimpleNamespace

import httpx
import pytest

from condo_classifier.language import GROQ_BASE_URL
from condo_classifier.voice import (
    GroqTranscriber,
    LocalTranscriber,
    TranscriberChain,
    TranscriptionError,
    to_wav,
    transcriber_from_env,
)


@pytest.fixture(scope="module")
def voice_note():
    """Two seconds of tone encoded like a Telegram voice note (OGG/Opus, 48 kHz mono)."""
    av = pytest.importorskip("av")
    np = pytest.importorskip("numpy")
    buffer = io.BytesIO()
    with av.open(buffer, "w", format="ogg") as container:
        stream = container.add_stream("libopus", rate=48000, layout="mono")
        t = np.arange(48000 * 2) / 48000
        samples = (np.sin(2 * math.pi * 440 * t) * 8000).astype(np.int16).reshape(1, -1)
        for start in range(0, samples.shape[1], 960):
            frame = av.AudioFrame.from_ndarray(
                samples[:, start : start + 960], format="s16", layout="mono"
            )
            frame.sample_rate = 48000
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return buffer.getvalue()


def test_voice_note_becomes_16khz_mono_wav(voice_note):
    with wave.open(io.BytesIO(to_wav(voice_note))) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
        assert wav.getnframes() == pytest.approx(32000, abs=400)


@pytest.mark.parametrize("audio", [b"", b"definitely not audio"])
def test_undecodable_audio_is_a_transcription_error(audio):
    pytest.importorskip("av")
    with pytest.raises(TranscriptionError, match="could not be decoded"):
        to_wav(audio)


def groq(handler):
    transcriber = GroqTranscriber("gsk-test")
    transcriber.client = httpx.Client(
        base_url=GROQ_BASE_URL,
        headers=transcriber.client.headers,
        transport=httpx.MockTransport(handler),
    )
    return transcriber


def test_groq_uploads_wav_and_returns_text(voice_note):
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.read()
        return httpx.Response(200, json={"text": " 电梯坏了 "})

    assert groq(handler).transcribe(voice_note) == "电梯坏了"
    assert seen["path"] == "/openai/v1/audio/transcriptions"
    assert seen["auth"] == "Bearer gsk-test"
    assert b'filename="voice.wav"' in seen["body"]
    assert b"whisper-large-v3" in seen["body"]
    assert b"RIFF" in seen["body"]


def test_groq_failure_is_a_transcription_error(voice_note):
    with pytest.raises(TranscriptionError, match="Groq transcription failed"):
        groq(lambda request: httpx.Response(429)).transcribe(voice_note)


def test_local_joins_segments(monkeypatch):
    pytest.importorskip("faster_whisper")
    transcriber = LocalTranscriber()
    segments = [SimpleNamespace(text=" Lif di Blok 5 "), SimpleNamespace(text="rosak. ")]
    transcriber.model = SimpleNamespace(transcribe=lambda audio, **_: (iter(segments), None))
    assert transcriber.transcribe(b"ogg") == "Lif di Blok 5 rosak."


class Fixed:
    def __init__(self, name, text=None):
        self.name = name
        self.text = text

    def transcribe(self, audio):
        if self.text is None:
            raise TranscriptionError("down")
        return self.text


def test_chain_skips_failures_and_silence():
    chain = TranscriberChain([Fixed("groq"), Fixed("silent", ""), Fixed("local", "电梯坏了")])
    assert chain.transcribe(b"ogg") == "电梯坏了"
    with pytest.raises(TranscriptionError):
        TranscriberChain([Fixed("groq"), Fixed("silent", "")]).transcribe(b"ogg")


def test_transcriber_from_env(monkeypatch):
    monkeypatch.delenv("TRANSCRIBE_WITH", raising=False)
    assert transcriber_from_env() is None
    monkeypatch.setenv("TRANSCRIBE_WITH", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "")
    with pytest.raises(ValueError, match="GROQ_API_KEY"):
        transcriber_from_env()
    monkeypatch.setenv("TRANSCRIBE_WITH", "siri")
    with pytest.raises(ValueError, match="groq, local"):
        transcriber_from_env()
