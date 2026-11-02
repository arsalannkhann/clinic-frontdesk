"""Web voice receptionist (ADR 0012): speech-to-text, the chat turn, text-to-speech, the
greeting, the profile flag and the Sarvam adapter. Synthetic audio, a scripted model and a
scripted speech port only; no network."""

import base64
import io
import json
import logging
import wave
from datetime import UTC

import httpx
import pytest
from fastapi.testclient import TestClient
from test_chat import (
    CLINIC,
    HASH_KEY,
    KEYS,
    NOW,
    PROXY_SECRET,
    SLUG,
    assert_envelope,
    propose_script,
    session_row,
    start,
)

from clinic_voice import chat, routes_chat, service, tick, voice
from clinic_voice.api import app
from clinic_voice.config import get_settings
from clinic_voice.crypto import Keyring
from clinic_voice.db import connect
from clinic_voice.providers import speech as speech_module
from clinic_voice.providers.chat_llm import FakeChatLLM, LLMReply
from clinic_voice.providers.speech import FakeSpeech, SarvamSpeech, SpeechError

HEARD = "SYNTHETIC-VOICE-MARKER I need a consultation"
MP3 = b"ID3synthetic-mp3-bytes"


def wav_bytes(seconds=0.5) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"\x00\x00" * int(16000 * seconds))
    return buffer.getvalue()


WAV = base64.b64encode(wav_bytes()).decode()


@pytest.fixture
def env(database, monkeypatch):
    clock = {"now": NOW.astimezone(UTC)}
    monkeypatch.setattr(service, "utc_now", lambda: clock["now"])
    monkeypatch.setenv("DATABASE_URL", database)
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEYS", KEYS)
    monkeypatch.setenv("PHONE_HASH_KEY", HASH_KEY)
    get_settings.cache_clear()
    tick._ports_cache.clear()
    voice.clear_greeting_cache()
    fake = FakeChatLLM()
    speech = FakeSpeech(audio=MP3)
    holder = {"speech": speech}
    monkeypatch.setattr(routes_chat, "llm_for", lambda settings: fake)
    monkeypatch.setattr(routes_chat, "speech_for", lambda settings: holder["speech"])
    with connect(database) as conn:
        conn.execute(
            "UPDATE clinics SET public_slug=%s, public_enabled=true, public_chat_enabled=true,"
            " public_phone_display='+91 90000 01000', timezone='Asia/Kolkata' WHERE id=%s",
            (SLUG, CLINIC),
        )
    with TestClient(app) as client:
        yield {
            "client": client,
            "url": database,
            "fake": fake,
            "speech": speech,
            "holder": holder,
        }
    get_settings.cache_clear()
    tick._ports_cache.clear()
    voice.clear_greeting_cache()


def speak(env, session, audio=WAV, **kwargs):
    return env["client"].post(f"/v1/chat/sessions/{session}/voice", json={"audio": audio}, **kwargs)


def user_texts(url) -> list[str]:
    keyring = Keyring.from_setting(KEYS)
    with connect(url) as conn:
        rows = conn.execute("SELECT * FROM chat_messages WHERE role='user' ORDER BY seq").fetchall()
    return [
        json.loads(
            keyring.decrypt(
                r["content_key_id"],
                bytes(r["content_ciphertext"]),
                chat.message_aad(CLINIC, r["session_id"], r["seq"]),
            )
        )["text"]
        for r in rows
    ]


# --- Voice turns ----------------------------------------------------------------------------


