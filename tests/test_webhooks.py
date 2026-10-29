"""Bolna execution webhooks: inbox, monotonic call-session state, reconciliation (T8).

Synthetic data only: example.test hosts, +9100000xxxxx numbers, generated credentials.
Payload shapes follow the execution payload reference in docs/SOURCE_REVIEW.md; none was
captured from Bolna. FakeBolna stands in for GET /executions; nothing reaches the network.
"""

import base64
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, Thread
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from clinic_voice import cli, credentials, outbox, retention, staff
from clinic_voice.api import StripQuery, app
from clinic_voice.bolna.webhooks import MAX_RECONCILES, enqueue_reconciles
from clinic_voice.cli import DEMO_CLINIC_ID as CLINIC
from clinic_voice.config import get_settings
from clinic_voice.db import connect
from clinic_voice.handlers import HANDLERS
from clinic_voice.handlers import bolna as bolna_handlers
from clinic_voice.handlers.calendar import needs_review_on_dead
from clinic_voice.outbox import Handler, Ports, check_ports, claim, fail_dead, run_once
from clinic_voice.providers.bolna import BolnaClient, Execution, FakeBolna, minimize_execution
from clinic_voice.providers.calendar import FakeCalendar, ProviderTimeout

KEYS = "k1:" + base64.b64encode(b"\x02" * 32).decode()
HASH_KEY = base64.b64encode(b"\x03" * 32).decode()
CALLER = "+910000000123"
CLINIC_LINE = "+910000000009"
EXECUTION = "5f0c6f7e-1d1a-4c55-9a43-0b6f1b2a0101"
OTHER_EXECUTION = "5f0c6f7e-1d1a-4c55-9a43-0b6f1b2a0102"
AGENT = "11111111-1111-4111-8111-111111111111"
OTHER_AGENT = "22222222-2222-4222-8222-222222222222"
OTHER_CLINIC = UUID("00000000-0000-4000-8000-0000000000c1")
MARKER = "SYNTHETIC-BODY-MARKER"
RECORDING = "https://recordings.example.test/synthetic.wav"
PASSWORD = "synthetic-password-01"
ALLOWED_KEYS = {
    "execution_id",
    "status",
    "agent_id",
    "created_at",
    "initiated_at",
    "updated_at",
    "conversation_duration",
    "telephony_duration",
    "termination_reason",
    "hangup_reason",
    "hangup_by",
    "hangup_provider_code",
    "call_type",
    "total_cost",
}


