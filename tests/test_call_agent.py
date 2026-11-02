"""The real-time call agent's synchronous side (ADR 0014, call_agent.CallState): the session,
the transcript, the turn limits and the clinic tools. No LiveKit, no network."""

import json

import pytest
from test_chat import DOCTOR_ID, NAME, SERVICE_ID, SLOT, session_row, start
from test_voice import appointments, env, user_texts  # noqa: F401  (fixture)

from clinic_voice import call_agent, chat
from clinic_voice.config import get_settings


def state(env, token):  # noqa: F811
    return call_agent.CallState(env["url"], get_settings(), token, "203.0.113.7")


def hold(call):
    call.run_tool(
        "c1", "check_slots", {"service_id": str(SERVICE_ID), "date": SLOT.date().isoformat()}
    )
    return call.run_tool(
        "c2",
        "propose_booking",
        {"service_id": str(SERVICE_ID), "doctor_id": str(DOCTOR_ID), "starts_at": SLOT.isoformat()},
    )


def test_metadata():
    token = "a" * 32
    assert call_agent.parse_metadata(json.dumps({"session": token, "address": "1.2.3.4"})) == (
        token,
        "1.2.3.4",
    )
    for raw in (None, "", "[]", '{"session": "x"}', json.dumps({"session": "A" * 32})):
        with pytest.raises(call_agent.CallError):
            call_agent.parse_metadata(raw)


def test_unknown_session_is_refused(env):  # noqa: F811
    with pytest.raises(call_agent.CallError):
        state(env, "0" * 32)


def test_prompt_and_tools(env):  # noqa: F811
    call = state(env, start(env))
    try:
        assert "Voice:" in call.prompt and "end_call" in call.prompt
        assert {s["name"] for s in call_agent.tool_schemas()} == chat.VOICE_TOOL_NAMES
        assert all("parameters" in s for s in call_agent.tool_schemas())
        assert call.greeting
    finally:
        call.close()


def test_turns_are_stored_and_counted(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CHAT_MAX_TURNS", "2")
    get_settings.cache_clear()
    token = start(env)
    call = state(env, token)
    try:
        assert call.user_said("hello there") == "ok"
        call.agent_said("Hi, how can I help?")
        assert call.user_said("my gums are bleeding heavily and it will not stop") == "emergency"
        assert call.emergency_text() == chat.emergency_script(call.clinic)
        assert call.user_said("one more") == "limit"
    finally:
        call.close()
    assert user_texts(env["url"])[:2] == [
        "hello there",
        "my gums are bleeding heavily and it will not stop",
    ]
    assert session_row(env["url"])["turn_count"] == 2


def test_daily_model_cap(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CHAT_LLM_CALLS_PER_CLINIC_PER_DAY", "1")
    get_settings.cache_clear()
    call = state(env, start(env))
    try:
        assert call.user_said("first") == "ok"
        assert call.user_said("second") == "capped"
        assert call.capped_text() == chat.fallback_text(call.clinic)
    finally:
        call.close()


def test_a_call_books_after_name_number_and_consent(env):  # noqa: F811
    call = state(env, start(env))
    try:
        call.user_said("book a consultation")
        held = hold(call)
        assert "error" not in held, held
        assert call.held()["service_name"] == "Consultation"
        # The model must not book details the caller has not said.
        refused = call.run_tool(
            "c3", "book_appointment", {"name": NAME, "phone": "9876543210", "consent_contact": True}
        )
        assert refused["error"] == "name_not_given"
        call.user_said(f"I am {NAME}, nine eight seven six five 43210")
        call.user_said("yes, that's right, you can contact me")
        booked = call.run_tool(
            "c4",
            "book_appointment",
            {"name": NAME, "phone": "98765 43210", "consent_contact": True},
        )
        assert "error" not in booked, booked
        booking = call.booking()
        assert booking["reference"] and booking["service_name"] == "Consultation"
        assert booking["text"] and call.held() is None
    finally:
        call.close()
    assert appointments(env) == [{"channel": "chat"}]