def test_voice_turn_transcribes_answers_and_speaks(env, caplog):
    caplog.set_level(logging.INFO)
    session = start(env)
    env["speech"].transcripts = [(f"  {HEARD}\n", "en-IN")]
    env["fake"].script = [LLMReply("**Sure.** Which day suits you?")]
    r = speak(env, session)
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert set(body) == {
        "messages",
        "proposal",
        "quick_replies",
        "transcript",
        "audio",
        "audio_mime",
    }
    assert body["transcript"] == HEARD
    assert body["messages"] == [{"role": "assistant", "text": "**Sure.** Which day suits you?"}]
    assert base64.b64decode(body["audio"]) == MP3 and body["audio_mime"] == "audio/mpeg"
    assert body["quick_replies"] == chat.QUICK_REPLIES
    # Speech-to-text got the decoded WAV; synthesis got speakable text in Indian English.
    assert env["speech"].stt_calls == [len(wav_bytes())]
    assert env["speech"].tts_calls == [("Sure. Which day suits you?", "en-IN")]
    # The transcript is the turn's encrypted user message; one turn was used.
    assert user_texts(env["url"]) == [HEARD]
    assert session_row(env["url"])["turn_count"] == 1
    # The model got the spoken-reply rules; neither audio nor transcript reached the log.
    system = env["fake"].calls[0]["messages"][0]["content"]
    assert system.endswith(chat.VOICE_RULES) and "tap confirm" in system
    assert "SYNTHETIC-VOICE-MARKER" not in caplog.text and WAV[:40] not in caplog.text


def test_typed_turns_have_no_voice_rules(env):
    session = start(env)
    r = env["client"].post(f"/v1/chat/sessions/{session}/messages", json={"text": "hi"})
    assert r.status_code == 200
    system = env["fake"].calls[0]["messages"][0]["content"]
    assert "Voice:" not in system and "tap confirm" not in system