@pytest.fixture
def env(database, monkeypatch):
    monkeypatch.setattr(staff, "SCRYPT_N", 2**10)
    monkeypatch.setenv("DATABASE_URL", database)
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEYS", KEYS)
    monkeypatch.setenv("PHONE_HASH_KEY", HASH_KEY)
    for name in ("BOLNA_WEBHOOK_ALLOWED_IPS", "TRUSTED_PROXY_HOPS", "WEBHOOK_MAX_BODY_BYTES"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    with connect(database) as conn:
        conn.execute(
            "INSERT INTO clinics (id,name) VALUES (%s,'Other Synthetic Clinic')", (OTHER_CLINIC,)
        )
        _, hook = credentials.issue(conn, CLINIC, "bolna-webhook", scope="webhook")
        _, other_hook = credentials.issue(conn, OTHER_CLINIC, "bolna-webhook", scope="webhook")
        _, tool = credentials.issue(conn, CLINIC, "bolna-tools")
    with TestClient(app) as client:
        yield {
            "client": client,
            "url": database,
            "hook": hook,
            "other_hook": other_hook,
            "tool": tool,
        }
    get_settings.cache_clear()


def settings_env(monkeypatch, **values):
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


def event(status, execution=EXECUTION, agent=AGENT, **extra):
    """A full synthetic execution payload, including fields that must never be stored."""
    body = {
        "id": execution,
        "agent_id": agent,
        "batch_id": None,
        "status": status,
        "termination_reason": None,
        "smart_status": f"{MARKER} smart status",
        "error_message": f"{MARKER} error text",
        "conversation_duration": 0,
        "created_at": "2031-03-03T03:00:00+00:00",
        "initiated_at": "2031-03-03T03:00:05+00:00",
        "updated_at": "2031-03-03T03:01:00+00:00",
        "user_number": CALLER,
        "agent_number": CLINIC_LINE,
        "provider": "plivo",
        "transcript": f"user: {MARKER} my number is {CALLER}",
        "summary": f"{MARKER} summary",
        "extracted_data": {"patient": {"name": MARKER}},
        "telephony_data": {
            "duration": 0,
            "to_number": CLINIC_LINE,
            "from_number": CALLER,
            "recording_url": RECORDING,
            "hosted_telephony": True,
            "provider_call_id": f"{MARKER}-call-id",
            "call_type": "inbound",
            "provider": "plivo",
            "hangup_by": None,
            "hangup_reason": None,
            "hangup_provider_code": None,
        },
        "total_cost": 0,
        "cost_breakdown": {"llm": 0},
        "context_details": {"recipient_phone_number": CALLER, "recipient_data": {"x": MARKER}},
        "retry_count": 0,
    }
    telephony = extra.pop("telephony", {})
    body["telephony_data"].update(telephony)
    body.update(extra)
    return body


def completed(**extra):
    values = {
        "termination_reason": "call_ended",
        "conversation_duration": 182.5,
        "total_cost": 12.5,
        "telephony": {"duration": 183, "hangup_by": "user", "hangup_reason": "normal_hangup"},
    }
    return event("completed", **(values | extra))


def deliver(env, body, token=None, **kwargs):
    return env["client"].post(f"/v1/webhooks/bolna/{token or env['hook']}", json=body, **kwargs)


def add_session(url, clinic=CLINIC, execution=EXECUTION, agent=AGENT, age=None):
    session_id = uuid4()
    with connect(url) as conn:
        conn.execute(
            "INSERT INTO call_sessions (id,clinic_id,execution_id,agent_id,started_at)"
            " VALUES (%s,%s,%s,%s,now() - %s)",
            (session_id, clinic, execution, agent, age or timedelta(0)),
        )
    return session_id


def configure_agent(url, clinic=CLINIC, agent=AGENT):
    with connect(url) as conn:
        conn.execute("UPDATE clinics SET bolna_agent_id=%s WHERE id=%s", (agent, clinic))


def session(url, clinic=CLINIC, execution=EXECUTION):
    with connect(url) as conn:
        return conn.execute(
            "SELECT * FROM call_sessions WHERE clinic_id=%s AND execution_id=%s",
            (clinic, execution),
        ).fetchone()


def inbox(url, clinic=None):
    with connect(url) as conn:
        return conn.execute(
            "SELECT * FROM webhook_inbox WHERE %s::uuid IS NULL OR clinic_id=%s"
            " ORDER BY received_seq",
            (clinic, clinic),
        ).fetchall()


def jobs(url, kind=None):
    with connect(url) as conn:
        return conn.execute(
            "SELECT * FROM outbox WHERE %s::text IS NULL OR kind=%s ORDER BY created_at, id",
            (kind, kind),
        ).fetchall()


def work(url, bolna=None, limit=10):
    return run_once(url, "w1", Ports(calendar=FakeCalendar(), bolna=bolna), limit=limit)


def nothing_stored(url):
    return inbox(url) == [] and jobs(url) == []


# Receipt, dedupe and ordering


def test_duplicate_delivery_is_harmless(env):
    first = deliver(env, event("in-progress"))
    assert first.status_code == 200 and first.json() == {"status": "accepted"}
    assert first.headers["cache-control"] == "no-store"
    again = deliver(env, event("in-progress"))
    assert again.status_code == 200 and again.json() == {"status": "duplicate"}
    assert len(inbox(env["url"])) == 1
    assert len(jobs(env["url"], "webhook.process")) == 1
    # Fields outside the allowlist do not change the event key: still a duplicate.
    assert deliver(env, event("in-progress", transcript="other")).json()["status"] == "duplicate"
    # The same status with different allowlisted values is a distinct event.
    changed = event("in-progress", updated_at="2031-03-03T03:02:00+00:00")
    assert deliver(env, changed).json()["status"] == "accepted"
    assert len(inbox(env["url"])) == 2 and len(jobs(env["url"])) == 2


def test_every_status_is_kept_in_order_of_receipt(env):
    add_session(env["url"])
    statuses = ["queued", "ringing", "in-progress", "call-disconnected"]
    for status in statuses:
        assert deliver(env, event(status)).status_code == 200
    assert deliver(env, completed()).status_code == 200
    rows = inbox(env["url"])
    assert [r["provider_status"] for r in rows] == [*statuses, "completed"]
    assert [r["status"] for r in rows] == ["received"] * 5
    assert work(env["url"]) == 5
    rows = inbox(env["url"])
    assert [r["status"] for r in rows] == ["processed"] * 5
    assert [r["outcome"] for r in rows] == ["applied"] * 5
    assert all(r["attempts"] == 1 and r["processed_at"] for r in rows)
    row = session(env["url"])
    assert (row["provider_status"], row["provider_final"], row["status"]) == (
        "completed",
        True,
        "ended",
    )
    assert row["ended_at"] is not None and row["next_reconcile_at"] is None
    assert (row["termination_reason"], row["hangup_reason"], row["hangup_by"]) == (
        "call_ended",
        "normal_hangup",
        "user",
    )
    assert (row["call_type"], row["conversation_seconds"], row["total_cost"]) == (
        "inbound",
        182.5,
        12.5,
    )


def test_regression_after_final_is_ignored(env):
    add_session(env["url"])
    first = event("completed", termination_reason="call_ended")
    del first["total_cost"]  # Not yet known at the first delivery.
    deliver(env, first)
    work(env["url"])
    assert session(env["url"])["total_cost"] is None
    for status in ("in-progress", "ringing", "failed"):
        deliver(env, event(status))
    work(env["url"])
    row = session(env["url"])
    assert (row["provider_status"], row["provider_final"]) == ("completed", True)
    later = inbox(env["url"])[1:]
    assert [(r["status"], r["outcome"]) for r in later] == [("ignored", "stale")] * 3
    # A repeat of the final status may only fill dispositions that are still empty.
    deliver(env, completed(termination_reason="other_reason"))
    work(env["url"])
    row = session(env["url"])
    assert row["termination_reason"] == "call_ended"  # Not replaced.
    assert row["total_cost"] == 12.5 and row["hangup_by"] == "user"  # Filled.
    assert row["provider_status"] == "completed"


def test_out_of_order_processing_keeps_highest_status(env):
    add_session(env["url"])
    deliver(env, completed())
    deliver(env, event("ringing"))
    deliver(env, event("in-progress"))
    with connect(env["url"]) as conn:  # Process the final event last.
        conn.execute(
            "UPDATE outbox SET available_at = now() + interval '1 hour'"
            " WHERE payload->>'inbox_id' = (SELECT id::text FROM webhook_inbox"
            " WHERE provider_status='completed')"
        )
    work(env["url"])
    assert session(env["url"])["provider_status"] == "in-progress"
    with connect(env["url"]) as conn:
        conn.execute("UPDATE outbox SET available_at = now() WHERE status='pending'")
    work(env["url"])
    assert session(env["url"])["provider_status"] == "completed"
    # Then a stale ringing redelivered with new allowlisted content is ignored.
    deliver(env, event("ringing", updated_at="2031-03-03T03:09:00+00:00"))
    work(env["url"])
    assert inbox(env["url"])[-1]["outcome"] == "stale"


def test_concurrent_processors_converge_on_final(env):
    add_session(env["url"])
    deliver(env, event("in-progress"))
    deliver(env, completed())

    def one(name):
        return run_once(env["url"], name, Ports(calendar=FakeCalendar()), limit=1)

    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(one, ["w1", "w2"])) == 2
    assert session(env["url"])["provider_status"] == "completed"
    assert {r["status"] for r in inbox(env["url"])} <= {"processed", "ignored"}


