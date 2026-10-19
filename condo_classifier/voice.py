"""Transcribe Telegram voice notes with Whisper, in the language the resident spoke.

Providers are tried in order (TRANSCRIBE_WITH=groq,local). Audio is processed in memory and
never saved or logged. Translation to English happens afterwards, like for typed messages.
"""

import io
import logging
import os
import threading
import wave

import httpx

from condo_classifier.language import GROQ_BASE_URL

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000  # What Whisper uses internally; keeps uploads small.


class TranscriptionError(RuntimeError):
    """Safe error message that excludes provider payloads, keys and audio."""


def to_wav(audio: bytes) -> bytes:
    """Decode a voice note (OGG/Opus from Telegram) to 16 kHz mono 16-bit WAV."""
    import av  # Installed with the telegram extra.

    pcm = bytearray()
    try:
        with av.open(io.BytesIO(audio)) as container:
            resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
            for frame in container.decode(audio=0):
                for resampled in resampler.resample(frame):
                    pcm += resampled.to_ndarray().tobytes()
            for resampled in resampler.resample(None):
                pcm += resampled.to_ndarray().tobytes()
    except (av.FFmpegError, IndexError) as exc:
        raise TranscriptionError("The voice message could not be decoded.") from exc
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(bytes(pcm))
    return output.getvalue()


class GroqTranscriber:
    """Whisper on Groq's OpenAI-compatible API. The free plan includes Whisper models."""

    name = "groq"

    def __init__(self, api_key: str, model: str = "whisper-large-v3", *, timeout=30.0):
        if not api_key:
            raise ValueError("Set GROQ_API_KEY in .env to transcribe with Groq.")
        self.model = model
        self.client = httpx.Client(
            base_url=GROQ_BASE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    def transcribe(self, audio: bytes) -> str:
        wav = to_wav(audio)
        try:
            response = self.client.post(
                "/audio/transcriptions",
                files={"file": ("voice.wav", wav, "audio/wav")},
                data={"model": self.model, "response_format": "json", "temperature": "0"},
            )
            response.raise_for_status()
            return response.json()["text"].strip()
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise TranscriptionError(f"Groq transcription failed: {type(exc).__name__}") from exc


class LocalTranscriber:
    """faster-whisper on this machine. The model downloads on first use."""

    name = "local"

    def __init__(self, model: str = "large-v3-turbo", *, device: str = "auto"):
        try:
            import faster_whisper  # noqa: F401
        except ImportError as exc:
            raise ValueError(
                "Local transcription needs the voice-local extra. Run the bot with "
                "uv run --extra telegram --extra voice-local "
                "python -m condo_classifier.telegram_bot"
            ) from exc
        self.model_name = model
        self.device = device
        self.model = None
        # One voice note at a time: loading twice would double memory, and it is CPU-bound.
        self.lock = threading.Lock()

    def transcribe(self, audio: bytes) -> str:
        from faster_whisper import WhisperModel

        with self.lock:
            try:
                if self.model is None:
                    log.info("Loading local Whisper model %s", self.model_name)
                    self.model = WhisperModel(
                        self.model_name, device=self.device, compute_type="int8"
                    )
                segments, _ = self.model.transcribe(io.BytesIO(audio), beam_size=5, vad_filter=True)
                return " ".join(segment.text.strip() for segment in segments).strip()
            except Exception as exc:  # ctranslate2 and PyAV raise many unrelated types.
                raise TranscriptionError(
                    f"Local transcription failed: {type(exc).__name__}"
                ) from exc


class TranscriberChain:
    def __init__(self, transcribers: list):
        if not transcribers:
            raise ValueError("Add at least one transcriber.")
        self.transcribers = transcribers

    def transcribe(self, audio: bytes) -> str:
        for transcriber in self.transcribers:
            try:
                text = transcriber.transcribe(audio)
            except TranscriptionError as exc:
                log.warning("Transcriber %s failed: %s", transcriber.name, exc)
                continue
            if text:
                return text
            log.warning("Transcriber %s heard no speech", transcriber.name)
        raise TranscriptionError("No transcriber returned any speech.")


TRANSCRIBERS = ("groq", "local")


def transcriber_from_env() -> TranscriberChain | None:
    """Build the chain from TRANSCRIBE_WITH, or None to keep voice messages turned off."""
    names = [name.strip() for name in os.getenv("TRANSCRIBE_WITH", "").split(",") if name.strip()]
    if not names:
        return None
    transcribers = []
    for name in names:
        if name == "groq":
            transcribers.append(
                GroqTranscriber(
                    os.getenv("GROQ_API_KEY", ""),
                    os.getenv("GROQ_WHISPER_MODEL", "whisper-large-v3"),
                    timeout=float(os.getenv("TRANSCRIBE_TIMEOUT_SECONDS", "30")),
                )
            )
        elif name == "local":
            transcribers.append(
                LocalTranscriber(
                    os.getenv("LOCAL_WHISPER_MODEL", "large-v3-turbo"),
                    device=os.getenv("LOCAL_WHISPER_DEVICE", "auto"),
                )
            )
        else:
            raise ValueError(f"TRANSCRIBE_WITH entries must be: {', '.join(TRANSCRIBERS)}.")
    return TranscriberChain(transcribers)
