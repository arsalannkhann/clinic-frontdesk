"""Speech port (ADR 0012): speech-to-text and text-to-speech for the web voice receptionist.

The default deployment is Sarvam REST. Facts verified on 1 November 2026:
- STT `POST {SPEECH_BASE_URL}/speech-to-text`, header `api-subscription-key`, multipart
  `file` (WAV), `model` (saarika:v2.5), `language_code` `unknown` (auto-detect); response
  JSON `{request_id, transcript, language_code, language_probability}`.
- TTS `POST {SPEECH_BASE_URL}/text-to-speech`, same header, JSON `{text,
  target_language_code, model (bulbul:v3), speaker, speech_sample_rate, output_audio_codec}`;
  response JSON `{request_id, audios: [<base64>]}`.

The adapter is a thin, stateless HTTP client. Errors are SpeechError with a short code
(timeout, transport, unauthorized, http_<status>, malformed); no audio, transcript, request
or response body, header or provider message is ever placed in an exception or a log line.
"""

import base64
import binascii
from threading import Lock
from typing import Protocol

import httpx


class SpeechError(Exception):
    """The speech call failed. `code` is a fixed short string (never provider text)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class SpeechPort(Protocol):
    def transcribe(self, wav: bytes) -> tuple[str, str | None]:
        """The transcript of one WAV utterance and the detected language code (or None)."""

    def synthesize(self, text: str, language: str) -> bytes:
        """MP3 audio speaking `text` in `language` (a BCP-47 code such as hi-IN)."""


class SarvamSpeech:
    """SpeechPort over Sarvam's REST speech-to-text and text-to-speech endpoints."""

    def __init__(
        self,
        client: httpx.Client,
        base_url: str,
        api_key: str,
        stt_model: str = "saarika:v2.5",
        tts_model: str = "bulbul:v3",
        speaker: str = "priya",
    ):
        self._client = client
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._stt_model = stt_model
        self._tts_model = tts_model
        self._speaker = speaker

    def __repr__(self):
        return f"SarvamSpeech(stt={self._stt_model!r}, tts={self._tts_model!r})"  # Never the key.

    def _post(self, path: str, **kwargs):
        try:
            response = self._client.post(
                self._base + path, headers={"api-subscription-key": self._key}, **kwargs
            )
        except httpx.TimeoutException:
            raise SpeechError("timeout") from None
        except httpx.HTTPError:
            raise SpeechError("transport") from None
        if response.status_code in (401, 403):
            raise SpeechError("unauthorized")
        if not response.is_success:
            raise SpeechError(f"http_{response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise SpeechError("malformed") from None
        if not isinstance(data, dict):
            raise SpeechError("malformed")
        return data

    def transcribe(self, wav):
        data = self._post(
            "/speech-to-text",
            files={"file": ("utterance.wav", wav, "audio/wav")},
            data={"model": self._stt_model, "language_code": "unknown"},
        )
        text = data.get("transcript")
        if not isinstance(text, str):
            raise SpeechError("malformed")
        language = data.get("language_code")
        return text, language if isinstance(language, str) and language else None

    def synthesize(self, text, language):
        data = self._post(
            "/text-to-speech",
            json={
                "text": text,
                "target_language_code": language,
                "model": self._tts_model,
                "speaker": self._speaker,
                "speech_sample_rate": 16000,
                "output_audio_codec": "mp3",
            },
        )
        audios = data.get("audios")
        if not isinstance(audios, list) or not audios or not isinstance(audios[0], str):
            raise SpeechError("malformed")
        try:
            audio = base64.b64decode(audios[0], validate=True)
        except (binascii.Error, ValueError):
            raise SpeechError("malformed") from None
        if not audio:
            raise SpeechError("malformed")
        return audio


class FakeSpeech:
    """Scripted SpeechPort for tests. `transcripts` items are (text, language) tuples or
    exception instances, consumed in order (then `default_transcript`). `synthesize` returns
    `audio` (or raises `tts_error` when set). `stt_calls` and `tts_calls` record the calls."""

    def __init__(self, transcripts=None, default_transcript=("", None), audio=b"ID3fake-mp3"):
        self._lock = Lock()
        self.transcripts = list(transcripts or [])
        self.default_transcript = default_transcript
        self.audio = audio
        self.tts_error: BaseException | None = None
        self.stt_calls: list[int] = []  # Byte lengths only.
        self.tts_calls: list[tuple[str, str]] = []

    def transcribe(self, wav):
        with self._lock:
            self.stt_calls.append(len(wav))
            item = self.transcripts.pop(0) if self.transcripts else self.default_transcript
        if isinstance(item, BaseException):
            raise item
        return item

    def synthesize(self, text, language):
        with self._lock:
            self.tts_calls.append((text, language))
        if self.tts_error is not None:
            raise self.tts_error
        return self.audio


def build(settings) -> SpeechPort | None:
    """The configured adapter, or None when SPEECH_API_KEY (or SARVAM_API_KEY) is unset."""
    if not settings.speech_configured():
        return None
    return SarvamSpeech(
        httpx.Client(timeout=settings.speech_timeout_seconds, follow_redirects=False),
        settings.speech_base_url,
        settings.speech_api_key.get_secret_value(),
        settings.speech_stt_model,
        settings.speech_tts_model,
        settings.speech_tts_speaker,
    )