def test_session_ends_and_tools_refuse_it(env):
    add_session(env["url"])
    deliver(env, event("call-disconnected"))
    work(env["url"])
    row = session(env["url"])
    assert (row["status"], row["provider_final"]) == ("ended", False)
    r = env["client"].post(
        "/v1/bolna/tools/check-slots",
        json={
            "execution_id": EXECUTION,
            "from_number": CALLER,
            "agent_id": AGENT,
            "doctor": "Demo Doctor",
            "service": "Consultation",
            "day": "2031-03-05",
        },
        headers={"Authorization": f"Bearer {env['tool']}"},
    )
    assert r.status_code == 409


def test_unknown_execution_and_status_are_ignored(env):
    deliver(env, event("completed", execution=OTHER_EXECUTION))
    add_session(env["url"])
    deliver(env, event("teleported"))
    work(env["url"])
    assert [(r["status"], r["outcome"]) for r in inbox(env["url"])] == [
        ("ignored", "unknown_execution"),
        ("ignored", "unknown_status"),
    ]
    assert session(env["url"])["provider_status"] is None
    assert session(env["url"], execution=OTHER_EXECUTION) is None  # No session is created.


# Authentication, tenant isolation and request limits


def test_bad_or_absent_secret_is_401_with_nothing_stored(env):
    hook_id, _ = credentials.parse(env["hook"], scope="webhook")
    bad = [
        "not-a-token",
        "cvw_" + "f" * 32 + "." + "B" * 43,  # Unknown id.
        f"cvw_{hook_id.hex}." + "B" * 43,  # Wrong secret.
        env["tool"],  # A tool credential is not a webhook credential.
        "cvt_" + env["hook"][4:],  # Same id and secret, tool prefix.
    ]
    for token in bad:
        r = deliver(env, event("completed"), token=token)
        assert r.status_code == 401, token
        assert r.json() == {"detail": "Invalid credentials"}
    assert env["client"].post("/v1/webhooks/bolna/", json=event("completed")).status_code in (
        404,
        405,
    )
    assert env["client"].post("/v1/webhooks/bolna", json=event("completed")).status_code in (
        404,
        405,
    )
    with connect(env["url"]) as conn:
        credentials.revoke(conn, hook_id)
    assert deliver(env, event("completed")).status_code == 401
    assert nothing_stored(env["url"])
    # The webhook credential does not authenticate the tool gateway either.
    r = env["client"].post(
        "/v1/bolna/context", headers={"Authorization": f"Bearer {env['other_hook']}"}
    )
    assert r.status_code == 401


def test_webhook_credential_rotation_keeps_scope(env):
    old_id, _ = credentials.parse(env["hook"], scope="webhook")
    with connect(env["url"]) as conn:
        new_id, new = credentials.rotate(conn, old_id, timedelta(minutes=5))
        rows = conn.execute(
            "SELECT id, scope FROM tenant_credentials WHERE id = ANY(%s)", ([old_id, new_id],)
        ).fetchall()
    scopes = {r["id"]: r["scope"] for r in rows}
    assert new.startswith("cvw_") and scopes == {old_id: "webhook", new_id: "webhook"}
    assert deliver(env, event("queued"), token=new).status_code == 200
    assert deliver(env, event("ringing")).status_code == 200  # Old one within its grace.


def test_other_clinic_token_cannot_touch_this_clinic(env):
    add_session(env["url"])
    r = deliver(env, completed(), token=env["other_hook"])
    assert r.status_code == 200
    work(env["url"])
    [row] = inbox(env["url"])
    assert row["clinic_id"] == OTHER_CLINIC
    assert (row["status"], row["outcome"]) == ("ignored", "unknown_execution")
    mine = session(env["url"])
    assert mine["provider_status"] is None and mine["status"] == "active"
    # The same event for the owning clinic is a separate inbox row, not a duplicate.
    assert deliver(env, completed()).json() == {"status": "accepted"}


def test_configured_agent_id_is_cross_checked(env):
    with connect(env["url"]) as conn:
        conn.execute("UPDATE clinics SET bolna_agent_id=%s WHERE id=%s", (AGENT, CLINIC))
    r = deliver(env, event("queued", agent=OTHER_AGENT))
    assert r.status_code == 403
    assert nothing_stored(env["url"])
    assert deliver(env, event("queued")).status_code == 200
    without = event("ringing")
    del without["agent_id"]
    assert deliver(env, without).status_code == 403  # A configured agent must be named.
    assert len(inbox(env["url"])) == 1
    # The session's own agent id is checked by the processor too.
    with connect(env["url"]) as conn:
        conn.execute("UPDATE clinics SET bolna_agent_id=NULL WHERE id=%s", (CLINIC,))
    assert deliver(env, without).status_code == 200
    add_session(env["url"], agent=OTHER_AGENT)
    deliver(env, completed())
    work(env["url"])
    assert inbox(env["url"])[-1]["outcome"] == "agent_mismatch"
    assert session(env["url"])["provider_status"] == "ringing"


def test_agent_configured_after_intake_is_enforced_by_processor(env):
    add_session(env["url"], agent=None)
    without = completed()
    del without["agent_id"]
    deliver(env, without)
    deliver(env, event("ringing", agent=OTHER_AGENT))
    configure_agent(env["url"])
    work(env["url"])
    assert [r["outcome"] for r in inbox(env["url"])] == ["agent_mismatch", "agent_mismatch"]
    assert session(env["url"])["provider_status"] is None