def test_voice_turn_proposes_and_never_books(env):
    session = start(env)
    env["speech"].transcripts = [("book a consultation", "en-IN")]
    env["fake"].script = propose_script()
    r = speak(env, session)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["proposal"] is not None and body["quick_replies"] == chat.PROPOSAL_QUICK_REPLIES
    with connect(env["url"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM appointments").fetchone()["n"] == 0


def test_hindi_reply_is_spoken_in_hindi(env):
    session = start(env)
    env["speech"].transcripts = [("मुझे अपॉइंटमेंट चाहिए", "hi-IN")]
    env["fake"].script = [LLMReply("ज़रूर, कौन सा दिन ठीक रहेगा?")]
    assert speak(env, session).status_code == 200
    assert env["speech"].tts_calls[0][1] == "hi-IN"


@pytest.mark.parametrize("heard", ["", "   ", "\n\t"])
def test_silence_is_no_speech_and_uses_no_turn(env, heard):
    session = start(env)
    env["speech"].transcripts = [(heard, None)]
    assert_envelope(speak(env, session), 422, "no_speech")
    assert speak(env, session).json()["error"]["message"] == (
        "We couldn't hear that. Please try again."
    )
    row = session_row(env["url"])
    assert row["turn_count"] == 0 and row["busy_until"] is None
    assert user_texts(env["url"]) == [] and env["fake"].calls == []


def test_voice_unavailable_without_speech_adapter(env):
    session = start(env)
    env["holder"]["speech"] = None
    r = speak(env, session)
    assert_envelope(r, 503, "voice_unavailable")
    assert_envelope(
        env["client"].post(f"/v1/chat/sessions/{session}/voice/greeting"), 503, "voice_unavailable"
    )
    assert session_row(env["url"])["turn_count"] == 0


@pytest.mark.parametrize(
    "failure", [SpeechError("timeout"), SpeechError("http_500"), RuntimeError("provider text")]
)
def test_stt_failure_is_503_and_uses_no_turn(env, failure, caplog):
    session = start(env)
    env["speech"].transcripts = [failure]
    assert_envelope(speak(env, session), 503, "voice_unavailable")
    assert session_row(env["url"])["turn_count"] == 0
    assert "provider text" not in caplog.text


def test_tts_failure_still_answers_with_text(env, caplog):
    session = start(env)
    env["speech"].transcripts = [("what are your hours", "en-IN")]
    env["speech"].tts_error = SpeechError("timeout")
    env["fake"].script = [LLMReply("We open at nine in the morning.")]
    r = speak(env, session)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["audio"] is None and body["audio_mime"] == "audio/mpeg"
    assert body["messages"][0]["text"] == "We open at nine in the morning."
    assert "voice tts failed" in caplog.text and "code=timeout" in caplog.text


def _riff(size=100, kind=b"WAVE"):
    return b"RIFF" + (size - 8).to_bytes(4, "little") + kind + b"\x00" * (size - 12)


@pytest.mark.parametrize(
    "audio",
    [
        "not base64!!",
        base64.b64encode(b"ID3 an mp3, not a wav" * 10).decode(),
        base64.b64encode(_riff(kind=b"AVI ")).decode(),
        base64.b64encode(b"RIFF").decode(),
        "",
    ],
)
def test_invalid_audio_is_422(env, audio):
    session = start(env)
    assert_envelope(speak(env, session, audio), 422, "invalid_request")
    assert env["speech"].stt_calls == [] and session_row(env["url"])["turn_count"] == 0


def test_oversized_audio_is_422(env, monkeypatch):
    session = start(env)
    monkeypatch.setenv("VOICE_MAX_AUDIO_BYTES", "16000")
    get_settings.cache_clear()
    assert_envelope(
        speak(env, session, base64.b64encode(_riff(16044)).decode()), 422, "invalid_request"
    )
    # Beyond the schema's character cap the body is refused before decoding.
    assert_envelope(speak(env, session, "A" * 1_400_004), 422, "invalid_request")
    assert env["speech"].stt_calls == []


def test_extra_fields_are_422(env):
    session = start(env)
    r = env["client"].post(f"/v1/chat/sessions/{session}/voice", json={"audio": WAV, "text": "hi"})
    assert_envelope(r, 422, "invalid_request")


@pytest.mark.parametrize("session", ["0" * 32, "not-a-session"])
def test_unknown_session_is_404_before_speech(env, session):
    start(env)
    assert_envelope(speak(env, session), 404, "not_found")
    assert_envelope(
        env["client"].post(f"/v1/chat/sessions/{session}/voice/greeting"), 404, "not_found"
    )
    assert env["speech"].stt_calls == [] and env["speech"].tts_calls == []


def test_chat_disabled_session_is_404(env):
    session = start(env)
    with connect(env["url"]) as conn:
        conn.execute("UPDATE clinics SET public_chat_enabled=false WHERE id=%s", (CLINIC,))
    assert_envelope(speak(env, session), 404, "not_found")


def test_busy_session_is_409_before_speech(env):
    session = start(env)
    with connect(env["url"]) as conn:
        conn.execute(
            "UPDATE chat_sessions SET busy_until=%s", (service.utc_now().replace(year=2040),)
        )
    assert_envelope(speak(env, session), 409, "turn_in_progress")
    assert env["speech"].stt_calls == []


def test_conversation_limit_passes_through(env, monkeypatch):
    monkeypatch.setenv("CHAT_MAX_TURNS", "1")
    get_settings.cache_clear()
    session = start(env)
    env["speech"].default_transcript = ("hello", "en-IN")
    assert speak(env, session).status_code == 200
    assert_envelope(speak(env, session), 409, "conversation_limit")
    assert len(env["speech"].stt_calls) == 1


def test_voice_rate_limit(env, monkeypatch):
    monkeypatch.setenv("CHAT_MESSAGE_IP_PER_HOUR", "1")
    get_settings.cache_clear()
    session = start(env)
    env["speech"].transcripts = [("", None)]
    assert_envelope(speak(env, session), 422, "no_speech")
    assert_envelope(speak(env, session), 429, "rate_limited")


def test_voice_paths_need_the_proxy_secret_when_set(env, monkeypatch):
    session = start(env)
    monkeypatch.setenv("PUBLIC_PROXY_SECRET", PROXY_SECRET)
    get_settings.cache_clear()
    assert_envelope(speak(env, session), 403, "forbidden")
    assert_envelope(
        env["client"].post(f"/v1/chat/sessions/{session}/voice/greeting"), 403, "forbidden"
    )
    env["speech"].transcripts = [("hello", "en-IN")]
    ok = speak(env, session, headers={"X-Proxy-Secret": PROXY_SECRET, "X-Client-IP": "203.0.113.5"})
    assert ok.status_code == 200, ok.text


# --- Greeting -------------------------------------------------------------------------------


def test_greeting_is_spoken_once_and_cached(env):
    first = env["client"].post(f"/v1/chat/clinics/{SLUG}/sessions").json()
    second = env["client"].post(f"/v1/chat/clinics/{SLUG}/sessions").json()
    a = env["client"].post(f"/v1/chat/sessions/{first['session_id']}/voice/greeting")
    assert a.status_code == 200, a.text
    assert a.headers["cache-control"] == "no-store"
    assert a.json() == {
        "text": first["greeting"],
        "audio": base64.b64encode(MP3).decode(),
        "audio_mime": "audio/mpeg",
    }
    b = env["client"].post(f"/v1/chat/sessions/{second['session_id']}/voice/greeting")
    assert b.json() == a.json()
    assert env["speech"].tts_calls == [(first["greeting"], "en-IN")]
    assert session_row(env["url"])["turn_count"] == 0


def test_greeting_tts_failure_is_not_cached(env):
    session = start(env)
    env["speech"].tts_error = SpeechError("http_503")
    r = env["client"].post(f"/v1/chat/sessions/{session}/voice/greeting")
    assert r.status_code == 200 and r.json()["audio"] is None
    env["speech"].tts_error = None
    r = env["client"].post(f"/v1/chat/sessions/{session}/voice/greeting")
    assert r.json()["audio"] == base64.b64encode(MP3).decode()
    assert len(env["speech"].tts_calls) == 2


# --- Public profile -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("speech_key", "chat_key", "chat_on", "expected"),
    [
        ("synthetic-speech-key", "synthetic-chat-key", True, True),
        (None, "synthetic-chat-key", True, False),
        ("synthetic-speech-key", None, True, False),
        ("synthetic-speech-key", "synthetic-chat-key", False, False),
    ],
)
def test_profile_voice_enabled(env, monkeypatch, speech_key, chat_key, chat_on, expected):
    for name, value in (("SPEECH_API_KEY", speech_key), ("CHAT_LLM_API_KEY", chat_key)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)
    get_settings.cache_clear()
    with connect(env["url"]) as conn:
        conn.execute("UPDATE clinics SET public_chat_enabled=%s WHERE id=%s", (chat_on, CLINIC))
    body = env["client"].get(f"/v1/public/clinics/{SLUG}").json()
    assert body["chat_enabled"] is chat_on
    assert body["voice_enabled"] is expected
    assert list(body)[-2:] == ["chat_enabled", "voice_enabled"]


def test_sarvam_api_key_is_a_fallback_name(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://synthetic/none_test")
    monkeypatch.delenv("SPEECH_API_KEY", raising=False)
    monkeypatch.setenv("SARVAM_API_KEY", "synthetic-sarvam")
    get_settings.cache_clear()
    try:
        assert get_settings().speech_configured()
        monkeypatch.setenv("SPEECH_API_KEY", "synthetic-speech")
        get_settings.cache_clear()
        assert get_settings().speech_api_key.get_secret_value() == "synthetic-speech"
    finally:
        get_settings.cache_clear()


# --- Pure helpers ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "heard", "expected"),
    [
        ("Sure, which day?", "en-IN", "en-IN"),
        ("Sure, which day?", None, "en-IN"),
        ("Haan ji, kaunsa din?", "hi-IN", "hi-IN"),
        ("Sure, which day?", "ta-IN", "en-IN"),
        ("ज़रूर", None, "hi-IN"),
        ("नक्की", "mr-IN", "mr-IN"),
        ("சரி", "ta-IN", "ta-IN"),
        ("ঠিক আছে", None, "bn-IN"),
    ],
)
def test_tts_language(reply, heard, expected):
    assert voice.tts_language(reply, heard) == expected


def test_speakable_strips_markdown_and_caps_length():
    assert voice.speakable("## Hours\n- **Mon**: 9 to 5\n- see [site](https://x.example)") == (
        "Hours Mon: 9 to 5 see site"
    )
    long = "This is a sentence. " * 100
    spoken = voice.speakable(long)
    assert len(spoken) <= voice.MAX_TTS_CHARS and spoken.endswith(".")


# --- Sarvam adapter -------------------------------------------------------------------------


def _adapter(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return SarvamSpeech(client, "https://speech.synthetic.example", "synthetic-key")


def test_sarvam_transcribe_request_and_parse():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["key"] = request.headers["api-subscription-key"]
        seen["body"] = request.content
        return httpx.Response(
            200,
            json={"request_id": "r", "transcript": "hello", "language_code": "en-IN"},
        )

    assert _adapter(handler).transcribe(wav_bytes(0.1)) == ("hello", "en-IN")
    assert seen["url"] == "https://speech.synthetic.example/speech-to-text"
    assert seen["key"] == "synthetic-key"
    assert b'name="model"' in seen["body"] and b"saarika:v2.5" in seen["body"]
    assert b'name="language_code"' in seen["body"] and b"unknown" in seen["body"]
    assert b'name="file"; filename="utterance.wav"' in seen["body"]


def test_sarvam_synthesize_request_and_parse():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["json"] = json.loads(request.content)
        return httpx.Response(
            200, json={"request_id": "r", "audios": [base64.b64encode(MP3).decode()]}
        )

    assert _adapter(handler).synthesize("Hello", "hi-IN") == MP3
    assert seen["url"] == "https://speech.synthetic.example/text-to-speech"
    assert seen["json"] == {
        "text": "Hello",
        "target_language_code": "hi-IN",
        "model": "bulbul:v3",
        "speaker": "priya",
        "speech_sample_rate": 16000,
        "output_audio_codec": "mp3",
    }


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(401, text="secret provider text"), "unauthorized"),
        (httpx.Response(403), "unauthorized"),
        (httpx.Response(429), "http_429"),
        (httpx.Response(500, text="secret provider text"), "http_500"),
        (httpx.Response(200, text="not json"), "malformed"),
        (httpx.Response(200, json=["list"]), "malformed"),
        (httpx.Response(200, json={"audios": []}), "malformed"),
        (httpx.Response(200, json={"audios": ["!!not base64"]}), "malformed"),
        (httpx.Response(302, headers={"location": "https://elsewhere.example"}), "http_302"),
    ],
)
def test_sarvam_errors_are_codes(response, code):
    adapter = _adapter(lambda request: response)
    with pytest.raises(SpeechError) as error:
        adapter.synthesize("Hello", "en-IN")
    assert error.value.code == code and str(error.value) == code


