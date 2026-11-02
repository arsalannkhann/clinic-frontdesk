"""The web voice receptionist (ADR 0012): speech-to-text, the chat turn, text-to-speech.

A voice turn is one recorded utterance (16 kHz mono 16-bit PCM WAV, base64 in JSON). The
session is checked first (the same 404, 429 and 409 refusals as a typed message, before any
speech call is paid for), then the audio is transcribed; an empty transcript is 422 no_speech
and consumes no turn. The transcript then runs as an ordinary chat turn (chat.send with
voice=True: the same tools, proposal flow, emergency script, limits and encrypted history,
plus call rules in the system prompt), and the reply is synthesized. A call books by voice:
the model holds a time (propose_booking), asks for the name and mobile number, reads the number
back, asks consent, and books it (book_appointment, through chat.confirm's checks and limits).

Audio is never stored or logged. The transcript is stored only as the session's encrypted
user message (chat.send). Logs carry session ids and error codes only. A failed synthesis
still answers the text with audio null.
"""

import base64
import binascii
import logging
import re
import time
from threading import Lock

from clinic_voice import chat, public, service
from clinic_voice.db import connect
from clinic_voice.errors import PublicError
from clinic_voice.providers.speech import SpeechError, SpeechPort

log = logging.getLogger("clinic_voice.voice")

AUDIO_MIME = "audio/mpeg"
MAX_TRANSCRIPT = 1000  # schemas.ChatText
MAX_TTS_CHARS = 900
GREETING_CACHE_SIZE = 256

# Text-to-speech target languages (Sarvam bulbul). Replies in an Indic script are spoken in
# that script's language; Latin text in Hindi (Hinglish) when the patient spoke Hindi, else
# Indian English.
SCRIPTS = [
    (re.compile(r"[ঀ-৿]"), "bn-IN"),
    (re.compile(r"[਀-੿]"), "pa-IN"),
    (re.compile(r"[઀-૿]"), "gu-IN"),
    (re.compile(r"[଀-୿]"), "od-IN"),
    (re.compile(r"[஀-௿]"), "ta-IN"),
    (re.compile(r"[ఀ-౿]"), "te-IN"),
    (re.compile(r"[ಀ-೿]"), "kn-IN"),
    (re.compile(r"[ഀ-ൿ]"), "ml-IN"),
]
DEVANAGARI = re.compile(r"[ऀ-ॿ]")
DEFAULT_LANGUAGE = "en-IN"

CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
URL = re.compile(r"https?://\S+")
BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+", re.M)
EMPHASIS = re.compile(r"[*_`~]+")
SYMBOLS = re.compile(r"[#>|\[\]{}<]+")
SPACE = re.compile(r"\s+")


def unavailable() -> PublicError:
    return chat.fail("voice_unavailable", 503, "Voice is unavailable; please type instead")


def no_speech() -> PublicError:
    return chat.fail("no_speech", 422, "We couldn't hear that. Please try again.")


def invalid_audio() -> PublicError:
    # The same envelope as a schema failure of the field (api.validation_error).
    return chat.fail("invalid_request", 422, "Invalid request: audio")


def decode_wav(audio: str, max_bytes: int) -> bytes:
    """The decoded recording, or 422 invalid_request: not base64, larger than max_bytes, or
    without a RIFF/WAVE header."""
    try:
        wav = base64.b64decode(audio, validate=True)
    except (binascii.Error, ValueError):
        raise invalid_audio() from None
    if len(wav) > max_bytes or len(wav) < 44 or wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise invalid_audio()
    return wav


def clean_transcript(text: str) -> str:
    return SPACE.sub(" ", CONTROL.sub(" ", text)).strip()[:MAX_TRANSCRIPT].strip()


def speakable(text: str) -> str:
    """Reply text for synthesis: no Markdown, links or symbols, at most MAX_TTS_CHARS (cut at
    a sentence end or a space)."""
    text = LINK.sub(r"\1", text)
    text = URL.sub("", text)
    text = BULLET.sub("", text)
    text = SPACE.sub(" ", SYMBOLS.sub(" ", EMPHASIS.sub("", text))).strip()
    if len(text) <= MAX_TTS_CHARS:
        return text
    cut = text[:MAX_TTS_CHARS]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "), cut.rfind("।"))
    if end >= MAX_TTS_CHARS // 2:
        return cut[: end + 1].strip()
    space = cut.rfind(" ")
    return (cut[:space] if space > 0 else cut).strip()


def tts_language(reply: str, heard: str | None) -> str:
    """The synthesis language for a reply, from its script and the language heard."""
    if DEVANAGARI.search(reply):
        return "mr-IN" if heard == "mr-IN" else "hi-IN"
    for pattern, code in SCRIPTS:
        if pattern.search(reply):
            return code
    return "hi-IN" if heard == "hi-IN" else DEFAULT_LANGUAGE