def test_oversize_body_is_413(env, monkeypatch):
    settings_env(monkeypatch, WEBHOOK_MAX_BODY_BYTES="1024")
    big = event("completed", padding="x" * 2000)
    r = deliver(env, big)
    assert r.status_code == 413 and r.json() == {"detail": "Request body too large"}

    def chunks():  # No Content-Length: the streamed body is cut off at the limit.
        for _ in range(10):
            yield b" " * 200

    r = env["client"].post(
        f"/v1/webhooks/bolna/{env['hook']}",
        content=chunks(),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 413
    assert nothing_stored(env["url"])
    small = event("queued")
    for key in ("transcript", "summary", "extracted_data", "context_details", "error_message"):
        small.pop(key)
    assert deliver(env, small).status_code == 200


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"[]",
        b'{"status": "completed"}',
        b'{"id": "not-a-uuid", "status": "completed"}',
        b'{"id": "5f0c6f7e-1d1a-4c55-9a43-0b6f1b2a0101", "status": "Completed Now"}',
        b'{"id": "5f0c6f7e-1d1a-4c55-9a43-0b6f1b2a0101", "status": "queued",'
        b' "status": "completed"}',
    ],
)
def test_unrecognized_events_are_422(env, content):
    r = env["client"].post(
        f"/v1/webhooks/bolna/{env['hook']}",
        content=content,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422
    assert r.json() == {"detail": "Unrecognized webhook event"}
    assert nothing_stored(env["url"])


def test_ip_allowlist_when_configured(env, monkeypatch):
    settings_env(monkeypatch, BOLNA_WEBHOOK_ALLOWED_IPS="13.203.39.153, 10.0.0.0/8")
    # No trusted proxy: X-Forwarded-For is ignored and the test client's peer is not listed.
    r = deliver(env, event("queued"), headers={"X-Forwarded-For": "13.203.39.153"})
    assert r.status_code == 403
    assert nothing_stored(env["url"])
    settings_env(monkeypatch, TRUSTED_PROXY_HOPS="1")
    ok = deliver(env, event("queued"), headers={"X-Forwarded-For": "198.51.100.7, 13.203.39.153"})
    assert ok.status_code == 200
    assert (
        deliver(env, event("ringing"), headers={"X-Forwarded-For": "10.1.2.3"}).status_code == 200
    )
    spoofed = deliver(
        env, event("in-progress"), headers={"X-Forwarded-For": "13.203.39.153, 198.51.100.7"}
    )
    assert spoofed.status_code == 403
    assert deliver(env, event("in-progress")).status_code == 403  # Missing header.
    assert len(inbox(env["url"])) == 2
    # The allowlist is checked before the credential: a refused source learns nothing.
    assert deliver(env, event("queued"), token="bad").status_code == 403


def test_invalid_allowlist_setting_fails_clearly(monkeypatch):
    from pydantic import ValidationError

    from clinic_voice.config import Settings

    with pytest.raises(ValidationError):
        Settings(database_url="postgresql://x/y_test", bolna_webhook_allowed_ips="13.0.0.999")


# Minimization


def test_allowlist_drops_transcript_phone_and_recording(env):
    add_session(env["url"])
    deliver(env, completed())
    work(env["url"])
    [row] = inbox(env["url"])
    assert set(row["payload"]) <= ALLOWED_KEYS
    assert row["payload"]["execution_id"] == EXECUTION
    with connect(env["url"]) as conn:
        dumped = json.dumps(
            [
                conn.execute("SELECT * FROM webhook_inbox").fetchall(),
                conn.execute("SELECT * FROM call_sessions").fetchall(),
                conn.execute("SELECT * FROM outbox").fetchall(),
                conn.execute("SELECT * FROM audit_events").fetchall(),
            ],
            default=str,
        )
    for secret in (MARKER, CALLER, CLINIC_LINE, RECORDING, "recordings.example.test", "plivo"):
        assert secret not in dumped


@pytest.mark.parametrize(
    "value",
    [
        "the caller said something private",
        "+910000000123",
        "9100000001",
        "call-9876543",
        "a.b",
        "code1",
        "_leading",
        "x" * 65,
    ],
)
def test_minimize_drops_codes_that_are_not_plain_words(value):
    body = event("completed", termination_reason=value, telephony={"hangup_reason": value})
    minimized = minimize_execution(body)
    assert "termination_reason" not in minimized and "hangup_reason" not in minimized
    assert (
        minimize_execution(event("completed", termination_reason="Call_ended-by-user"))[
            "termination_reason"
        ]
        == "Call_ended-by-user"
    )


@pytest.mark.parametrize(
    "value",
    [
        "0001-01-01T00:00:00+05:30",  # Before datetime.min in UTC.
        "9999-12-31T23:59:59-05:00",  # After datetime.max in UTC.
        "9999-12-31T23:59:59+99:00",  # Invalid offset.
    ],
)
def test_minimize_drops_out_of_range_timestamps(value):
    minimized = minimize_execution(event("queued", updated_at=value))
    assert "updated_at" not in minimized
    assert (
        minimize_execution(event("queued", updated_at="9999-12-31T23:00:00+05:30"))["updated_at"]
        == "9999-12-31T17:30:00+00:00"
    )


def test_minimize_rejects_free_text_codes_and_bad_numbers():
    body = event(
        "completed",
        termination_reason="the caller said something private",
        conversation_duration=-1,
        total_cost="NaN",
        created_at="2031-03-03T03:00:00",  # No offset: dropped, not guessed.
        telephony={"hangup_provider_code": True, "call_type": "sideways", "duration": "183"},
    )
    minimized = minimize_execution(body)
    assert minimized == {
        "execution_id": EXECUTION,
        "status": "completed",
        "agent_id": AGENT,
        "initiated_at": "2031-03-03T03:00:05+00:00",
        "updated_at": "2031-03-03T03:01:00+00:00",
        "telephony_duration": 183.0,
    }
    assert minimize_execution({"id": EXECUTION.upper(), "status": "queued"})["execution_id"] == (
        EXECUTION
    )


# Processor idempotency and dead letters


def test_processor_is_idempotent(env):
    add_session(env["url"])
    deliver(env, completed())
    [job] = jobs(env["url"])
    with connect(env["url"]) as conn:
        [claimed] = claim(conn, "w1")
        assert bolna_handlers.process_webhook(conn, claimed, Ports(calendar=FakeCalendar()))
    before = session(env["url"])
    # A retry of the same job, and a duplicate job for the same event, change nothing.
    with connect(env["url"]) as conn:
        conn.execute("UPDATE outbox SET status='pending' WHERE id=%s", (job["id"],))
        conn.execute(
            "INSERT INTO outbox (id,clinic_id,kind,payload) VALUES (%s,%s,'webhook.process',%s)",
            (uuid4(), CLINIC, json.dumps(job["payload"])),
        )
    assert work(env["url"]) == 2
    assert session(env["url"]) == before
    [row] = inbox(env["url"])
    assert (row["status"], row["attempts"]) == ("processed", 1)
    assert {j["status"] for j in jobs(env["url"])} == {"done"}


def test_lost_lease_applies_nothing(env):
    add_session(env["url"])
    deliver(env, completed())
    with connect(env["url"]) as conn:
        [claimed] = claim(conn, "w1")
        conn.execute("UPDATE outbox SET lease_owner='w2' WHERE id=%s", (claimed.id,))
        conn.commit()
        assert (
            bolna_handlers.process_webhook(conn, claimed, Ports(calendar=FakeCalendar())) is False
        )
    assert session(env["url"])["provider_status"] is None
    [row] = inbox(env["url"])
    assert row["status"] == "received"


def staff_headers(env, role="owner"):
    email = f"{role}-{uuid4().hex[:8]}@example.test"
    with connect(env["url"]) as conn:
        staff.create_user(conn, CLINIC, email, role, PASSWORD)
    r = env["client"].post("/v1/staff/login", json={"email": email, "password": PASSWORD})
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_dead_job_marks_event_failed_and_requeue_reprocesses(env):
    add_session(env["url"])
    deliver(env, completed())
    with connect(env["url"]) as conn:
        [claimed] = claim(conn, "w1")
        assert fail_dead(conn, claimed, "internal")
    [row] = inbox(env["url"])
    assert (row["status"], row["processed_at"] is not None) == ("failed", True)
    owner = staff_headers(env)
    r = env["client"].post(f"/v1/admin/outbox/{claimed.id}/requeue", headers=owner)
    assert r.status_code == 200
    [row] = inbox(env["url"])
    assert (row["status"], row["processed_at"]) == ("received", None)
    work(env["url"])
    assert inbox(env["url"])[0]["status"] == "processed"
    assert session(env["url"])["provider_status"] == "completed"


# Reconciliation


def test_reconciliation_fills_missed_final_status(env):
    configure_agent(env["url"])
    add_session(env["url"], age=timedelta(hours=2))
    add_session(env["url"], execution=OTHER_EXECUTION, age=timedelta(minutes=5))  # Too recent.
    fake = FakeBolna()
    fake.executions[EXECUTION] = Execution(
        EXECUTION, AGENT, "completed", "call_ended", "inbound", 42.0
    )
    with connect(env["url"]) as conn:
        assert enqueue_reconciles(conn, timedelta(minutes=30), 50) == 1
        assert enqueue_reconciles(conn, timedelta(minutes=30), 50) == 0  # Backoff.
    [job] = jobs(env["url"], "bolna.reconcile")
    assert job["appointment_id"] is None and job["payload"]["session_id"]
    assert work(env["url"], bolna=fake) == 1
    row = session(env["url"])
    assert (row["provider_status"], row["provider_final"], row["status"]) == (
        "completed",
        True,
        "ended",
    )
    assert (row["termination_reason"], row["conversation_seconds"]) == ("call_ended", 42.0)
    assert fake.calls == [("get_execution", (EXECUTION,))]
    assert jobs(env["url"], "bolna.reconcile")[0]["status"] == "done"
    with connect(env["url"]) as conn:  # A final session is never polled again.
        conn.execute("UPDATE call_sessions SET next_reconcile_at = now() - interval '1 day'")
        assert enqueue_reconciles(conn, timedelta(minutes=1), 50) == 1  # Only the other one.


def test_reconciliation_is_bounded_and_backs_off(env):
    configure_agent(env["url"])
    for index in range(3):
        add_session(env["url"], execution=str(uuid4()), age=timedelta(hours=1 + index))
    with connect(env["url"]) as conn:
        assert enqueue_reconciles(conn, timedelta(minutes=30), 2) == 2
        assert enqueue_reconciles(conn, timedelta(minutes=30), 2) == 1
        assert enqueue_reconciles(conn, timedelta(minutes=30), 2) == 0
        rows = conn.execute(
            "SELECT next_reconcile_at - now() AS wait FROM call_sessions"
        ).fetchall()
        assert all(timedelta(minutes=29) < r["wait"] <= timedelta(minutes=30) for r in rows)
        for _ in range(1, MAX_RECONCILES):
            conn.execute("UPDATE call_sessions SET next_reconcile_at = now() - interval '1 s'")
            assert enqueue_reconciles(conn, timedelta(minutes=30), 10) == 3
        conn.execute("UPDATE call_sessions SET next_reconcile_at = now() - interval '1 s'")
        assert enqueue_reconciles(conn, timedelta(minutes=30), 10) == 0  # Cap reached.
        waits = conn.execute(
            "SELECT reconcile_count, next_reconcile_at - now() AS wait FROM call_sessions"
        ).fetchall()
    assert {r["reconcile_count"] for r in waits} == {MAX_RECONCILES}
    assert len(jobs(env["url"], "bolna.reconcile")) == 3 * MAX_RECONCILES


def test_recent_webhook_activity_defers_reconciliation(env):
    configure_agent(env["url"])
    add_session(env["url"], age=timedelta(hours=2))
    deliver(env, event("in-progress"))
    work(env["url"])
    with connect(env["url"]) as conn:
        assert enqueue_reconciles(conn, timedelta(minutes=30), 50) == 0


class TimingOutBolna(FakeBolna):
    def get_execution(self, execution_id):
        self.calls.append(("get_execution", (execution_id,)))
        raise ProviderTimeout()


def test_reconciliation_timeout_changes_nothing(env):
    configure_agent(env["url"])
    add_session(env["url"], age=timedelta(hours=2))
    with connect(env["url"]) as conn:
        enqueue_reconciles(conn, timedelta(minutes=30), 50)
    before = session(env["url"])
    fake = TimingOutBolna()
    work(env["url"], bolna=fake)
    [job] = jobs(env["url"], "bolna.reconcile")
    assert (job["status"], job["last_error"]) == ("pending", "timeout")
    assert session(env["url"]) == before
    assert len(fake.calls) == 1


def test_reconciliation_edge_cases(env):
    configure_agent(env["url"])
    add_session(env["url"], age=timedelta(hours=2))
    with connect(env["url"]) as conn:
        enqueue_reconciles(conn, timedelta(minutes=30), 50)
    work(env["url"], bolna=FakeBolna())  # Unknown at the provider: done, nothing changed.
    assert jobs(env["url"], "bolna.reconcile")[0]["status"] == "done"
    assert session(env["url"])["provider_status"] is None
    fake = FakeBolna()
    fake.executions[EXECUTION] = Execution(OTHER_EXECUTION, AGENT, "completed")
    with connect(env["url"]) as conn:
        conn.execute("UPDATE call_sessions SET next_reconcile_at = now() - interval '1 s'")
        enqueue_reconciles(conn, timedelta(minutes=30), 50)
    work(env["url"], bolna=fake)
    assert jobs(env["url"], "bolna.reconcile")[-1]["last_error"] == "execution_mismatch"
    assert session(env["url"])["provider_status"] is None


def test_housekeeping_enqueues_only_with_bolna_port(env):
    configure_agent(env["url"])
    add_session(env["url"], age=timedelta(hours=2))
    with connect(env["url"]) as conn:
        outbox._default_housekeeping(conn, Ports(calendar=FakeCalendar()))
        assert jobs(env["url"], "bolna.reconcile") == []
        outbox._default_housekeeping(conn, Ports(calendar=FakeCalendar(), bolna=FakeBolna()))
    assert len(jobs(env["url"], "bolna.reconcile")) == 1


def test_check_ports_and_cli_guard_bolna(env, monkeypatch, capsys):
    import httpx

    slow = BolnaClient("synthetic-key", httpx.Client(), timeout_seconds=60.0)
    with pytest.raises(ValueError, match="bolna timeout"):
        check_ports(Ports(calendar=FakeCalendar(), bolna=slow))
    check_ports(Ports(calendar=FakeCalendar(), bolna=BolnaClient("synthetic-key", httpx.Client())))
    check_ports(Ports(calendar=FakeCalendar(), bolna=FakeBolna()))
    settings = get_settings()
    with pytest.raises(SystemExit, match="Refusing --bolna fake"):
        cli.build_bolna("fake", "postgresql://localhost/clinic_voice", settings)
    assert isinstance(cli.build_bolna("fake", env["url"], settings), FakeBolna)
    monkeypatch.delenv("BOLNA_API_KEY", raising=False)
    get_settings.cache_clear()
    with pytest.raises(SystemExit, match="BOLNA_API_KEY"):
        cli.build_bolna("live", env["url"], get_settings())
    assert cli.build_bolna(None, env["url"], settings) is None


def test_cli_issues_webhook_credential_and_sets_agent(env, capsys):
    cli.main(
        ["credential", "issue", "--clinic", str(CLINIC), "--label", "hook", "--scope", "webhook"]
    )
    out = capsys.readouterr().out
    token = next(line[6:] for line in out.splitlines() if line.startswith("token="))
    assert token.startswith("cvw_") and f"webhook_path=/v1/webhooks/bolna/{token}" in out
    assert deliver(env, event("queued"), token=token).status_code == 200
    cli.main(["bolna", "set-agent", "--clinic", str(CLINIC), "--agent-id", AGENT])
    assert deliver(env, event("ringing", agent=OTHER_AGENT), token=token).status_code == 403
    cli.main(["bolna", "set-agent", "--clinic", str(CLINIC), "--clear"])
    assert deliver(env, event("ringing", agent=OTHER_AGENT), token=token).status_code == 200


def test_retention_purges_old_webhook_events(env):
    deliver(env, event("queued"))
    deliver(env, event("ringing"))
    with connect(env["url"]) as conn:
        conn.execute(
            "UPDATE webhook_inbox SET received_at = now() - interval '91 days'"
            " WHERE provider_status='queued'"
        )
        counts = retention.purge(conn, 90)
    assert counts["webhook_events"] == 1
    assert [r["provider_status"] for r in inbox(env["url"])] == ["ringing"]
    work(env["url"])  # The purged event's job completes without effect.
    assert {j["status"] for j in jobs(env["url"])} == {"done"}


# Logging


def test_tokens_never_logged(env, caplog):
    caplog.set_level(logging.DEBUG)
    add_session(env["url"])
    deliver(env, completed())
    deliver(env, completed())
    deliver(env, event("queued"), token=env["hook"][:-1] + "Z")
    work(env["url"])
    secret = env["hook"].split(".", 1)[1]
    # The test's own HTTP client (httpx2, used by TestClient) logs its request lines; the
    # application's records must not carry the token or any body content.
    text = "\n".join(
        r.getMessage() for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))
    )
    assert secret not in text and env["hook"] not in text
    assert MARKER not in text and CALLER not in text
    assert "outcome=applied" in text
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "POST", f"/v1/webhooks/bolna/{env['hook']}?x=1", "1.1", 200),
        None,
    )
    StripQuery().filter(record)
    line = record.getMessage()
    assert env["hook"] not in line and secret not in line
    assert "/v1/webhooks/bolna/<redacted>" in line and "x=1" not in line


