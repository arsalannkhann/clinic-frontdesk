"""Chat assistant (P2, ADR 0011): sessions, the tool loop, proposals and confirm, fallbacks,
the OpenAI-compatible adapter. Synthetic data and a scripted model only; no network."""

import json
import logging
from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from clinic_voice import chat, routes_chat, service, tick
from clinic_voice.api import app
from clinic_voice.cli import DEMO_CLINIC_ID as CLINIC
from clinic_voice.cli import DOCTOR_ID, SERVICE_ID
from clinic_voice.config import get_settings
from clinic_voice.crypto import Keyring, generate_key
from clinic_voice.db import connect
from clinic_voice.providers.chat_llm import (
    ChatLLMError,
    FakeChatLLM,
    LLMReply,
    OpenAIChatLLM,
    ToolCall,
    parse_reply,
)

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2031, 3, 3, 8, 30, tzinfo=IST)
DAY = NOW.date() + timedelta(days=2)
SLOT = datetime.combine(DAY, time(10, 0), IST)
KEYS = "k1:" + generate_key()
HASH_KEY = generate_key()
SLUG = "demo-clinic"
NAME = "Synthetic Chatpatient"
PHONE = "+919000000611"
SECRET_TEXT = "SYNTHETIC-SYMPTOM-MARKER my molar hurts"
PROXY_SECRET = "synthetic-proxy-secret-" + "2" * 32


@pytest.fixture
def env(database, monkeypatch):
    clock = {"now": NOW.astimezone(UTC)}
    monkeypatch.setattr(service, "utc_now", lambda: clock["now"])
    monkeypatch.setenv("DATABASE_URL", database)
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEYS", KEYS)
    monkeypatch.setenv("PHONE_HASH_KEY", HASH_KEY)
    get_settings.cache_clear()
    tick._ports_cache.clear()
    fake = FakeChatLLM()
    monkeypatch.setattr(routes_chat, "llm_for", lambda settings: fake)
    with connect(database) as conn:
        conn.execute(
            "UPDATE clinics SET public_slug=%s, public_enabled=true, public_chat_enabled=true,"
            " public_phone_display='+91 90000 01000', timezone='Asia/Kolkata' WHERE id=%s",
            (SLUG, CLINIC),
        )
        conn.execute(
            "UPDATE doctors SET booking_calendar_id='ledger:' || id::text WHERE id=%s",
            (DOCTOR_ID,),
        )
    with TestClient(app) as client:
        yield {"client": client, "url": database, "fake": fake, "clock": clock}
    get_settings.cache_clear()
    tick._ports_cache.clear()


def start(env) -> str:
    r = env["client"].post(f"/v1/chat/clinics/{SLUG}/sessions")
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"session_id", "greeting", "quick_replies"}
    assert len(body["session_id"]) == 32 and "virtual assistant" in body["greeting"]
    return body["session_id"]


def say(env, session, text):
    return env["client"].post(f"/v1/chat/sessions/{session}/messages", json={"text": text})


def call(tool, **arguments):
    return ToolCall(f"call_{uuid4().hex[:8]}", tool, arguments)


def propose_script(starts_at=SLOT):
    return [
        LLMReply(
            None,
            [call("check_slots", service_id=str(SERVICE_ID), date=starts_at.date().isoformat())],
        ),
        LLMReply(
            None,
            [
                call(
                    "propose_booking",
                    service_id=str(SERVICE_ID),
                    doctor_id=str(DOCTOR_ID),
                    starts_at=starts_at.isoformat(),
                )
            ],
        ),
        LLMReply("Please confirm the time in the card below."),
    ]


def confirm(env, session, proposal, headers=None, **changes):
    body = {"name": NAME, "phone": PHONE, "consent_contact": True, **changes}
    return env["client"].post(
        f"/v1/chat/sessions/{session}/proposals/{proposal}/confirm", json=body, headers=headers
    )


def assert_envelope(response, status, code):
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code


def session_row(url):
    with connect(url) as conn:
        return conn.execute("SELECT * FROM chat_sessions").fetchone()


# --- Happy path ------------------------------------------------------------------------------