def _synthesize(speech: SpeechPort, text: str, language: str, session_id) -> str | None:
    """Base64 MP3, or None (logged by code) when synthesis fails or there is nothing to say."""
    spoken = speakable(text)
    if not spoken:
        return None
    try:
        audio = speech.synthesize(spoken, language)
    except SpeechError as error:
        log.warning("voice tts failed session=%s code=%s", session_id, error.code)
        return None
    except Exception as error:  # Never fail the turn over audio; type only in the log.
        log.error("voice tts failed session=%s error=%s", session_id, type(error).__name__)
        return None
    return base64.b64encode(audio).decode("ascii")


def _precheck(url, settings, token, address):
    """The session for a voice turn, refused exactly as chat.send would refuse the turn
    (404 not_found, 429 rate_limited, 409 turn_in_progress / conversation_limit) before any
    speech call. chat.send re-checks all of it under the session lock. Recordings count
    against CHAT_MESSAGE_IP_PER_HOUR in their own bucket, so an utterance that turns out to
    be silence still costs the caller one request."""
    _, hash_key = public.crypto(settings)
    with connect(url) as conn:
        session = chat.find_session(conn, token)
        public.limit(
            conn,
            hash_key,
            "chat-voice-ip",
            settings.chat_message_ip_per_hour,
            public.HOUR,
            public.client_value(address),
        )
    if session["busy_until"] is not None and session["busy_until"] > service.utc_now():
        raise chat.fail(
            "turn_in_progress", 409, "The assistant is still answering your previous message"
        )
    if session["turn_count"] >= settings.chat_max_turns:
        raise chat.fail(
            "conversation_limit",
            409,
            "This conversation is full; please start a new chat or call the clinic",
        )
    return session


def turn(url, settings, token, audio: str, address, llm, speech: SpeechPort | None) -> dict:
    """POST /v1/chat/sessions/{id}/voice: {messages, proposal?, quick_replies, transcript,
    audio, audio_mime}."""
    if speech is None:
        raise unavailable()
    session = _precheck(url, settings, token, address)
    wav = decode_wav(audio, settings.voice_max_audio_bytes)
    started = time.monotonic()
    try:
        heard, language = speech.transcribe(wav)
    except SpeechError as error:
        log.warning("voice stt failed session=%s code=%s", session["id"], error.code)
        raise unavailable() from None
    except Exception as error:
        log.error("voice stt failed session=%s error=%s", session["id"], type(error).__name__)
        raise unavailable() from None
    del wav
    text = clean_transcript(heard if isinstance(heard, str) else "")
    if not text:
        log.info("voice no speech session=%s", session["id"])
        raise no_speech()
    heard_at = time.monotonic()
    result = chat.send(url, settings, token, text, address, llm, voice=True)
    reply = " ".join(m["text"] for m in result["messages"] if m["role"] == "assistant")
    answered_at = time.monotonic()
    result["transcript"] = text
    result["audio"] = _synthesize(speech, reply, tts_language(reply, language), session["id"])
    result["audio_mime"] = AUDIO_MIME
    log.info(
        "voice turn session=%s language=%s audio=%s stt_ms=%d turn_ms=%d tts_ms=%d",
        session["id"],
        language or "unknown",
        "yes" if result["audio"] else "no",
        (heard_at - started) * 1000,
        (answered_at - heard_at) * 1000,
        (time.monotonic() - answered_at) * 1000,
    )
    return result


_greetings: dict[tuple, str] = {}
_greetings_lock = Lock()


def clear_greeting_cache() -> None:
    with _greetings_lock:
        _greetings.clear()


def greeting(url, settings, token, address, speech: SpeechPort | None) -> dict:
    """POST /v1/chat/sessions/{id}/voice/greeting: the call's short greeting
    (chat.voice_greeting) and its audio, synthesized once per adapter, clinic, text and
    language and then served from memory. Counted against CHAT_HISTORY_IP_PER_HOUR before the
    session lookup, like a transcript reload."""
    if speech is None:
        raise unavailable()
    _, hash_key = public.crypto(settings)
    with connect(url) as conn:
        public.limit(
            conn,
            hash_key,
            "chat-history-ip",
            settings.chat_history_ip_per_hour,
            public.HOUR,
            public.client_value(address),
        )
        session = chat.find_session(conn, token)
        clinic = chat._clinic(conn, session["clinic_id"])
    text = chat.voice_greeting(clinic)
    language = tts_language(text, None)
    key = (id(speech), clinic["id"], text, language)
    with _greetings_lock:
        audio = _greetings.get(key)
    if audio is None:
        audio = _synthesize(speech, text, language, session["id"])
        if audio is not None:
            with _greetings_lock:
                if len(_greetings) >= GREETING_CACHE_SIZE:
                    _greetings.clear()
                _greetings[key] = audio
    return {"text": text, "audio": audio, "audio_mime": AUDIO_MIME}