# T7 review fold-in: requeue never meets a dead-lettering worker in reverse lock order


def test_requeue_refuses_non_dead_job_before_locking(env):
    deliver(env, event("queued"))
    [job] = jobs(env["url"])
    owner = staff_headers(env)
    r = env["client"].post(f"/v1/admin/outbox/{job['id']}/requeue", headers=owner)
    assert r.status_code == 409


def test_concurrent_requeue_and_dead_lettering(env, monkeypatch):
    from clinic_voice import service
    from clinic_voice.cli import DOCTOR_ID, SERVICE_ID
    from clinic_voice.schemas import ReservationRequest

    with connect(env["url"]) as conn:
        conn.execute(
            "UPDATE doctors SET booking_calendar_id='booking@example.test' WHERE id=%s",
            (DOCTOR_ID,),
        )
        start = conn.execute(
            "SELECT date_trunc('day', now() + interval '3 days') + interval '5 hours' AS s"
        ).fetchone()["s"]
    monkeypatch.setattr(service, "utc_now", lambda: start - timedelta(days=2))
    appointment_id = service.reserve(
        env["url"],
        CLINIC,
        ReservationRequest(doctor_id=DOCTOR_ID, service_id=SERVICE_ID, starts_at=start),
        f"key-{uuid4().hex}",
    )["appointment_id"]
    locked, release = Event(), Event()

    def blocking_on_dead(conn, job):
        locked.set()  # The outbox row is locked by this (uncommitted) dead-lettering.
        release.wait(10)
        needs_review_on_dead(conn, job)

    original = HANDLERS["calendar.create"]
    monkeypatch.setitem(
        HANDLERS, "calendar.create", Handler(original.run, on_dead=blocking_on_dead)
    )
    owner = staff_headers(env)
    with connect(env["url"]) as conn:
        [job] = claim(conn, "w1", kinds=["calendar.create"])

    def dead_letter():
        with connect(env["url"]) as conn:
            fail_dead(conn, job, "timeout")

    worker = Thread(target=dead_letter)
    worker.start()
    result = {}

    def requeue():
        result["response"] = env["client"].post(f"/v1/admin/outbox/{job.id}/requeue", headers=owner)

    staff_request = Thread(target=requeue)
    try:
        assert locked.wait(10)
        staff_request.start()
        staff_request.join(5)
        # Answered while the dead-lettering still holds the outbox row: no lock wait, and so
        # no deadlock (503) once the hook goes on to lock the appointment.
        assert not staff_request.is_alive()
        assert result["response"].status_code == 409
    finally:
        release.set()
        worker.join(10)
        staff_request.join(10)
    with connect(env["url"]) as conn:
        row = conn.execute(
            "SELECT status FROM appointments WHERE id=%s", (appointment_id,)
        ).fetchone()
    assert row["status"] == "needs_review"
    r = env["client"].post(f"/v1/admin/outbox/{job.id}/requeue", headers=owner)
    assert r.status_code == 200
    with connect(env["url"]) as conn:
        row = conn.execute(
            "SELECT status FROM appointments WHERE id=%s", (appointment_id,)
        ).fetchone()
    assert row["status"] == "pending_sync"