def test_chat_books_through_a_confirmed_proposal(env):
    session = start(env)
    env["fake"].script = propose_script()
    r = say(env, session, "I would like a consultation on " + DAY.isoformat())
    assert r.status_code == 200, r.text
    turn = r.json()
    assert turn["messages"] == [
        {"role": "assistant", "text": "Please confirm the time in the card below."}
    ]
    proposal = turn["proposal"]
    assert datetime.fromisoformat(proposal["starts_at"]) == SLOT
    assert proposal["doctor_name"] == "Demo Doctor" and proposal["service_name"] == "Consultation"
    with connect(env["url"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM appointments").fetchone()["n"] == 0
    # The model saw the clinic, the zone and the tool results; nothing was booked by it.
    first = env["fake"].calls[0]
    system = first["messages"][0]["content"]
    assert "Synthetic Demo Clinic" in system and "Asia/Kolkata" in system
    assert NOW.strftime("%d %B %Y") in system and "AI assistant" in system
    assert set(first["tools"]) == chat.TOOL_NAMES
    tool_result = json.loads(env["fake"].calls[1]["messages"][-1]["content"])
    assert any(datetime.fromisoformat(s["starts_at"]) == SLOT for s in tool_result["slots"])

    r = confirm(env, session, proposal["id"])
    assert r.status_code == 202, r.text
    booked = r.json()
    assert set(booked) == {"reference", "status", "starts_at", "doctor_name", "service_name"}
    assert booked["status"] == "confirmed"
    with connect(env["url"]) as conn:
        appointment = conn.execute("SELECT * FROM appointments").fetchone()
        consents = conn.execute("SELECT DISTINCT source FROM consents").fetchall()
        used = conn.execute("SELECT * FROM chat_proposals").fetchone()
        roles = [r["role"] for r in conn.execute("SELECT role FROM chat_messages ORDER BY seq")]
        audit = conn.execute("SELECT action FROM audit_events").fetchall()
    assert (
        appointment["channel"] == "chat" and appointment["booking_reference"] == booked["reference"]
    )
    assert [c["source"] for c in consents] == ["chat"]
    assert used["consumed_at"] is not None and used["appointment_id"] == appointment["id"]
    assert session_row(env["url"])["patient_id"] == appointment["patient_id"]
    assert roles == [
        "assistant",
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
        "system",
    ]
    assert {"action": "chat.booking_created"} in audit

    # The reloaded transcript shows only user and assistant text, and no open proposal.
    history = env["client"].get(f"/v1/chat/sessions/{session}").json()
    assert [m["role"] for m in history["messages"]] == ["assistant", "user", "assistant"]
    assert "proposal" not in history or history["proposal"] is None


def test_messages_are_stored_encrypted(env):
    session = start(env)
    say(env, session, SECRET_TEXT)
    keyring = Keyring.from_setting(KEYS)
    with connect(env["url"]) as conn:
        rows = conn.execute("SELECT * FROM chat_messages ORDER BY seq").fetchall()
        stored = conn.execute("SELECT token_hash FROM chat_sessions").fetchone()["token_hash"]
    assert all(SECRET_TEXT.encode() not in bytes(r["content_ciphertext"]) for r in rows)
    user = rows[1]
    aad = chat.message_aad(CLINIC, user["session_id"], user["seq"])
    plain = json.loads(
        keyring.decrypt(user["content_key_id"], bytes(user["content_ciphertext"]), aad)
    )
    assert plain == {"text": SECRET_TEXT}
    assert bytes(stored) == chat.token_hash(session) and session.encode() not in bytes(stored)


def test_history_restores_the_open_proposal(env):
    session = start(env)
    env["fake"].script = propose_script()
    proposal = say(env, session, "book please").json()["proposal"]
    history = env["client"].get(f"/v1/chat/sessions/{session}")
    assert history.status_code == 200
    body = history.json()
    assert body["proposal"]["id"] == proposal["id"]
    assert [m["role"] for m in body["messages"]] == ["assistant", "user", "assistant"]
    assert all(set(m) == {"role", "text"} for m in body["messages"])


# --- Proposals -------------------------------------------------------------------------------


def test_a_proposal_books_once(env):
    session = start(env)
    env["fake"].script = propose_script()
    proposal = say(env, session, "book").json()["proposal"]["id"]
    first = confirm(env, session, proposal)
    second = confirm(env, session, proposal)  # A double click replays the same booking.
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert_envelope(
        confirm(env, session, proposal, name="Someone Else"), 409, "idempotency_conflict"
    )
    # With another Idempotency-Key the consumed proposal is refused.
    assert_envelope(
        confirm(env, session, proposal, headers={"Idempotency-Key": "other-key-123"}),
        409,
        "proposal_used",
    )
    with connect(env["url"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM appointments").fetchone()["n"] == 1
        notes = conn.execute("SELECT count(*) AS n FROM chat_messages WHERE role='system'")
        assert notes.fetchone()["n"] == 1


def test_idempotency_key_replays_the_confirm(env):
    session = start(env)
    env["fake"].script = propose_script()
    proposal = say(env, session, "book").json()["proposal"]["id"]
    headers = {"Idempotency-Key": "card-submit-0001"}
    first = confirm(env, session, proposal, headers=headers)
    again = confirm(env, session, proposal, headers=headers)
    assert first.status_code == again.status_code == 202 and first.json() == again.json()


def test_an_expired_proposal_cannot_be_confirmed(env):
    session = start(env)
    env["fake"].script = propose_script()
    proposal = say(env, session, "book").json()["proposal"]["id"]
    env["clock"]["now"] += chat.PROPOSAL_TTL + timedelta(seconds=1)
    assert_envelope(confirm(env, session, proposal), 410, "proposal_expired")
    history = env["client"].get(f"/v1/chat/sessions/{session}").json()
    assert history.get("proposal") is None
    with connect(env["url"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM appointments").fetchone()["n"] == 0


def test_confirm_requires_consent(env):
    session = start(env)
    env["fake"].script = propose_script()
    proposal = say(env, session, "book").json()["proposal"]["id"]
    assert_envelope(confirm(env, session, proposal, consent_contact=False), 422, "consent_required")


def test_propose_refuses_a_time_that_is_not_offered(env):
    session = start(env)
    odd = SLOT + timedelta(minutes=7)
    env["fake"].script = propose_script(odd)
    turn = say(env, session, "book").json()
    assert turn.get("proposal") is None
    result = json.loads(env["fake"].calls[2]["messages"][-1]["content"])
    assert result == {"error": "slot_not_offered"}
    with connect(env["url"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM chat_proposals").fetchone()["n"] == 0


def test_a_taken_slot_is_refused_at_confirm(env):
    session = start(env)
    env["fake"].script = propose_script()
    proposal = say(env, session, "book").json()["proposal"]["id"]
    from clinic_voice.schemas import ReservationRequest

    service.reserve(
        env["url"],
        CLINIC,
        ReservationRequest(doctor_id=DOCTOR_ID, service_id=SERVICE_ID, starts_at=SLOT),
        "test:taken",
    )
    assert_envelope(confirm(env, session, proposal), 409, "slot_unavailable")


# --- Session secrets -------------------------------------------------------------------------


def test_forged_or_tampered_session_ids_are_404(env):
    session = start(env)
    env["fake"].script = propose_script()
    proposal = say(env, session, "book").json()["proposal"]["id"]
    other = start(env)
    tampered = session[:-1] + ("0" if session[-1] != "0" else "1")
    for bad in (tampered, "0" * 32, session.upper(), session + "0", "not-a-session"):
        assert_envelope(say(env, bad, "hi"), 404, "not_found")
        assert_envelope(env["client"].get(f"/v1/chat/sessions/{bad}"), 404, "not_found")
        assert_envelope(confirm(env, bad, proposal), 404, "not_found")
    # Another session cannot confirm this session's proposal.
    assert_envelope(confirm(env, other, proposal), 404, "not_found")
    assert_envelope(confirm(env, session, uuid4()), 404, "not_found")


def test_chat_is_404_when_the_clinic_turns_it_off(env):
    session = start(env)
    with connect(env["url"]) as conn:
        conn.execute("UPDATE clinics SET public_chat_enabled=false WHERE id=%s", (CLINIC,))
    assert_envelope(env["client"].post(f"/v1/chat/clinics/{SLUG}/sessions"), 404, "not_found")
    assert_envelope(say(env, session, "hello"), 404, "not_found")


def test_message_validation(env):
    session = start(env)
    assert_envelope(say(env, session, "x" * 1001), 422, "invalid_request")
    assert_envelope(say(env, session, "   "), 422, "invalid_request")
    r = env["client"].post(
        f"/v1/chat/sessions/{session}/messages", json={"text": "hi", "history": []}
    )
    assert_envelope(r, 422, "invalid_request")


def test_turn_limit(env, monkeypatch):
    monkeypatch.setenv("CHAT_MAX_TURNS", "2")
    get_settings.cache_clear()
    session = start(env)
    assert say(env, session, "one").status_code == 200
    assert say(env, session, "two").status_code == 200
    assert_envelope(say(env, session, "three"), 409, "conversation_limit")


def test_session_creation_is_rate_limited_per_ip(env, monkeypatch):
    monkeypatch.setenv("CHAT_SESSION_IP_PER_HOUR", "2")
    get_settings.cache_clear()
    start(env)
    start(env)
    assert_envelope(env["client"].post(f"/v1/chat/clinics/{SLUG}/sessions"), 429, "rate_limited")


def test_chat_paths_need_the_proxy_secret_when_set(env, monkeypatch):
    session = start(env)
    monkeypatch.setenv("PUBLIC_PROXY_SECRET", PROXY_SECRET)
    get_settings.cache_clear()
    client = env["client"]
    assert_envelope(client.post(f"/v1/chat/clinics/{SLUG}/sessions"), 403, "forbidden")
    assert_envelope(say(env, session, "hi"), 403, "forbidden")
    assert_envelope(client.get(f"/v1/chat/sessions/{session}"), 403, "forbidden")
    ok = client.post(
        f"/v1/chat/sessions/{session}/messages",
        json={"text": "hi"},
        headers={"X-Proxy-Secret": PROXY_SECRET, "X-Client-IP": "203.0.113.5"},
    )
    assert ok.status_code == 200


# --- Model failures, tool cap, emergencies ---------------------------------------------------


@pytest.mark.parametrize(
    "failure", [ChatLLMError("timeout"), ChatLLMError("http_500"), RuntimeError()]
)
def test_model_failure_falls_back_to_quick_replies(env, failure, caplog):
    caplog.set_level(logging.INFO)
    session = start(env)
    env["fake"].script = [failure]
    r = say(env, session, SECRET_TEXT)
    assert r.status_code == 200
    body = r.json()
    assert body["quick_replies"] == chat.FALLBACK_QUICK_REPLIES
    assert "+91 90000 01000" in body["messages"][0]["text"]
    assert SECRET_TEXT not in caplog.text


def test_unconfigured_model_falls_back(env, monkeypatch):
    monkeypatch.setattr(routes_chat, "llm_for", lambda settings: None)
    session = start(env)
    body = say(env, session, "hello").json()
    assert body["quick_replies"] == chat.FALLBACK_QUICK_REPLIES


def test_real_llm_for_without_a_key_is_none(env):
    from clinic_voice.providers import chat_llm

    assert chat_llm.build(get_settings()) is None


def test_tool_rounds_are_capped(env):
    session = start(env)
    env["fake"].script = [LLMReply(None, [call("clinic_info")]) for _ in range(10)]
    r = say(env, session, "loop")
    assert r.status_code == 200
    calls = env["fake"].calls
    assert len(calls) == chat.MAX_TOOL_ROUNDS + 1
    assert all(c["tools"] for c in calls[:-1]) and calls[-1]["tools"] == []
    with connect(env["url"]) as conn:
        tools = conn.execute("SELECT count(*) AS n FROM chat_messages WHERE role='tool'")
        assert tools.fetchone()["n"] == chat.MAX_TOOL_ROUNDS
    assert r.json()["quick_replies"] == chat.FALLBACK_QUICK_REPLIES


def test_malformed_tool_arguments_are_refused_to_the_model(env):
    session = start(env)
    env["fake"].script = [
        LLMReply(None, [ToolCall("c1", "check_slots", None), ToolCall("c2", "drop_tables", {})]),
        LLMReply("Which day suits you?"),
    ]
    assert say(env, session, "book").json()["messages"][0]["text"] == "Which day suits you?"
    results = [json.loads(m["content"]) for m in env["fake"].calls[1]["messages"][-2:]]
    assert results == [{"error": "invalid_arguments"}, {"error": "unknown_tool"}]


@pytest.mark.parametrize(
    "text",
    [
        "My gum bleeding won't stop after extraction",
        "I can't breathe properly and my face is swollen",
        "Swelling is spreading to my eye",
        "khoon band nahi ho raha",
        "This is an emergency",
    ],
)
def test_emergency_messages_get_the_script(env, text):
    session = start(env)
    body = say(env, session, text).json()
    assert body["messages"][0]["text"] == chat.DEFAULT_EMERGENCY
    assert body["quick_replies"] == chat.EMERGENCY_QUICK_REPLIES
    assert env["fake"].calls == []  # No model call for an emergency.


def test_emergency_uses_the_clinic_approved_script(env):
    approved = "Synthetic approved emergency text: call 112 now."
    with connect(env["url"]) as conn:
        conn.execute(
            "UPDATE clinics SET emergency_text=%s, handoff_text='Synthetic handoff',"
            " scripts_approved=true WHERE id=%s",
            (approved, CLINIC),
        )
    session = start(env)
    body = say(env, session, "heavy bleeding from my tooth").json()
    assert body["messages"][0]["text"] == approved


def test_a_booking_request_for_an_emergency_visit_is_not_an_emergency(env):
    session = start(env)
    env["fake"].script = [LLMReply("Which day would you like?")]
    body = say(env, session, "Can I book an emergency pain visit tomorrow?").json()
    assert body["messages"][0]["text"] == "Which day would you like?"


# --- Tools -----------------------------------------------------------------------------------


def test_request_callback_tool_stores_a_chat_callback(env):
    session = start(env)
    env["fake"].script = [
        LLMReply(None, [call("request_callback", name=NAME, phone=PHONE, reason=SECRET_TEXT)]),
        LLMReply("Done, the clinic will call you back."),
    ]
    say(env, session, "please call me")
    with connect(env["url"]) as conn:
        row = conn.execute("SELECT source, status FROM callback_requests").fetchone()
    assert (row["source"], row["status"]) == ("chat", "open")


def test_booking_status_tool_needs_reference_and_phone(env):
    session = start(env)
    env["fake"].script = propose_script()
    proposal = say(env, session, "book").json()["proposal"]["id"]
    reference = confirm(env, session, proposal).json()["reference"]
    env["fake"].script = [
        LLMReply(None, [call("booking_status", reference=reference, phone=PHONE)]),
        LLMReply(None, [call("booking_status", reference=reference, phone="+919000000999")]),
        LLMReply("ok"),
    ]
    say(env, session, "status?")
    found = json.loads(env["fake"].calls[-2]["messages"][-1]["content"])
    missing = json.loads(env["fake"].calls[-1]["messages"][-1]["content"])
    assert found["found"] is True and found["reference"] == reference
    assert missing == {"found": False}


def test_no_patient_data_in_logs(env, caplog):
    caplog.set_level(logging.DEBUG)
    session = start(env)
    env["fake"].script = [
        LLMReply(None, [call("request_callback", name=NAME, phone=PHONE, reason=SECRET_TEXT)]),
        *propose_script(),
    ]
    say(env, session, SECRET_TEXT + " " + PHONE)
    say(env, session, "book it")
    proposal = env["client"].get(f"/v1/chat/sessions/{session}").json()["proposal"]["id"]
    reference = confirm(env, session, proposal).json()["reference"]
    # Application logs only: the test client's own request log is not the server's (the
    # server's access log redacts the session id, test_access_log_redacts_session_ids).
    text = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("clinic_voice"))
    assert "chat booking appointment=" in text
    for secret in (NAME, PHONE, PHONE[1:], SECRET_TEXT, session, reference):
        assert secret not in text


# --- The OpenAI-compatible adapter -----------------------------------------------------------


def _adapter(handler, auth_header="api-subscription-key"):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenAIChatLLM(client, "https://llm.example/v1", "sarvam-105b", "test-key", auth_header)


def test_adapter_request_and_tool_call_parsing():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "<think>internal</think>",
                            "reasoning_content": "SECRET REASONING",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "check_slots",
                                        "arguments": '{"service_id": "x", "date": "2031-03-05"}',
                                    },
                                },
                                {
                                    "id": "call_2",
                                    "function": {"name": "clinic_info", "arguments": "{bad"},
                                },
                            ],
                        }
                    }
                ],
                "usage": {"completion_tokens_details": {"reasoning_tokens": 120}},
            },
        )

    reply = _adapter(handler).complete([{"role": "user", "content": "hi"}], chat.TOOLS)
    assert seen["url"] == "https://llm.example/v1/chat/completions"
    assert seen["headers"]["api-subscription-key"] == "test-key"
    assert "authorization" not in seen["headers"]
    assert seen["body"]["model"] == "sarvam-105b" and seen["body"]["max_tokens"] == 1500
    assert seen["body"]["tools"] == chat.TOOLS
    assert reply.text is None  # The think block and reasoning_content never surface.
    assert reply.tool_calls == [
        ToolCall("call_1", "check_slots", {"service_id": "x", "date": "2031-03-05"}),
        ToolCall("call_2", "clinic_info", None),
    ]