def test_sarvam_transport_errors_and_repr():
    def timeout(request):
        raise httpx.ReadTimeout("synthetic")

    def broken(request):
        raise httpx.ConnectError("synthetic")

    with pytest.raises(SpeechError) as error:
        _adapter(timeout).transcribe(b"RIFF")
    assert error.value.code == "timeout"
    with pytest.raises(SpeechError) as error:
        _adapter(broken).transcribe(b"RIFF")
    assert error.value.code == "transport"
    with pytest.raises(SpeechError) as error:
        _adapter(lambda r: httpx.Response(200, json={"transcript": 5})).transcribe(b"RIFF")
    assert error.value.code == "malformed"
    assert "synthetic-key" not in repr(_adapter(timeout))


def test_build_needs_a_key(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://synthetic/none_test")
    monkeypatch.delenv("SPEECH_API_KEY", raising=False)
    monkeypatch.delenv("SARVAM_API_KEY", raising=False)
    get_settings.cache_clear()
    try:
        assert speech_module.build(get_settings()) is None
        monkeypatch.setenv("SPEECH_API_KEY", "synthetic-key")
        get_settings.cache_clear()
        assert isinstance(speech_module.build(get_settings()), SarvamSpeech)
        monkeypatch.setenv("SPEECH_BASE_URL", "http://speech.example")
        get_settings.cache_clear()
        with pytest.raises(ValueError):
            get_settings()
    finally:
        get_settings.cache_clear()