# Security review follow-ups


def test_second_forwarded_for_line_cannot_bypass_allowlist(env, monkeypatch):
    settings_env(monkeypatch, BOLNA_WEBHOOK_ALLOWED_IPS="13.203.39.153", TRUSTED_PROXY_HOPS="1")
    # The attacker sends an allowed address; the trusted proxy adds its own line with the
    # real peer. Only the first line used to be read.
    lines = [("X-Forwarded-For", "13.203.39.153"), ("X-Forwarded-For", "198.51.100.7")]
    r = env["client"].post(f"/v1/webhooks/bolna/{env['hook']}", json=event("queued"), headers=lines)
    assert r.status_code == 403
    assert nothing_stored(env["url"])
    lines = [("X-Forwarded-For", "198.51.100.7"), ("X-Forwarded-For", "13.203.39.153")]
    r = env["client"].post(f"/v1/webhooks/bolna/{env['hook']}", json=event("queued"), headers=lines)
    assert r.status_code == 200


def test_ipv4_mapped_addresses_match_ipv4_allowlist(env, monkeypatch):
    settings_env(monkeypatch, BOLNA_WEBHOOK_ALLOWED_IPS="13.203.39.153/32", TRUSTED_PROXY_HOPS="1")
    ok = deliver(env, event("queued"), headers={"X-Forwarded-For": "::ffff:13.203.39.153"})
    assert ok.status_code == 200
    other = deliver(env, event("ringing"), headers={"X-Forwarded-For": "::ffff:13.203.39.154"})
    assert other.status_code == 403


