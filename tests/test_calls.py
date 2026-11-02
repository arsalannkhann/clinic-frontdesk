"""Real-time call tokens (ADR 0014): POST /v1/chat/sessions/{id}/call and the profile flag."""

import base64
import hashlib
import hmac
import json

import pytest
from test_chat import SLUG, assert_envelope, start
from test_voice import env  # noqa: F401  (fixture)

from clinic_voice import calls
from clinic_voice.config import get_settings

API_KEY = "APIsyntheticKey"
API_SECRET = "synthetic-livekit-secret-0123456789abcdef"


@pytest.fixture
def live(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LIVEKIT_URL", "wss://synthetic.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", API_KEY)
    monkeypatch.setenv("LIVEKIT_API_SECRET", API_SECRET)
    monkeypatch.setenv("CHAT_LLM_API_KEY", "synthetic-chat-key")
    get_settings.cache_clear()
    yield env
    get_settings.cache_clear()


def decode(token: str, secret: str = API_SECRET) -> dict:
    header, body, signature = token.split(".")
    expected = hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    assert base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)) == expected
    assert json.loads(base64.urlsafe_b64decode(header + "==")) == {"alg": "HS256", "typ": "JWT"}
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


def test_call_token(live):
    session = start(live)
    r = live["client"].post(f"/v1/chat/sessions/{session}/call")
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert set(body) == {"server_url", "participant_token", "room"}
    assert body["server_url"] == "wss://synthetic.livekit.cloud"
    claims = decode(body["participant_token"])
    assert claims["iss"] == API_KEY
    assert claims["exp"] - claims["nbf"] == calls.TOKEN_TTL_SECONDS
    assert claims["sub"].startswith("caller-")
    video = claims["video"]
    assert video["room"] == body["room"] and body["room"].startswith("call-")
    assert video["roomJoin"] is True
    assert video["canPublishSources"] == ["microphone"] and video["canPublishData"] is False
    (dispatch,) = claims["roomConfig"]["agents"]
    assert dispatch["agentName"] == "clinic-receptionist"
    metadata = json.loads(dispatch["metadata"])
    assert metadata["session"] == session
    # Each call is a new room.
    again = live["client"].post(f"/v1/chat/sessions/{session}/call").json()
    assert again["room"] != body["room"]


def test_unknown_session_is_404(live):
    r = live["client"].post("/v1/chat/sessions/" + "0" * 32 + "/call")
    assert r.status_code == 404
    assert_envelope(r, 404, "not_found")


def test_unconfigured_is_503(env):  # noqa: F811
    session = start(env)
    r = env["client"].post(f"/v1/chat/sessions/{session}/call")
    assert r.status_code == 503
    assert_envelope(r, 503, "call_unavailable")


def test_rate_limited(live, monkeypatch):
    monkeypatch.setenv("CALL_IP_PER_HOUR", "2")
    get_settings.cache_clear()
    session = start(live)
    for _ in range(2):
        assert live["client"].post(f"/v1/chat/sessions/{session}/call").status_code == 200
    r = live["client"].post(f"/v1/chat/sessions/{session}/call")
    assert r.status_code == 429


def test_profile_offers_realtime_call(live):
    body = live["client"].get(f"/v1/public/clinics/{SLUG}").json()
    assert body["realtime_call"] is True


@pytest.mark.parametrize("url", ["https://x.livekit.cloud", "ws://example.com", "wss://u:p@x.io"])
def test_livekit_url_must_be_wss(monkeypatch, url):
    from clinic_voice.config import Settings

    with pytest.raises(ValueError):
        Settings(_env_file=None, database_url="postgresql://x@localhost/x_test", livekit_url=url)