def test_adapter_bearer_auth_and_errors():
    def ok(request):
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"choices": [{"message": {"content": "Hello"}}]})

    assert _adapter(ok, "authorization").complete([], []).text == "Hello"
    for status, code in ((401, "unauthorized"), (429, "http_429"), (500, "http_500")):
        llm = _adapter(lambda request, s=status: httpx.Response(s, text="provider says no"))
        with pytest.raises(ChatLLMError) as raised:
            llm.complete([], [])
        assert raised.value.code == code and "provider" not in str(raised.value)

    def timeout(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(ChatLLMError) as raised:
        _adapter(timeout).complete([], [])
    assert raised.value.code == "timeout"
    with pytest.raises(ChatLLMError):
        parse_reply({"choices": []})
    assert "test-key" not in repr(_adapter(ok))


def test_chat_settings_validation(monkeypatch):
    from clinic_voice.config import Settings

    monkeypatch.setenv("DATABASE_URL", "postgresql://u@localhost/x_test")
    settings = Settings()
    assert settings.chat_llm_base_url == "https://api.sarvam.ai/v1"
    assert settings.chat_llm_model == "sarvam-105b"
    assert settings.chat_llm_auth_header == "api-subscription-key"
    assert settings.chat_llm_timeout_seconds == 25.0
    monkeypatch.setenv("CHAT_LLM_AUTH_HEADER", "x-api-key")
    with pytest.raises(ValueError):
        Settings()
    monkeypatch.setenv("CHAT_LLM_AUTH_HEADER", "Authorization")
    monkeypatch.setenv("CHAT_LLM_BASE_URL", "http://llm.example/v1")
    with pytest.raises(ValueError):
        Settings()


def test_retention_deletes_old_chats_and_keeps_bookings(env):
    from clinic_voice import retention

    session = start(env)
    env["fake"].script = propose_script()
    proposal = say(env, session, "book").json()["proposal"]["id"]
    confirm(env, session, proposal)
    fresh = start(env)
    with connect(env["url"]) as conn:
        conn.execute(
            "UPDATE chat_sessions SET last_active_at=now() - interval '100 days'"
            " WHERE token_hash=%s",
            (chat.token_hash(session),),
        )
        conn.commit()
        result = retention.purge(conn, 90)
        left = conn.execute("SELECT token_hash FROM chat_sessions").fetchall()
        counts = conn.execute(
            "SELECT (SELECT count(*) FROM chat_proposals) AS p,"
            " (SELECT count(*) FROM appointments) AS a"
        ).fetchone()
    assert result["chat_sessions"] == 1
    assert [bytes(r["token_hash"]) for r in left] == [chat.token_hash(fresh)]
    assert (counts["p"], counts["a"]) == (0, 1)
    assert_envelope(say(env, session, "hello?"), 404, "not_found")


def test_migration_019_repeats_the_018_client_role_hardening():
    import re
    from pathlib import Path

    def block(name):
        text = Path(f"migrations/{name}").read_text()
        return [b for b in re.findall(r"DO \$\$.*?\$\$;", text, re.S) if "'anon'" in b]

    assert block("019_chat.sql") == block("018_platform.sql")
    text = Path("migrations/019_chat.sql").read_text()
    for table in ("chat_sessions", "chat_messages", "chat_proposals"):
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;" in text


def test_access_log_redacts_session_ids():
    from clinic_voice.api import redact_path

    token = "ab" * 16
    assert token not in redact_path(f"/v1/chat/sessions/{token}/messages?x=1")
    assert redact_path(f"/v1/chat/sessions/{token}") == "/v1/chat/sessions/<redacted>"
    assert redact_path(f"/api/v1/chat//sessions/{token}/proposals/p/confirm").endswith(
        "/sessions/<redacted>/proposals/p/confirm"
    )


def test_session_ids_are_random_128_bit(env):
    ids = {start(env) for _ in range(3)}
    assert len(ids) == 3 and all(len(UUID(hex=i).hex) == 32 for i in ids)


# --- Review fixes: confirm gate (clinic row lock, hidden services, parallel confirms) --------


def _hide_service(url, **values):
    with connect(url) as conn:
        sets = ", ".join(f"{k}=%s" for k in values)
        conn.execute(
            f"UPDATE services SET {sets} WHERE id=%s",  # noqa: S608
            (*values.values(), SERVICE_ID),
        )


@pytest.mark.parametrize("change", [{"public": False}, {"active": False}])
def test_a_service_hidden_after_the_proposal_cannot_be_booked(env, change):
    session = start(env)
    env["fake"].script = propose_script()
    proposal = say(env, session, "book").json()["proposal"]["id"]
    _hide_service(env["url"], **change)
    assert_envelope(confirm(env, session, proposal), 409, "slot_unavailable")
    with connect(env["url"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM appointments").fetchone()["n"] == 0
        assert (
            conn.execute("SELECT consumed_at FROM chat_proposals").fetchone()["consumed_at"] is None
        )


def test_the_confirm_gate_rechecks_the_offer_inside_the_transaction(env, monkeypatch):
    session = start(env)
    env["fake"].script = propose_script()
    proposal = say(env, session, "book").json()["proposal"]["id"]
    real = chat._offered
    seen = []

    def hide_after_precheck(conn, clinic_id, row, public_only=True):
        seen.append(public_only)
        if len(seen) == 2:  # The gate's check, inside the booking transaction.
            _hide_service(env["url"], public=False)
        return real(conn, clinic_id, row, public_only)

    monkeypatch.setattr(chat, "_offered", hide_after_precheck)
    assert_envelope(confirm(env, session, proposal), 409, "slot_unavailable")
    assert seen == [True, True]
    with connect(env["url"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM appointments").fetchone()["n"] == 0


def test_a_replayed_confirm_still_answers_after_the_service_is_hidden(env):
    session = start(env)
    env["fake"].script = propose_script()
    proposal = say(env, session, "book").json()["proposal"]["id"]
    first = confirm(env, session, proposal)
    _hide_service(env["url"], public=False)
    again = confirm(env, session, proposal)
    assert first.status_code == again.status_code == 202 and again.json() == first.json()


def test_the_confirm_gate_takes_the_clinic_row_for_share(env):
    """A clinic update in flight (it holds the row FOR NO KEY UPDATE, like the UPDATE in
    admin.update_clinic) blocks the confirm at the gate; once it commits chat off, the
    confirm books nothing. Foreign-key checks alone (FOR KEY SHARE) would not wait."""
    import threading

    session = start(env)
    env["fake"].script = propose_script()
    proposal = say(env, session, "book").json()["proposal"]["id"]
    result = {}

    def run():
        result["response"] = confirm(env, session, proposal)

    worker = threading.Thread(target=run)
    with connect(env["url"]) as holder:  # Commits (and releases the row) on every exit.
        holder.execute("SELECT 1 FROM clinics WHERE id=%s FOR NO KEY UPDATE", (CLINIC,))
        worker.start()
        worker.join(timeout=2)
        blocked = worker.is_alive()
        holder.execute("UPDATE clinics SET public_chat_enabled=false WHERE id=%s", (CLINIC,))
    worker.join(timeout=10)
    assert blocked, "the confirm must wait for the clinic row"
    assert not worker.is_alive()
    assert_envelope(result["response"], 404, "not_found")
    with connect(env["url"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM appointments").fetchone()["n"] == 0


def test_parallel_confirms_with_different_keys_book_once(env, monkeypatch):
    import threading

    monkeypatch.setenv("PUBLIC_BOOKING_PHONE_PER_DAY", "100")
    monkeypatch.setenv("PUBLIC_BOOKING_IP_PER_HOUR", "100")
    get_settings.cache_clear()
    session = start(env)
    env["fake"].script = propose_script()
    proposal = say(env, session, "book").json()["proposal"]["id"]
    barrier = threading.Barrier(4)
    responses = []

    def run(index):
        barrier.wait()
        responses.append(
            confirm(env, session, proposal, headers={"Idempotency-Key": f"card-key-{index:04d}"})
        )

    threads = [threading.Thread(target=run, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    statuses = sorted(r.status_code for r in responses)
    assert statuses == [202, 409, 409, 409], [r.text for r in responses]
    assert {r.json()["error"]["code"] for r in responses if r.status_code == 409} == {
        "proposal_used"
    }
    with connect(env["url"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM appointments").fetchone()["n"] == 1
        used = conn.execute("SELECT consumed_at, appointment_id FROM chat_proposals").fetchone()
    assert used["consumed_at"] is not None and used["appointment_id"] is not None


# --- Review fixes: unclosed reasoning and length-truncated replies ---------------------------


def _completion(content, finish_reason="stop", tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message, "finish_reason": finish_reason}]}


@pytest.mark.parametrize(
    ("content", "text"),
    [
        ("<think>closed</think>Hello", "Hello"),
        ("Hello <think>never closed, cut off by max_tok", "Hello"),
        ("<THINK>\nall reasoning\nno end", None),
        ("<think>a</think>Hi<think>b", "Hi"),
        ("No reasoning at all", "No reasoning at all"),
    ],
)
def test_reasoning_blocks_are_dropped_closed_or_not(content, text):
    assert parse_reply(_completion(content)).text == text


def test_a_length_truncated_reply_without_visible_text_is_a_failure():
    with pytest.raises(ChatLLMError) as raised:
        parse_reply(_completion("<think>thinking until the token limit", "length"))
    assert raised.value.code == "length"
    with pytest.raises(ChatLLMError):
        parse_reply(_completion(None, "length"))
    # Visible text survives a length stop; tool calls of a truncated reply still run.
    assert parse_reply(_completion("Partial but visible", "length")).text == "Partial but visible"
    calls = [{"id": "c1", "function": {"name": "clinic_info", "arguments": "{}"}}]
    reply = parse_reply(_completion("<think>cut", "length", calls))
    assert reply.text is None and reply.tool_calls == [ToolCall("c1", "clinic_info", {})]


def test_chat_falls_back_on_a_truncated_reasoning_reply(env, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    replies = [
        _completion(
            "<think>tool first",
            "length",
            [{"id": "c1", "function": {"name": "clinic_info", "arguments": "{}"}}],
        ),
        _completion("<think>SYNTHETIC-REASONING the clinic opens at", "length"),
    ]
    llm = _adapter(lambda request: httpx.Response(200, json=replies.pop(0)))
    monkeypatch.setattr(routes_chat, "llm_for", lambda settings: llm)
    session = start(env)
    body = say(env, session, "when do you open?").json()
    assert body["quick_replies"] == chat.FALLBACK_QUICK_REPLIES
    assert "SYNTHETIC-REASONING" not in json.dumps(body)
    assert "code=length" in caplog.text and "SYNTHETIC-REASONING" not in caplog.text
    keyring = Keyring.from_setting(KEYS)
    with connect(env["url"]) as conn:
        stored = chat.transcript(conn, keyring, CLINIC, session_row(env["url"])["id"])
    # The tool call of the first, truncated reply ran and was answered.
    assert any(m["role"] == "tool" and m["tool_name"] == "clinic_info" for m in stored)
    assert "SYNTHETIC-REASONING" not in json.dumps([m["document"] for m in stored])


# --- Review fixes: the per-clinic daily model cap --------------------------------------------


def test_model_calls_are_capped_per_clinic_per_day(env, monkeypatch):
    monkeypatch.setenv("CHAT_LLM_CALLS_PER_CLINIC_PER_DAY", "3")
    get_settings.cache_clear()
    first, second = start(env), start(env)
    env["fake"].script = [LLMReply(None, [call("clinic_info")]), LLMReply("We open at 9.")]
    assert say(env, first, "when do you open?").json()["messages"][0]["text"] == "We open at 9."
    assert say(env, second, "hello").json()["quick_replies"] == chat.QUICK_REPLIES
    # Three model calls today; the fourth, from any session, is not made.
    over = say(env, first, "and on Sunday?")
    assert over.status_code == 200
    body = over.json()
    assert body["messages"][0]["text"].startswith("Sorry, I cannot answer right now.")
    assert body["quick_replies"] == chat.FALLBACK_QUICK_REPLIES
    assert len(env["fake"].calls) == 3
    # Emergencies never need the model and still get the script.
    assert say(env, second, "This is an emergency").json()["messages"][0]["text"] == (
        chat.DEFAULT_EMERGENCY
    )


# --- Review fixes: tool failures never orphan a tool call ------------------------------------


def _assert_every_tool_call_answered(model_messages):
    """The OpenAI contract strict providers enforce: each assistant tool_calls message is
    followed by exactly one tool result per call id."""
    for index, message in enumerate(model_messages):
        if message["role"] == "tool":
            assert index > 0
        if message.get("tool_calls"):
            ids = [c["id"] for c in message["tool_calls"]]
            after = model_messages[index + 1 : index + 1 + len(ids)]
            assert [m["role"] for m in after] == ["tool"] * len(ids)
            assert [m["tool_call_id"] for m in after] == ids


def test_an_unexpected_tool_exception_is_a_tool_failed_result(env, caplog):
    caplog.set_level(logging.INFO)
    session = start(env)
    bad = call(
        "propose_booking",
        service_id=str(SERVICE_ID),
        doctor_id=str(DOCTOR_ID),
        starts_at="0001-01-01T00:00:00+05:00",  # astimezone() raises OverflowError.
    )
    env["fake"].script = [LLMReply(None, [bad]), LLMReply("Which day would suit you?")]
    r = say(env, session, "book the first possible time")
    assert r.status_code == 200
    assert r.json()["messages"][0]["text"] == "Which day would suit you?"
    second = env["fake"].calls[1]["messages"]
    assert json.loads(second[-1]["content"]) == {"error": "tool_failed"}
    _assert_every_tool_call_answered(second)
    assert "error=OverflowError" in caplog.text and "0001-01-01" not in caplog.text
    # The next turn's history is still valid for a strict provider.
    say(env, session, "tomorrow then")
    _assert_every_tool_call_answered(env["fake"].calls[-1]["messages"])


def test_a_turn_failing_mid_tools_stores_placeholder_results(env, monkeypatch):
    session = start(env)
    env["fake"].script = [LLMReply(None, [call("clinic_info"), call("list_services")])]

    def explode(turn, name, arguments):
        raise RuntimeError("synthetic failure outside the tool")

    monkeypatch.setattr(chat, "run_tool", explode)
    body = say(env, session, "hello").json()
    assert body["quick_replies"] == chat.FALLBACK_QUICK_REPLIES
    keyring = Keyring.from_setting(KEYS)
    row = session_row(env["url"])
    with connect(env["url"]) as conn:
        stored = chat.transcript(conn, keyring, CLINIC, row["id"])
    calls = [m for m in stored if m["role"] == "assistant" and m["document"].get("tool_calls")]
    results = [m["document"] for m in stored if m["role"] == "tool"]
    ids = [c["id"] for c in calls[0]["document"]["tool_calls"]]
    assert [r["tool_call_id"] for r in results] == ids
    assert all(r["result"] == {"error": "tool_failed"} for r in results)


def test_history_drops_assistant_tool_calls_without_results(env):
    session = start(env)
    keyring = Keyring.from_setting(KEYS)
    row = session_row(env["url"])
    with connect(env["url"]) as conn:
        chat.append(conn, keyring, row, "user", {"text": "earlier question"})
        # An interrupted older turn: two calls, only one answered.
        chat.append(
            conn,
            keyring,
            row,
            "assistant",
            {
                "text": None,
                "tool_calls": [
                    {"id": "c_old1", "name": "clinic_info", "arguments": {}},
                    {"id": "c_old2", "name": "list_services", "arguments": {}},
                ],
            },
        )
        chat.append(conn, keyring, row, "tool", {"tool_call_id": "c_old1", "result": {}})
        # A stray result with no call before it.
        chat.append(conn, keyring, row, "tool", {"tool_call_id": "c_stray", "result": {}})
    env["fake"].script = [LLMReply("Hello again")]
    assert say(env, session, "are you there?").status_code == 200
    sent = env["fake"].calls[0]["messages"]
    assert not any(m.get("tool_calls") for m in sent)
    assert not any(m["role"] == "tool" for m in sent)
    assert [m["content"] for m in sent if m["role"] == "user"] == [
        "earlier question",
        "are you there?",
    ]