def access_line(path):
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "POST", path, "1.1", 404),
        None,
    )
    StripQuery().filter(record)
    return record.getMessage()


@pytest.mark.parametrize(
    "template",
    [
        "/v1/webhooks/bolna/{t}",
        "/clinic-api/v1/webhooks/bolna/{t}",  # root_path prefix
        "//v1/webhooks/bolna/{t}",
        "/v1//webhooks/bolna/{t}",
        "/v1/webhooks//bolna//{t}?q=1",
        "/v1/webhooks/other/{t}/extra",
        "/v1/unknown/{t}",  # 404 path carrying a token
        "/{t}",
    ],
)
def test_access_log_redacts_tokens_anywhere(env, template):
    secret = env["hook"].split(".", 1)[1]
    for token in (env["hook"], env["tool"], "cvs_" + "a" * 32 + "." + "b" * 43):
        line = access_line(template.format(t=token))
        assert token not in line and token.split(".", 1)[1] not in line
    assert secret not in access_line("/v1/webhooks/bolna/not-a-token-but-secret-" + secret)


def test_unrouted_paths_with_token_store_nothing(env):
    for path in (f"//v1/webhooks/bolna/{env['hook']}", f"/v1/webhooks/bolna/{env['hook']}/x"):
        assert env["client"].post(path, json=event("queued")).status_code in (404, 405)
    assert nothing_stored(env["url"])


OTHER_CLINIC_EXECUTION = "5f0c6f7e-1d1a-4c55-9a43-0b6f1b2a0199"


def provider_view(row):
    """A session without the reconciliation bookkeeping (which a poll legitimately bumps)."""
    return {k: v for k, v in row.items() if k not in ("reconcile_count", "next_reconcile_at")}


def test_reconcile_cannot_pull_another_clinics_execution(env):
    configure_agent(env["url"])
    configure_agent(env["url"], clinic=OTHER_CLINIC, agent=OTHER_AGENT)
    # Clinic A's tool caller asserted clinic B's execution id for its session.
    add_session(env["url"], execution=OTHER_CLINIC_EXECUTION, age=timedelta(hours=2))
    add_session(
        env["url"],
        clinic=OTHER_CLINIC,
        execution=OTHER_CLINIC_EXECUTION,
        agent=OTHER_AGENT,
        age=timedelta(minutes=1),
    )
    fake = FakeBolna()
    fake.executions[OTHER_CLINIC_EXECUTION] = Execution(
        OTHER_CLINIC_EXECUTION, OTHER_AGENT, "completed", "call_ended", "inbound", 99.0
    )
    with connect(env["url"]) as conn:
        assert enqueue_reconciles(conn, timedelta(minutes=30), 50) == 1
    before = session(env["url"], execution=OTHER_CLINIC_EXECUTION)
    assert work(env["url"], bolna=fake) == 1
    [job] = jobs(env["url"], "bolna.reconcile")
    assert job["status"] == "done"
    after = session(env["url"], execution=OTHER_CLINIC_EXECUTION)
    # Nothing of clinic B's call was written into clinic A.
    assert provider_view(after) == provider_view(before)
    theirs = session(env["url"], clinic=OTHER_CLINIC, execution=OTHER_CLINIC_EXECUTION)
    assert theirs["provider_status"] is None
    # An execution without an agent id is refused the same way.
    fake.executions[OTHER_CLINIC_EXECUTION] = Execution(OTHER_CLINIC_EXECUTION, None, "completed")
    with connect(env["url"]) as conn:
        conn.execute("UPDATE call_sessions SET next_reconcile_at = now() - interval '1 s'")
        assert enqueue_reconciles(conn, timedelta(minutes=30), 50) == 1
    work(env["url"], bolna=fake)
    after = session(env["url"], execution=OTHER_CLINIC_EXECUTION)
    assert provider_view(after) == provider_view(before)


def test_clinic_without_agent_id_is_never_reconciled(env):
    add_session(env["url"], age=timedelta(hours=2))
    with connect(env["url"]) as conn:
        assert enqueue_reconciles(conn, timedelta(minutes=30), 50) == 0
    # A job that already exists (e.g. the agent id was cleared) does not ask the provider.
    session_id = session(env["url"])["id"]
    with connect(env["url"]) as conn:
        conn.execute(
            "INSERT INTO outbox (id,clinic_id,kind,payload) VALUES (%s,%s,'bolna.reconcile',%s)",
            (uuid4(), CLINIC, json.dumps({"session_id": str(session_id)})),
        )
    fake = FakeBolna()
    work(env["url"], bolna=fake)
    assert fake.calls == []
    assert jobs(env["url"], "bolna.reconcile")[0]["status"] == "done"


def test_worker_without_bolna_does_not_claim_reconcile_jobs(env):
    configure_agent(env["url"])
    add_session(env["url"], age=timedelta(hours=2))
    with connect(env["url"]) as conn:
        enqueue_reconciles(conn, timedelta(minutes=30), 50)
    deliver(env, event("queued"))
    assert work(env["url"]) == 1  # Only the webhook job.
    [job] = jobs(env["url"], "bolna.reconcile")
    assert (job["status"], job["attempts"], job["last_error"]) == ("pending", 0, None)
    with connect(env["url"]) as conn:  # An expired lease is not dead-lettered there either.
        conn.execute(
            "UPDATE outbox SET status='processing', lease_owner='gone', attempts=max_attempts,"
            " lease_expires_at=now() - interval '1 s' WHERE id=%s",
            (job["id"],),
        )
    assert work(env["url"]) == 0
    assert jobs(env["url"], "bolna.reconcile")[0]["status"] == "processing"


def test_equal_rank_events_apply_in_provider_time_order(env):
    add_session(env["url"])
    newer = event("in-progress", updated_at="2031-03-03T03:02:00+00:00", conversation_duration=20)
    older = event("in-progress", updated_at="2031-03-03T03:01:00+00:00", conversation_duration=10)
    same = event(
        "in-progress",
        updated_at="2031-03-03T03:02:00+00:00",
        conversation_duration=20,
        total_cost=3,
    )
    undated = event("in-progress", conversation_duration=30)
    del undated["updated_at"]
    for body in (newer, older, same, undated):
        deliver(env, body)
        work(env["url"])
    assert [r["outcome"] for r in inbox(env["url"])] == ["applied", "stale", "applied", "stale"]
    row = session(env["url"])
    assert (row["conversation_seconds"], row["total_cost"]) == (20.0, 3.0)
    assert row["provider_event_at"].isoformat() == "2031-03-03T03:02:00+00:00"
    # Lateral moves follow provider time as well; a higher rank still wins regardless.
    deliver(env, event("call-disconnected", updated_at="2031-03-03T03:00:00+00:00"))
    work(env["url"])
    assert session(env["url"])["provider_status"] == "call-disconnected"


def test_bolna_client_enforces_total_deadline():
    import httpx

    now = [0.0]

    def clock():
        return now[0]

    def slow_body():
        for _ in range(5):
            now[0] += 4.0  # Each chunk arrives within the per-read timeout.
            yield b" "
        yield b'{"id": "33333333-3333-4333-8333-333333333333", "status": "completed"}'

    def handler(request):
        return httpx.Response(200, content=slow_body())

    client = BolnaClient(
        "synthetic-key",
        httpx.Client(transport=httpx.MockTransport(handler)),
        timeout_seconds=10.0,
        clock=clock,
    )
    from clinic_voice.providers.calendar import ProviderRetryable

    with pytest.raises(ProviderRetryable) as caught:
        client.get_execution("33333333-3333-4333-8333-333333333333")
    assert caught.value.code == "deadline"
    with pytest.raises(ProviderTimeout) as caught:  # A write's outcome is unknown.
        now[0] = 0.0
        client.update_agent("33333333-3333-4333-8333-333333333333", {})
    assert caught.value.code == "deadline"
    now[0] = 0.0
    fast = BolnaClient(
        "synthetic-key",
        httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    json={"id": "33333333-3333-4333-8333-333333333333", "status": "queued"},
                )
            )
        ),
        clock=clock,
    )
    assert fast.get_execution("33333333-3333-4333-8333-333333333333").status == "queued"
    huge = BolnaClient(
        "synthetic-key",
        httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b" " * 2_000_000))
        ),
    )
    with pytest.raises(ProviderRetryable) as caught:
        huge.get_execution("33333333-3333-4333-8333-333333333333")
    assert caught.value.code == "too_large"
