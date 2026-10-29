"""Booking confirmations, reminder calls and messages, opt-outs (T9).

Synthetic data only: "Synthetic Patient" names, +9100000xxxxx numbers, generated keys and
credentials. FakeCalendar, FakeBolna and FakeMessaging stand in for every provider; nothing
reaches the network. The application clock (service.utc_now) is pinned in 2031; jobs are made
due with the database clock (available_at) and the handlers then decide on the pinned clock.
"""

import base64
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, time, timedelta
from threading import Barrier
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import psycopg
import pytest
from fastapi.testclient import TestClient

from clinic_voice import cli, credentials, reminders, retention, service, staff
from clinic_voice.api import app
from clinic_voice.bolna import bookings, gateway
from clinic_voice.bolna.webhooks import disposition_opts_out
from clinic_voice.cli import DEMO_CLINIC_ID as CLINIC
from clinic_voice.config import get_settings
from clinic_voice.db import connect
from clinic_voice.outbox import Ports, check_ports, excluded_kinds, run_once
from clinic_voice.providers.bolna import Execution, FakeBolna
from clinic_voice.providers.calendar import (
    FakeCalendar,
    ProviderPermanent,
    ProviderRetryable,
    ProviderTimeout,
)
from clinic_voice.providers.messaging import FakeMessaging

IST = ZoneInfo("Asia/Kolkata")
NY = ZoneInfo("America/New_York")
NOW = datetime(2031, 3, 3, 8, 30, tzinfo=IST)  # Inside the default quiet hours.
DAY = NOW.date() + timedelta(days=2)
SLOT = datetime.combine(DAY, time(10, 0), IST)
REMINDER_AT = SLOT - timedelta(hours=24)
KEYS = "k1:" + base64.b64encode(b"\x02" * 32).decode()
HASH_KEY = base64.b64encode(b"\x03" * 32).decode()
CALLER = "+910000000123"
OTHER_CALLER = "+910000000456"
NAME = "Synthetic Patient Alpha"
OTHER_NAME = "Synthetic Patient Gamma"
AGENT = "11111111-1111-4111-8111-111111111111"
OUTBOUND = "22222222-2222-4222-8222-222222222222"
OTHER_CLINIC = UUID("00000000-0000-4000-8000-0000000000c1")
PASSWORD = "synthetic-password-01"
CAL = "booking@example.test"
RESERVE = "/v1/bolna/tools/reserve-slot"
VERIFY = "/v1/bolna/tools/verify-booking"
CANCEL = "/v1/bolna/tools/cancel-booking"
RESCHEDULE = "/v1/bolna/tools/reschedule-booking"
OPT_OUT = "/v1/bolna/tools/opt-out"
ALL_CONSENTS = {
    "consent_booking_confirmation": "true",
    "consent_reminder_call": "true",
    "consent_reminder_message": "true",
}


@pytest.fixture
def env(database, monkeypatch):
    clock = {"now": NOW}
    monkeypatch.setattr(service, "utc_now", lambda: clock["now"].astimezone(UTC))
    monkeypatch.setattr(staff, "SCRYPT_N", 2**10)
    monkeypatch.setenv("DATABASE_URL", database)
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEYS", KEYS)
    monkeypatch.setenv("PHONE_HASH_KEY", HASH_KEY)
    get_settings.cache_clear()
    with connect(database) as conn:
        conn.execute("INSERT INTO clinics (id,name) VALUES (%s,'Other Synthetic')", (OTHER_CLINIC,))
        conn.execute(
            "UPDATE clinics SET outbound_enabled=true, bolna_agent_id=%s,"
            " bolna_outbound_agent_id=%s WHERE id=%s",
            (AGENT, OUTBOUND, CLINIC),
        )
        conn.execute("UPDATE doctors SET booking_calendar_id=%s WHERE clinic_id=%s", (CAL, CLINIC))
        _, token = credentials.issue(conn, CLINIC, "t9")
        _, other = credentials.issue(conn, OTHER_CLINIC, "t9-other")
    ports = Ports(calendar=FakeCalendar(), bolna=FakeBolna(), messaging=FakeMessaging())
    with TestClient(app) as client:
        yield {
            "client": client,
            "url": database,
            "token": token,
            "other": other,
            "clock": clock,
            "ports": ports,
        }
    get_settings.cache_clear()


def at(env, moment):
    env["clock"]["now"] = moment


def post(env, path, payload, token=None):
    return env["client"].post(
        path, json=payload, headers={"Authorization": f"Bearer {token or env['token']}"}
    )


def body(execution_id, number=CALLER, **extra):
    return {"execution_id": execution_id, "from_number": number, "agent_id": AGENT, **extra}


def book(env, consents=None, name=NAME, number=CALLER, starts_at=SLOT, execution_id=None):
    execution_id = execution_id or str(uuid4())
    r = post(
        env,
        RESERVE,
        body(
            execution_id,
            number,
            doctor="Demo Doctor",
            service="Consultation",
            starts_at=starts_at.isoformat(),
            patient_name=name,
            **(ALL_CONSENTS if consents is None else consents),
        ),
    )
    assert r.status_code == 202, r.text
    return {**r.json(), "execution_id": execution_id}


def rows(url, sql, params=()):
    with connect(url) as conn:
        return conn.execute(sql, params).fetchall()


def execute(url, sql, params=()):
    with connect(url) as conn:
        conn.execute(sql, params)


def contacts(env, appointment_id=None):
    return rows(
        env["url"],
        "SELECT * FROM reminders WHERE (%s::uuid IS NULL OR appointment_id=%s::uuid)"
        " ORDER BY purpose, channel, created_at",
        (appointment_id, appointment_id),
    )


def contact(env, appointment_id, purpose, channel):
    found = [
        r
        for r in contacts(env, appointment_id)
        if r["purpose"] == purpose and r["channel"] == channel
    ]
    assert len(found) <= 1
    return found[0] if found else None


def work(env, ports=None, kinds=None, due=True):
    """Make every pending job due (database clock) once, then process until nothing is
    claimable. Jobs a handler defers move to a later available_at and are not re-run here."""
    ports = ports or env["ports"]
    if due:
        execute(
            env["url"],
            "UPDATE outbox SET available_at = now() - interval '1 second' WHERE status='pending'",
        )
    total = 0
    while count := run_once(env["url"], "w1", ports, kinds=kinds):
        total += count
    return total


def confirm(env):
    """Run the calendar.create jobs (already due) in booking order."""
    return work(env, kinds=["calendar.create"], due=False)


def calls(env, method="start_outbound_call"):
    return [args for name, args in env["ports"].bolna.calls if name == method]


def staff_headers(env, role="owner", clinic=CLINIC):
    email = f"{role}-{uuid4().hex[:8]}@example.test"
    with connect(env["url"]) as conn:
        staff.create_user(conn, clinic, email, role, PASSWORD)
    r = env["client"].post("/v1/staff/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200
    return {"Authorization": "Bearer " + r.json()["token"]}


def jobs(env, kind):
    return rows(env["url"], "SELECT * FROM outbox WHERE kind=%s ORDER BY created_at", (kind,))


# Quiet hours (pure)


NINE, EVENING = time(9), time(21)


def test_quiet_hours_kolkata():
    def shift(local):
        return reminders.next_allowed(local, IST, EVENING, NINE).astimezone(IST)

    day = datetime(2031, 3, 3, tzinfo=IST)
    assert shift(day.replace(hour=22, minute=30)) == datetime(2031, 3, 4, 9, 0, tzinfo=IST)
    assert shift(day.replace(hour=8, minute=59)) == day.replace(hour=9)
    assert shift(day.replace(hour=21)) == datetime(2031, 3, 4, 9, 0, tzinfo=IST)  # [start, end)
    assert shift(day.replace(hour=9)) == day.replace(hour=9)
    assert shift(day.replace(hour=12, minute=5)) == day.replace(hour=12, minute=5)
    # A window that does not wrap midnight.
    lunch = reminders.next_allowed(day.replace(hour=13, minute=20), IST, time(13), time(14))
    assert lunch == day.replace(hour=14)
    assert reminders.in_quiet(day.replace(hour=13), IST, time(13), time(14))
    assert not reminders.in_quiet(day.replace(hour=14), IST, time(13), time(14))


def test_quiet_hours_across_dst():
    # 2031-03-09: New York springs forward at 02:00. 22:00 EST to 09:00 EDT is 10 hours.
    evening = datetime(2031, 3, 8, 22, 0, tzinfo=NY)
    shifted = reminders.next_allowed(evening, NY, EVENING, NINE)
    assert shifted == datetime(2031, 3, 9, 13, 0, tzinfo=UTC)
    assert shifted.astimezone(NY).hour == 9 and shifted - evening == timedelta(hours=10)
    # 2031-11-02: falls back at 02:00. 22:00 EDT to 09:00 EST is 12 hours.
    autumn = datetime(2031, 11, 1, 22, 0, tzinfo=NY)
    shifted = reminders.next_allowed(autumn, NY, EVENING, NINE)
    assert shifted == datetime(2031, 11, 2, 14, 0, tzinfo=UTC)
    assert shifted - autumn == timedelta(hours=12)
    # A quiet end inside the spring gap (02:30 does not exist): after the transition.
    gap = reminders.next_allowed(datetime(2031, 3, 8, 23, 0, tzinfo=NY), NY, EVENING, time(2, 30))
    assert gap >= datetime(2031, 3, 9, 7, 0, tzinfo=UTC)
    assert not reminders.in_quiet(gap, NY, EVENING, time(2, 30))
    assert gap - datetime(2031, 3, 9, 7, 0, tzinfo=UTC) <= timedelta(hours=1)
    # An ambiguous quiet end (01:30 happens twice): the first occurrence.
    first = reminders.next_allowed(
        datetime(2031, 11, 1, 23, 0, tzinfo=NY), NY, EVENING, time(1, 30)
    )
    assert first == datetime(2031, 11, 2, 5, 30, tzinfo=UTC)
    # The local day boundary used by the budget follows the zone too.
    assert reminders.start_of_next_day(datetime(2031, 3, 8, 23, 0, tzinfo=NY), NY) == datetime(
        2031, 3, 9, 5, 0, tzinfo=UTC
    )


# Planning and sending


def test_no_consent_no_contact(env):
    made = book(env, consents={})
    confirm(env)
    assert rows(env["url"], "SELECT status FROM appointments")[0]["status"] == "confirmed"
    # Each refused contact is recorded as suppressed (so it is never planned again) with no job.
    assert {(r["purpose"], r["channel"], r["status"], r["last_error"]) for r in contacts(env)} == {
        ("booking_confirmation", "message", "suppressed", "no_consent"),
        ("reminder", "call", "suppressed", "no_consent"),
        ("reminder", "message", "suppressed", "no_consent"),
    }
    assert jobs(env, "notification.send") == [] and jobs(env, "reminder.call") == []
    for moment in (NOW.replace(hour=9, minute=0), REMINDER_AT):
        at(env, moment)
        work(env)
        with connect(env["url"]) as conn:
            assert reminders.schedule_due(conn) == 0
    assert env["ports"].messaging.sent == [] and calls(env) == []
    # A declined consent is the same as none.
    declined = {key: "false" for key in ALL_CONSENTS}
    other = book(env, consents=declined, starts_at=SLOT + timedelta(hours=1), name=OTHER_NAME)
    confirm(env)
    assert {r["status"] for r in contacts(env, other["appointment_id"])} == {"suppressed"}
    assert jobs(env, "notification.send") == [] and made["status"] == "pending_sync"


def test_outbound_disabled_no_contact(env):
    execute(env["url"], "UPDATE clinics SET outbound_enabled=false WHERE id=%s", (CLINIC,))
    made = book(env)
    confirm(env)
    assert contacts(env) == []
    at(env, REMINDER_AT)
    with connect(env["url"]) as conn:
        assert reminders.schedule_due(conn) == 0
    work(env)
    assert env["ports"].messaging.sent == [] and calls(env) == []
    # Enabled when planned, disabled before the send: suppressed, nothing sent.
    at(env, NOW)
    execute(env["url"], "UPDATE clinics SET outbound_enabled=true WHERE id=%s", (CLINIC,))
    later = book(env, starts_at=SLOT + timedelta(hours=2))
    confirm(env)
    assert len(contacts(env, later["appointment_id"])) == 3
    execute(env["url"], "UPDATE clinics SET outbound_enabled=false WHERE id=%s", (CLINIC,))
    at(env, NOW.replace(hour=9, minute=0))
    work(env)
    row = contact(env, later["appointment_id"], "booking_confirmation", "message")
    assert (row["status"], row["last_error"]) == ("suppressed", "outbound_disabled")
    assert env["ports"].messaging.sent == []
    assert contacts(env, made["appointment_id"]) == []


def test_confirmation_and_reminders_are_sent_once(env, caplog):
    caplog.set_level(logging.DEBUG)
    made = book(env)
    appointment_id = made["appointment_id"]
    confirm(env)
    confirmation = contact(env, appointment_id, "booking_confirmation", "message")
    call = contact(env, appointment_id, "reminder", "call")
    message = contact(env, appointment_id, "reminder", "message")
    # Confirmed at 08:30 IST, inside quiet hours: the confirmation waits until 09:00.
    assert confirmation["scheduled_for"] == NOW.replace(hour=9, minute=0)
    assert call["scheduled_for"] == message["scheduled_for"] == REMINDER_AT
    assert {r["status"] for r in contacts(env)} == {"scheduled"}
    [job] = jobs(env, "reminder.call")
    assert job["payload"] == {"reminder_id": str(call["id"])}
    assert job["appointment_version"] == 1
    # Before 09:00 nothing goes out; claimed early, the jobs wait for their time.
    at(env, NOW.replace(hour=8, minute=45))
    work(env)
    assert env["ports"].messaging.sent == [] and calls(env) == []
    assert (
        rows(env["url"], "SELECT available_at FROM outbox WHERE id=%s", (job["id"],))[0][
            "available_at"
        ]
        == REMINDER_AT
    )
    at(env, NOW.replace(hour=9, minute=0))
    work(env)
    [(to, text, reference)] = env["ports"].messaging.sent
    assert to == CALLER and reference == confirmation["provider_reference"]
    assert "Synthetic Demo Clinic" in text and made["booking_reference"] in text
    assert "is confirmed" in text and "Wednesday 5 March at 10:00 AM" in text
    assert NAME not in text and "Consultation" not in text and "Demo Doctor" not in text
    sent = contact(env, appointment_id, "booking_confirmation", "message")
    assert sent["status"] == "sent" and sent["provider_message_id"] and sent["attempts"] == 1
    assert calls(env) == []
    at(env, REMINDER_AT + timedelta(minutes=1))
    work(env)
    [(agent, recipient, origin, scheduled, user_data)] = calls(env)
    # The separate outbound agent, never the inbound agent that carries the booking tools.
    assert (agent, recipient, origin, scheduled) == (OUTBOUND, CALLER, None, None)
    # No booking reference: it is what verifies a caller, and the answerer is unverified.
    assert "booking_reference" not in user_data
    assert made["booking_reference"] not in str(user_data)
    assert user_data["contact_reference"] == call["provider_reference"]
    assert NAME not in str(user_data)
    placed = contact(env, appointment_id, "reminder", "call")
    assert (
        placed["status"] == "sent"
        and placed["provider_execution_id"] in env["ports"].bolna.executions
    )
    assert len(env["ports"].messaging.sent) == 2
    assert "reminder of your appointment" in env["ports"].messaging.sent[1][1]
    # Nothing is ever sent twice, however often the worker runs or housekeeping backfills.
    with connect(env["url"]) as conn:
        assert reminders.schedule_due(conn) == 0
    work(env)
    assert len(env["ports"].messaging.sent) == 2 and len(calls(env)) == 1
    assert_no_phi(env, caplog, [text, env["ports"].messaging.sent[1][1]])


def assert_no_phi(env, caplog, bodies):
    digits = CALLER.lstrip("+")
    stored = [
        str(row)
        for sql in (
            "SELECT payload::text AS t, last_error FROM outbox",
            "SELECT action, target_id FROM audit_events",
            "SELECT r::text AS t FROM reminders r",
            "SELECT p::text AS t FROM contact_preferences p",
        )
        for row in rows(env["url"], sql)
    ]
    texts = stored + [record.getMessage() for record in caplog.records]
    for text in texts:
        assert NAME not in text and digits not in text and CALLER not in text
        for message in bodies:
            assert message not in text
        assert "is confirmed." not in text and "reminder of your appointment" not in text


def test_quiet_hours_changed_after_planning_shift_at_send(env):
    made = book(env, consents={"consent_booking_confirmation": "true"})
    confirm(env)
    execute(env["url"], "UPDATE clinics SET quiet_hours_end='10:00' WHERE id=%s", (CLINIC,))
    at(env, NOW.replace(hour=9, minute=5))
    work(env)
    row = contact(env, made["appointment_id"], "booking_confirmation", "message")
    assert row["status"] == "scheduled" and row["scheduled_for"] == NOW.replace(hour=10, minute=0)
    [job] = jobs(env, "notification.send")
    assert job["last_error"] == "quiet_hours" and job["available_at"] == NOW.replace(
        hour=10, minute=0
    )
    assert job["attempts"] < job["max_attempts"] - 1  # Deferral spends no attempt.
    assert env["ports"].messaging.sent == []
    at(env, NOW.replace(hour=10, minute=0))
    work(env)
    assert len(env["ports"].messaging.sent) == 1


def test_quiet_hours_never_shift_past_the_appointment(env):
    execute(env["url"], "UPDATE clinics SET reminder_lead_minutes=60 WHERE id=%s", (CLINIC,))
    early = datetime.combine(DAY, time(9, 15), IST)
    made = book(env, consents={"consent_reminder_call": "true"}, starts_at=early)
    confirm(env)
    row = contact(env, made["appointment_id"], "reminder", "call")
    # Due 08:15 (quiet); the next allowed time, 09:00, is less than 30 minutes before 09:15.
    assert (row["status"], row["last_error"]) == ("suppressed", "quiet_hours")
    assert jobs(env, "reminder.call") == []
    at(env, early - timedelta(minutes=50))
    work(env)
    assert calls(env) == []


def test_lead_time_passed_plans_no_reminder(env):
    soon = datetime.combine(NOW.date(), time(16, 0), IST)
    made = book(env, starts_at=soon)
    confirm(env)
    kinds = {(r["purpose"], r["channel"]) for r in contacts(env, made["appointment_id"])}
    assert kinds == {("booking_confirmation", "message")}


def test_daily_budget_postpones_then_suppresses(env):
    execute(env["url"], "UPDATE clinics SET daily_contact_budget=1 WHERE id=%s", (CLINIC,))
    only_call = {"consent_reminder_call": "true"}
    first = book(env, consents=only_call)
    second = book(env, consents=only_call, starts_at=SLOT + timedelta(hours=1), name=OTHER_NAME)
    third = book(
        env,
        consents=only_call,
        starts_at=datetime.combine(DAY, time(9, 15), IST),
        number=OTHER_CALLER,
    )
    confirm(env)  # Planned in booking order: a, b, c.
    a = contact(env, first["appointment_id"], "reminder", "call")
    b = contact(env, second["appointment_id"], "reminder", "call")
    c = contact(env, third["appointment_id"], "reminder", "call")
    eve = DAY - timedelta(days=1)
    # a takes the day before's one place at its due time.
    assert a["status"] == "scheduled" and a["scheduled_for"] == datetime.combine(eve, time(10), IST)
    # b does not fit that day: it moves to 09:00 on the appointment day (10:30 is its limit).
    assert b["status"] == "scheduled" and b["scheduled_for"] == datetime.combine(DAY, time(9), IST)
    assert b["contact_day"] == DAY
    # c (09:15) finds no day with room before its window closes: suppressed, not dropped.
    assert (c["status"], c["last_error"]) == ("suppressed", "budget_exhausted")
    assert len(jobs(env, "reminder.call")) == 2
    # Send time enforces the budget again: lowered to 0, a is postponed, then suppressed.
    execute(env["url"], "UPDATE clinics SET daily_contact_budget=0 WHERE id=%s", (CLINIC,))
    at(env, datetime.combine(eve, time(10), IST))
    work(env)
    a = contact(env, first["appointment_id"], "reminder", "call")
    assert a["status"] == "scheduled" and a["scheduled_for"] == datetime.combine(DAY, time(9), IST)
    at(env, datetime.combine(DAY, time(9), IST))
    work(env)
    assert calls(env) == []
    for made in (first, second):
        row = contact(env, made["appointment_id"], "reminder", "call")
        assert (row["status"], row["last_error"]) == ("suppressed", "budget_exhausted")


def test_send_time_budget_counts_attempted_contacts(env):
    execute(env["url"], "UPDATE clinics SET daily_contact_budget=5 WHERE id=%s", (CLINIC,))
    only_message = {"consent_booking_confirmation": "true"}
    first = book(env, consents=only_message)
    second = book(env, consents=only_message, starts_at=SLOT + timedelta(hours=1), name=OTHER_NAME)
    confirm(env)
    execute(env["url"], "UPDATE clinics SET daily_contact_budget=1 WHERE id=%s", (CLINIC,))
    at(env, NOW.replace(hour=9, minute=0))
    work(env)
    assert len(env["ports"].messaging.sent) == 1
    statuses = sorted(
        (r["status"], r["scheduled_for"])
        for r in contacts(env)
        if r["appointment_id"] in (UUID(first["appointment_id"]), UUID(second["appointment_id"]))
        and r["purpose"] == "booking_confirmation"
    )
    # The second confirmation waits for tomorrow's budget (09:00 IST), still before the slot.
    assert statuses == [
        ("scheduled", datetime.combine(NOW.date() + timedelta(days=1), time(9), IST)),
        ("sent", NOW.replace(hour=9, minute=0)),
    ]


def test_stale_version_is_a_no_op(env):
    made = book(env)
    confirm(env)
    # The planned version no longer exists (as after any change); the contact must not run.
    execute(
        env["url"],
        "UPDATE appointments SET version=version + 1 WHERE id=%s",
        (made["appointment_id"],),
    )
    at(env, NOW.replace(hour=9, minute=0))
    work(env)
    at(env, REMINDER_AT)
    work(env)
    assert env["ports"].messaging.sent == [] and calls(env) == []
    assert {(r["status"], r["last_error"]) for r in contacts(env)} == {("cancelled", "stale")}


def test_cancellation_stops_the_placed_call(env):
    made = book(env)
    appointment_id = made["appointment_id"]
    confirm(env)
    at(env, REMINDER_AT)
    work(env)
    placed = contact(env, appointment_id, "reminder", "call")
    execution_id = placed["provider_execution_id"]
    assert env["ports"].bolna.executions[execution_id].status == "queued"
    # A reminder message still unsent is cancelled in the cancelling transaction itself.
    execute(
        env["url"],
        "UPDATE reminders SET status='scheduled', sent_at=NULL"
        " WHERE appointment_id=%s AND channel='message' AND purpose='reminder'",
        (appointment_id,),
    )
    headers = staff_headers(env)
    r = env["client"].post(f"/v1/admin/appointments/{appointment_id}/cancel", headers=headers)
    assert r.status_code == 200, r.text
    message = contact(env, appointment_id, "reminder", "message")
    assert (message["status"], message["last_error"]) == ("cancelled", "appointment_changed")
    [stop_job] = jobs(env, "reminder.stop")
    assert stop_job["appointment_version"] == 1 and stop_job["payload"] == {}
    work(env)
    assert calls(env, "stop_scheduled_call") == [(execution_id,)]
    assert env["ports"].bolna.executions[execution_id].status == "stopped"
    stopped = contact(env, appointment_id, "reminder", "call")
    assert (stopped["status"], stopped["stop_outcome"]) == ("sent", "stopped")
    work(env)
    assert len(calls(env, "stop_scheduled_call")) == 1
    assert (
        rows(env["url"], "SELECT status FROM outbox WHERE id=%s", (stop_job["id"],))[0]["status"]
        == "done"
    )


def test_stop_of_a_call_that_already_rang_is_recorded(env):
    made = book(env, consents={"consent_reminder_call": "true"})
    confirm(env)
    at(env, REMINDER_AT)
    work(env)
    placed = contact(env, made["appointment_id"], "reminder", "call")
    execution_id = placed["provider_execution_id"]
    env["ports"].bolna.executions[execution_id] = Execution(execution_id, AGENT, "completed")
    headers = staff_headers(env)
    env["client"].post(f"/v1/admin/appointments/{made['appointment_id']}/cancel", headers=headers)
    env["ports"].bolna.fail_next("stop_scheduled_call", 1, ProviderTimeout)
    work(env)  # Timeout: retried later, nothing recorded.
    assert contact(env, made["appointment_id"], "reminder", "call")["stop_outcome"] is None
    work(env)
    assert (
        contact(env, made["appointment_id"], "reminder", "call")["stop_outcome"] == "not_stoppable"
    )


def verify(env, execution_id, reference, number=CALLER, token=None):
    return post(env, VERIFY, body(execution_id, number, booking_reference=reference), token)


def test_reschedule_creates_new_reminders_and_stops_old(env):
    made = book(env)
    old_id = made["appointment_id"]
    confirm(env)
    assert len(contacts(env, old_id)) == 3
    session = str(uuid4())
    assert verify(env, session, made["booking_reference"]).status_code == 200
    moved = SLOT + timedelta(days=1)
    r = post(
        env,
        RESCHEDULE,
        body(
            session,
            booking_reference=made["booking_reference"],
            starts_at=moved.isoformat(),
            confirmed="true",
        ),
    )
    assert r.status_code == 200, r.text
    assert {(c["status"], c["last_error"]) for c in contacts(env, old_id)} == {
        ("cancelled", "appointment_changed")
    }
    assert len(jobs(env, "reminder.stop")) == 1
    [new] = rows(
        env["url"], "SELECT id, version FROM appointments WHERE rescheduled_from=%s", (old_id,)
    )
    confirm(env)
    fresh = contacts(env, new["id"])
    assert {(c["purpose"], c["channel"], c["status"]) for c in fresh} == {
        ("booking_confirmation", "message", "scheduled"),
        ("reminder", "call", "scheduled"),
        ("reminder", "message", "scheduled"),
    }
    assert contact(env, new["id"], "reminder", "call")["scheduled_for"] == moved - timedelta(
        hours=24
    )
    at(env, REMINDER_AT)
    work(env)  # The old version's reminder time: nothing for the cancelled booking.
    assert calls(env) == []
    at(env, moved - timedelta(hours=24))
    work(env)
    assert len(calls(env)) == 1
    assert r.json()["booking_reference"] not in str(calls(env)[0][4])


# Ambiguous timeouts


def test_message_timeout_after_send_reconciles_and_sends_once(env):
    made = book(env, consents={"consent_booking_confirmation": "true"})
    confirm(env)
    messaging = env["ports"].messaging
    messaging.timeout_after_send = True
    at(env, NOW.replace(hour=9, minute=0))
    work(env)
    row = contact(env, made["appointment_id"], "booking_confirmation", "message")
    assert (row["status"], row["last_error"]) == ("unknown", "timeout")
    [job] = jobs(env, "notification.send")
    assert (job["status"], job["last_error"]) == ("pending", "timeout")
    work(env)
    assert messaging.count(row["provider_reference"]) == 1 and messaging.lookups == 1
    row = contact(env, made["appointment_id"], "booking_confirmation", "message")
    assert (
        row["status"] == "sent"
        and row["provider_message_id"] == messaging.by_reference[row["provider_reference"]]
    )
    work(env)
    assert len(messaging.sent) == 1


def test_message_timeout_before_send_is_sent_once_after_lookup(env):
    made = book(env, consents={"consent_booking_confirmation": "true"})
    confirm(env)
    messaging = env["ports"].messaging
    messaging.timeout_before_send = True
    at(env, NOW.replace(hour=9, minute=0))
    work(env)
    assert messaging.sent == []
    work(env)
    row = contact(env, made["appointment_id"], "booking_confirmation", "message")
    assert row["status"] == "sent" and row["attempts"] == 2
    assert messaging.count(row["provider_reference"]) == 1 and messaging.lookups == 1


def test_lookup_failure_never_resends_blindly(env):
    made = book(env, consents={"consent_booking_confirmation": "true"})
    confirm(env)
    messaging = env["ports"].messaging
    messaging.timeout_after_send = True
    at(env, NOW.replace(hour=9, minute=0))
    work(env)
    messaging.fail_next(1, ProviderRetryable)  # The lookup itself fails: still unknown.
    work(env)
    row = contact(env, made["appointment_id"], "booking_confirmation", "message")
    assert row["status"] == "unknown" and len(messaging.sent) == 1
    work(env)
    assert (
        contact(env, made["appointment_id"], "booking_confirmation", "message")["status"] == "sent"
    )
    assert len(messaging.sent) == 1


def test_worker_death_mid_send_is_reconciled(env):
    made = book(env, consents={"consent_booking_confirmation": "true"})
    confirm(env)
    # A worker marked the contact sending and died before (or after) the provider call.
    execute(env["url"], "UPDATE reminders SET status='sending', attempts=1")
    at(env, NOW.replace(hour=9, minute=0))
    work(env)
    row = contact(env, made["appointment_id"], "booking_confirmation", "message")
    assert row["status"] == "sent" and env["ports"].messaging.lookups == 1
    assert len(env["ports"].messaging.sent) == 1


def test_call_timeout_goes_to_review_and_is_never_redialled(env):
    made = book(env, consents={"consent_reminder_call": "true"})
    confirm(env)
    env["ports"].bolna.timeout_after_call = True
    at(env, REMINDER_AT)
    work(env)
    row = contact(env, made["appointment_id"], "reminder", "call")
    assert (row["status"], row["last_error"]) == ("needs_review", "outcome_unknown")
    assert row["provider_execution_id"] is None
    [job] = jobs(env, "reminder.call")
    assert job["status"] == "done"
    execute(env["url"], "UPDATE outbox SET status='pending' WHERE id=%s", (job["id"],))
    work(env)
    assert len(calls(env)) == 1
    headers = staff_headers(env)
    listed = env["client"].get("/v1/admin/reminders?status=needs_review", headers=headers)
    assert [r["id"] for r in listed.json()] == [str(row["id"])]


def test_retryable_call_error_retries_and_permanent_fails(env):
    made = book(env, consents={"consent_reminder_call": "true"})
    confirm(env)
    env["ports"].bolna.fail_next("start_outbound_call", 1, ProviderRetryable)
    at(env, REMINDER_AT)
    work(env)
    row = contact(env, made["appointment_id"], "reminder", "call")
    assert (row["status"], row["last_error"]) == ("scheduled", "retryable")
    work(env)
    assert contact(env, made["appointment_id"], "reminder", "call")["status"] == "sent"
    other = book(
        env, consents={"consent_reminder_call": "true"}, starts_at=SLOT + timedelta(hours=1)
    )
    at(env, NOW)
    confirm(env)
    env["ports"].bolna.fail_next("start_outbound_call", 1, ProviderPermanent)
    at(env, REMINDER_AT + timedelta(hours=1))
    work(env)
    failed = contact(env, other["appointment_id"], "reminder", "call")
    assert (failed["status"], failed["last_error"]) == ("failed", "provider_permanent")


# Opt-out


def opt_out(env, execution_id, reference, scope="all", number=CALLER, token=None, confirmed=True):
    extra = {"confirmed": "true"} if confirmed else {}
    return post(
        env,
        OPT_OUT,
        body(execution_id, number, booking_reference=reference, scope=scope, **extra),
        token,
    )


def test_opt_out_tool_requires_verification_and_suppresses(env):
    mine = book(env)
    theirs = book(env, name=OTHER_NAME, number=OTHER_CALLER, starts_at=SLOT + timedelta(hours=1))
    confirm(env)
    session = str(uuid4())
    # Unverified: refused, nothing recorded.
    r = opt_out(env, session, mine["booking_reference"])
    assert r.status_code == 403 and r.json()["detail"] == bookings.OPT_OUT_NOT_VERIFIED
    # Another patient's reference from this phone neither verifies nor opts out.
    assert verify(env, session, theirs["booking_reference"]).status_code == 404
    assert opt_out(env, session, theirs["booking_reference"]).status_code == 403
    # Another clinic's credential with the same reference: not verified there.
    assert opt_out(env, session, mine["booking_reference"], token=env["other"]).status_code == 403
    assert rows(env["url"], "SELECT * FROM contact_preferences") == []
    assert verify(env, session, mine["booking_reference"]).status_code == 200
    # Without confirmed=true the change is only read back.
    r = opt_out(env, session, mine["booking_reference"], scope="calls", confirmed=False)
    assert r.status_code == 200 and r.json()["opted_out"] is False
    assert (
        "Please confirm" in r.json()["spoken"] and "Nothing has been changed" in r.json()["spoken"]
    )
    assert rows(env["url"], "SELECT * FROM contact_preferences") == []
    r = opt_out(env, session, mine["booking_reference"], scope="calls")
    assert r.status_code == 200, r.text
    assert r.json()["opted_out"] is True and "calls" in r.json()["spoken"]
    assert r.headers["cache-control"] == "no-store"
    assert opt_out(env, session, mine["booking_reference"], scope="calls").status_code == 200
    [preference] = rows(env["url"], "SELECT * FROM contact_preferences")
    assert (
        preference["purpose"],
        preference["channel"],
        preference["source"],
        preference["appointment_id"],
    ) == ("all", "call", "tool", UUID(mine["appointment_id"]))
    call = contact(env, mine["appointment_id"], "reminder", "call")
    assert (call["status"], call["last_error"]) == ("suppressed", "opted_out")
    assert contact(env, mine["appointment_id"], "reminder", "message")["status"] == "scheduled"
    # The other patient is untouched.
    assert {c["status"] for c in contacts(env, theirs["appointment_id"])} == {"scheduled"}
    # A verification in one session does not carry over to another.
    assert opt_out(env, str(uuid4()), mine["booking_reference"]).status_code == 403
    at(env, REMINDER_AT + timedelta(hours=1))
    work(env)
    assert [args[1] for args in calls(env)] == [OTHER_CALLER]
    # The opt-out covers that booking only: the same patient's later booking is reminded
    # (a patient-wide opt-out is a staff action).
    later = book(env, starts_at=SLOT + timedelta(days=3))
    at(env, NOW)
    confirm(env)
    assert contact(env, later["appointment_id"], "reminder", "call")["status"] == "scheduled"


def test_spoofed_caller_cannot_silence_the_patients_other_bookings(env):
    """Caller ID and the patient's name attach a spoofer's booking to the victim's patient
    record; verifying that new booking and opting out must not touch the victim's own."""
    victim = book(env)
    confirm(env)
    attacker = str(uuid4())
    spoofed = book(env, starts_at=SLOT + timedelta(hours=2), execution_id=attacker)
    confirm(env)
    patients = rows(
        env["url"],
        "SELECT DISTINCT patient_id FROM appointments WHERE id = ANY(%s)",
        ([UUID(victim["appointment_id"]), UUID(spoofed["appointment_id"])],),
    )
    assert len(patients) == 1  # Attached to the victim's record.
    session = str(uuid4())
    assert verify(env, session, spoofed["booking_reference"]).status_code == 200
    assert opt_out(env, session, spoofed["booking_reference"]).status_code == 200
    assert {c["status"] for c in contacts(env, spoofed["appointment_id"])} == {"suppressed"}
    assert {c["status"] for c in contacts(env, victim["appointment_id"])} == {"scheduled"}
    # The victim's booking is still verified only by its own reference.
    assert opt_out(env, session, victim["booking_reference"]).status_code == 403
    at(env, NOW.replace(hour=9, minute=0))
    work(env)
    victim_confirmation = contact(env, victim["appointment_id"], "booking_confirmation", "message")
    assert [m[2] for m in env["ports"].messaging.sent] == [
        victim_confirmation["provider_reference"]
    ]


def test_staff_opt_out(env):
    made = book(env)
    confirm(env)
    url = f"/v1/admin/appointments/{made['appointment_id']}/opt-out"
    viewer = staff_headers(env, "viewer")
    assert env["client"].post(url, json={}, headers=viewer).status_code == 403
    other = staff_headers(env, clinic=OTHER_CLINIC)
    assert env["client"].post(url, json={}, headers=other).status_code == 404
    admin = staff_headers(env, "admin")
    r = env["client"].post(url, json={"purpose": "reminder"}, headers=admin)
    assert r.status_code == 200 and r.json() == {
        "purpose": "reminder",
        "channel": "all",
        "suppressed": 2,
    }
    assert (
        contact(env, made["appointment_id"], "booking_confirmation", "message")["status"]
        == "scheduled"
    )
    [event] = rows(env["url"], "SELECT * FROM audit_events WHERE action='patient.opted_out'")
    assert event["target_id"] == UUID(made["appointment_id"])
    bad = env["client"].post(url, json={"channel": "sms"}, headers=admin)
    assert bad.status_code == 422


def test_staff_can_remove_an_opt_out(env):
    made = book(env)
    confirm(env)
    session = str(uuid4())
    assert verify(env, session, made["booking_reference"]).status_code == 200
    assert opt_out(env, session, made["booking_reference"]).status_code == 200
    admin = staff_headers(env, "admin")
    base = f"/v1/admin/appointments/{made['appointment_id']}/opt-out"
    assert env["client"].post(base, json={}, headers=admin).status_code == 200  # Patient-wide.
    assert len(rows(env["url"], "SELECT * FROM contact_preferences")) == 2
    viewer = staff_headers(env, "viewer")
    assert env["client"].post(base + "/remove", json={}, headers=viewer).status_code == 403
    other = staff_headers(env, clinic=OTHER_CLINIC)
    assert env["client"].post(base + "/remove", json={}, headers=other).status_code == 404
    r = env["client"].post(base + "/remove", json={}, headers=admin)
    assert r.status_code == 200 and r.json() == {"purpose": "all", "channel": "all", "removed": 2}
    assert rows(env["url"], "SELECT * FROM contact_preferences") == []
    [event] = rows(env["url"], "SELECT * FROM audit_events WHERE action='patient.opt_out_removed'")
    assert event["target_id"] == UUID(made["appointment_id"])
    # Suppressed contacts stay suppressed; the next booking is planned normally.
    assert {c["status"] for c in contacts(env, made["appointment_id"])} == {"suppressed"}
    later = book(env, starts_at=SLOT + timedelta(days=1))
    confirm(env)
    assert {c["status"] for c in contacts(env, later["appointment_id"])} == {"scheduled"}


def test_decline_on_a_matched_patient_speaks_that_staff_change_preferences(env):
    first = book(env, consents={"consent_reminder_call": "true"})
    assert gateway.PREFERENCE_KEPT not in first["spoken"]
    again = book(
        env, consents={"consent_reminder_call": "false"}, starts_at=SLOT + timedelta(hours=1)
    )
    assert gateway.PREFERENCE_KEPT in again["spoken"]
    assert rows(env["url"], "SELECT revoked_at FROM consents") == [{"revoked_at": None}]
    # A decline with no existing grant has nothing to keep: no sentence.
    fresh = book(
        env,
        consents={"consent_reminder_message": "false"},
        name=OTHER_NAME,
        starts_at=SLOT + timedelta(hours=2),
    )
    assert gateway.PREFERENCE_KEPT not in fresh["spoken"]


def test_webhook_dispositions_never_opt_out():
    for code in ("opted_out", "user_hangup", "do_not_call", "stop", None):
        assert not disposition_opts_out({"termination_reason": code})


# Concurrency, isolation, settings


def test_concurrent_scheduling_creates_each_reminder_once(env):
    execute(env["url"], "UPDATE clinics SET outbound_enabled=false WHERE id=%s", (CLINIC,))
    made = book(env)
    confirm(env)
    execute(env["url"], "UPDATE clinics SET outbound_enabled=true WHERE id=%s", (CLINIC,))
    barrier = Barrier(4)

    def race(_):
        with connect(env["url"]) as conn:
            barrier.wait()
            return reminders.schedule_due(conn)

    with ThreadPoolExecutor(4) as pool:
        created = list(pool.map(race, range(4)))
    assert sum(created) == 2
    keys = [r["dedupe_key"] for r in contacts(env, made["appointment_id"])]
    assert sorted(keys) == sorted(
        f"{made['appointment_id']}:1:{channel}:reminder:1440" for channel in ("call", "message")
    )
    assert len(jobs(env, "reminder.call")) == 1 and len(jobs(env, "notification.send")) == 1
    # The confirmation-time planner racing the backfill also yields one row per key.
    with connect(env["url"]) as conn:
        assert reminders.on_confirmed(conn, CLINIC, UUID(made["appointment_id"]), 1) == 1
        conn.commit()
    assert len(contacts(env, made["appointment_id"])) == 3


def test_cross_tenant_isolation(env):
    made = book(env)
    confirm(env)
    other = staff_headers(env, clinic=OTHER_CLINIC)
    assert env["client"].get("/v1/admin/reminders", headers=other).json() == []
    mine = staff_headers(env)
    listed = env["client"].get("/v1/admin/reminders", headers=mine).json()
    assert len(listed) == 3 and {r["appointment_id"] for r in listed} == {made["appointment_id"]}
    assert "provider_reference" not in listed[0] and "dedupe_key" not in listed[0]
    viewer = staff_headers(env, "viewer")
    assert env["client"].get("/v1/admin/reminders", headers=viewer).status_code == 403
    # A job's clinic scopes every statement. Another clinic's job cannot even name this
    # clinic's appointment (composite foreign key), for any kind:
    for kind in ("reminder.call", "notification.send", "reminder.stop"):
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            execute(
                env["url"],
                "INSERT INTO outbox (id,clinic_id,appointment_id,kind,appointment_version)"
                " VALUES (%s,%s,%s,%s,1)",
                (uuid4(), OTHER_CLINIC, made["appointment_id"], kind),
            )
    # and its contact jobs carrying this clinic's real reminder ids contact nobody and
    # change nothing here.
    call = contact(env, made["appointment_id"], "reminder", "call")
    message = contact(env, made["appointment_id"], "booking_confirmation", "message")
    foreign = []
    for kind, reminder in (("reminder.call", call), ("notification.send", message)):
        foreign.append(uuid4())
        execute(
            env["url"],
            "INSERT INTO outbox (id,clinic_id,kind,payload) VALUES (%s,%s,%s,%s::jsonb)",
            (foreign[-1], OTHER_CLINIC, kind, f'{{"reminder_id": "{reminder["id"]}"}}'),
        )
    before = {r["id"]: r for r in contacts(env, made["appointment_id"])}
    at(env, REMINDER_AT)  # Due for this clinic; the foreign jobs must still do nothing.
    execute(
        env["url"],
        "UPDATE outbox SET available_at = now() - interval '1 second' WHERE clinic_id=%s",
        (OTHER_CLINIC,),
    )
    while run_once(env["url"], "w1", env["ports"], kinds=["reminder.call", "reminder.stop"]):
        pass
    # Only the foreign jobs are due (this clinic's are at their planned 2031 times).
    assert env["ports"].messaging.sent == [] and env["ports"].bolna.calls == []
    after = {r["id"]: r for r in contacts(env, made["appointment_id"])}
    for reminder_id, row in before.items():
        for column in ("status", "attempts", "updated_at", "last_error", "scheduled_for"):
            assert after[reminder_id][column] == row[column]
    while run_once(env["url"], "w1", env["ports"], kinds=["notification.send"]):
        pass
    assert env["ports"].messaging.sent == []
    done = rows(env["url"], "SELECT status FROM outbox WHERE id = ANY(%s)", (foreign,))
    assert [r["status"] for r in done] == ["done"] * 2
    assert {r["id"]: r["status"] for r in contacts(env, made["appointment_id"])} == {
        reminder_id: row["status"] for reminder_id, row in before.items()
    }
    # This clinic's own jobs still work.
    work(env)
    assert len(calls(env)) == 1


def test_clinic_contact_settings(env):
    owner = staff_headers(env)
    r = env["client"].get("/v1/admin/clinic", headers=owner)
    assert r.status_code == 200
    settings = r.json()
    assert (settings["quiet_hours_start"], settings["quiet_hours_end"]) == ("21:00", "09:00")
    assert settings["reminder_lead_minutes"] == 1440 and settings["daily_contact_budget"] == 200
    assert settings["outbound_enabled"] is True  # Enabled by this test's fixture.
    other = env["client"].get("/v1/admin/clinic", headers=staff_headers(env, clinic=OTHER_CLINIC))
    assert other.json()["outbound_enabled"] is False  # The default.
    viewer = staff_headers(env, "viewer")
    assert (
        env["client"]
        .patch("/v1/admin/clinic", json={"outbound_enabled": False}, headers=viewer)
        .status_code
        == 403
    )
    for bad in (
        {"quiet_hours_start": "22:00", "quiet_hours_end": "22:00"},
        {"quiet_hours_start": "09:00"},  # Equal to the stored end.
        {"quiet_hours_end": "25:00"},
        {"reminder_lead_minutes": 10},
        {"daily_contact_budget": -1},
        {"outbound_enabled": None},
    ):
        assert env["client"].patch("/v1/admin/clinic", json=bad, headers=owner).status_code == 422
    r = env["client"].patch(
        "/v1/admin/clinic",
        json={"outbound_enabled": False, "quiet_hours_start": "20:30", "daily_contact_budget": 5},
        headers=owner,
    )
    assert r.status_code == 200, r.text
    assert (r.json()["outbound_enabled"], r.json()["quiet_hours_start"]) == (False, "20:30")
    actions = [row["action"] for row in rows(env["url"], "SELECT action FROM audit_events")]
    assert "clinic.outbound_disabled" in actions


def test_worker_without_a_port_leaves_its_kinds(env):
    made = book(env)
    confirm(env)
    calendar_only = Ports(calendar=FakeCalendar())
    assert set(excluded_kinds(calendar_only)) == {
        "bolna.reconcile",
        "reminder.call",
        "notification.send",
    }
    at(env, REMINDER_AT)
    work(env, ports=calendar_only)
    assert {c["status"] for c in contacts(env, made["appointment_id"])} == {"scheduled"}
    assert {j["status"] for j in jobs(env, "reminder.call")} == {"pending"}

    class Slow:
        def send_message(self, to, body, reference):
            raise AssertionError

        def get_message(self, reference):
            raise AssertionError

    with pytest.raises(ValueError):
        check_ports(Ports(calendar=FakeCalendar(), messaging=Slow()))


def test_cli_refuses_fake_messaging_outside_test(monkeypatch):
    settings = get_settings.__wrapped__ if hasattr(get_settings, "__wrapped__") else None
    assert settings is not None
    monkeypatch.setenv("DATABASE_URL", "postgresql://x@localhost/clinic")
    monkeypatch.setenv("APP_ENV", "production")
    configured = settings()
    with pytest.raises(SystemExit):
        cli.build_messaging("fake", "postgresql://x@localhost/clinic", configured)
    assert isinstance(
        cli.build_messaging("fake", "postgresql://x@localhost/clinic_test", configured),
        FakeMessaging,
    )
    assert cli.build_messaging(None, "postgresql://x@localhost/clinic", configured) is None


def test_retention_purge_removes_opt_outs(env):
    made = book(env)
    confirm(env)
    with connect(env["url"]) as conn:
        patient = conn.execute(
            "SELECT patient_id FROM appointments WHERE id=%s", (made["appointment_id"],)
        ).fetchone()["patient_id"]
        reminders.opt_out(
            conn, CLINIC, patient, "all", "all", "tool", appointment_id=made["appointment_id"]
        )
        conn.execute("UPDATE appointments SET status='cancelled'")
        conn.execute("UPDATE patients SET retention_until = now() - interval '1 day'")
        conn.commit()
        counts = retention.purge(conn, 90)
    assert counts["contact_preferences"] == 1 and counts["patients"] == 1
    assert rows(env["url"], "SELECT * FROM contact_preferences") == []
    # The planned contacts stay (no patient data) and no longer find a patient to contact.
    assert len(contacts(env, made["appointment_id"])) == 3


# Security review follow-ups


def test_start_of_next_day_when_midnight_does_not_exist():
    # America/Santiago springs forward at 2026-09-06 00:00 and America/Havana at
    # 2026-03-08 00:00: local midnight does not exist, and one fold lands on the evening
    # before. The next day starts at 01:00 local, on the next day.
    for zone, moment, expected in (
        (
            ZoneInfo("America/Santiago"),
            datetime(2026, 9, 5, 15, 0, tzinfo=ZoneInfo("America/Santiago")),
            datetime(2026, 9, 6, 4, 0, tzinfo=UTC),
        ),
        (
            ZoneInfo("America/Havana"),
            datetime(2026, 3, 7, 15, 0, tzinfo=ZoneInfo("America/Havana")),
            datetime(2026, 3, 8, 5, 0, tzinfo=UTC),
        ),
    ):
        start = reminders.start_of_next_day(moment, zone)
        assert start == expected
        assert reminders.local_day(start, zone) == moment.date() + timedelta(days=1)
        assert start.astimezone(zone).hour == 1
        # Repeated postponement always makes progress.
        assert reminders.start_of_next_day(start, zone) > start + timedelta(hours=12)


class Takeover:
    """A messaging port whose first send happens after another worker has taken the job
    over (the first worker's lease expired mid-send). Wraps a FakeMessaging."""

    timeout_seconds = 1

    def __init__(self, env, inner):
        self.env = env
        self.inner = inner
        self.idempotent_reference = inner.idempotent_reference
        self.consistent_lookup = inner.consistent_lookup
        self.taken_over = False

    def get_message(self, reference):
        return self.inner.get_message(reference)

    def send_message(self, to, body, reference):
        if not self.taken_over:
            self.taken_over = True
            execute(
                self.env["url"],
                "UPDATE outbox SET lease_expires_at = now() - interval '1 second'"
                " WHERE kind='notification.send' AND status='processing'",
            )
            ports = Ports(calendar=FakeCalendar(), messaging=self.inner)
            assert run_once(self.env["url"], "w2", ports, kinds=["notification.send"]) == 1
        return self.inner.send_message(to, body, reference)


@pytest.mark.parametrize("consistent", [True, False])
def test_lease_lost_mid_send_never_overwrites_the_new_owner(env, consistent):
    made = book(env, consents={"consent_booking_confirmation": "true"})
    confirm(env)
    inner = FakeMessaging(consistent_lookup=consistent)
    port = Takeover(env, inner)
    at(env, NOW.replace(hour=9, minute=0))
    work(env, ports=Ports(calendar=FakeCalendar(), messaging=port))
    row = contact(env, made["appointment_id"], "booking_confirmation", "message")
    [job] = jobs(env, "notification.send")
    assert len(inner.sent) == 1  # Delivered once, whoever sent first.
    if consistent:
        # The new owner found nothing yet, resent under the same reference (deduplicated by
        # the provider) and recorded attempt 2; the old worker's late write was fenced out.
        assert (row["status"], row["attempts"]) == ("sent", 2)
        assert row["provider_message_id"] == inner.by_reference[row["provider_reference"]]
    else:
        # A lookup that may lag cannot prove nothing was sent: staff review, no resend, and
        # the old worker does not overwrite it.
        assert (row["status"], row["last_error"]) == ("needs_review", "lookup_not_found")
    assert job["status"] == "done"


@pytest.mark.parametrize("flags", [{"idempotent_reference": False}, {"consistent_lookup": False}])
def test_lookup_not_found_resends_only_with_both_capabilities(env, flags):
    made = book(env, consents={"consent_booking_confirmation": "true"})
    confirm(env)
    messaging = FakeMessaging(**flags)
    ports = Ports(calendar=FakeCalendar(), messaging=messaging)
    messaging.timeout_before_send = True
    at(env, NOW.replace(hour=9, minute=0))
    work(env, ports=ports)
    work(env, ports=ports)
    row = contact(env, made["appointment_id"], "booking_confirmation", "message")
    assert (row["status"], row["last_error"]) == ("needs_review", "lookup_not_found")
    assert messaging.sent == [] and messaging.lookups == 1
    work(env, ports=ports)
    assert messaging.sent == []


def test_messaging_port_must_declare_capabilities(env):
    class Partial:
        timeout_seconds = 1
        idempotent_reference = True

        def send_message(self, to, body, reference):
            raise AssertionError

        def get_message(self, reference):
            raise AssertionError

    with pytest.raises(ValueError, match="consistent_lookup"):
        check_ports(Ports(calendar=FakeCalendar(), messaging=Partial()))


def test_booking_tools_are_refused_on_reminder_calls(env):
    made = book(env, consents={"consent_reminder_call": "true"})
    confirm(env)
    at(env, REMINDER_AT)
    work(env)
    execution_id = contact(env, made["appointment_id"], "reminder", "call")["provider_execution_id"]
    reference = made["booking_reference"]
    requests = (
        (VERIFY, {"booking_reference": reference}),
        (CANCEL, {"booking_reference": reference, "confirmed": "true"}),
        (
            RESCHEDULE,
            {
                "booking_reference": reference,
                "starts_at": (SLOT + timedelta(days=1)).isoformat(),
                "confirmed": "true",
            },
        ),
        (OPT_OUT, {"booking_reference": reference, "confirmed": "true"}),
        (
            RESERVE,
            {
                "doctor": "Demo Doctor",
                "service": "Consultation",
                "starts_at": (SLOT + timedelta(days=1)).isoformat(),
                "patient_name": NAME,
            },
        ),
    )
    # The reminder call's own execution id (any agent), or the outbound agent (any
    # execution, e.g. a reminder call whose execution id was never learned).
    for execution, agent in ((execution_id, AGENT), (str(uuid4()), OUTBOUND)):
        for path, extra in requests:
            payload = {"execution_id": execution, "from_number": CALLER, "agent_id": agent}
            r = post(env, path, {**payload, **extra})
            assert r.status_code == 403, (path, r.text)
            assert r.json()["detail"] == gateway.REMINDER_CALL
    assert {r["status"] for r in rows(env["url"], "SELECT status FROM appointments")} == {
        "confirmed"
    }
    assert rows(env["url"], "SELECT * FROM contact_preferences") == []
    # An inbound call still works.
    assert verify(env, str(uuid4()), reference).status_code == 200


def test_reminder_call_needs_a_separate_outbound_agent(env, capsys):
    made = book(env, consents={"consent_reminder_call": "true"})
    confirm(env)
    clinic = str(CLINIC)
    cli.main(["bolna", "set-agent", "--clinic", clinic, "--outbound", "--clear"])
    at(env, REMINDER_AT)
    work(env)
    row = contact(env, made["appointment_id"], "reminder", "call")
    assert (row["status"], row["last_error"]) == ("suppressed", "no_outbound_agent")
    assert calls(env) == []
    # The inbound agent (it carries the booking tools) is refused as the outbound agent.
    with pytest.raises(SystemExit):
        cli.main(["bolna", "set-agent", "--clinic", clinic, "--outbound", "--agent-id", AGENT])
    cli.main(["bolna", "set-agent", "--clinic", clinic, "--outbound", "--agent-id", OUTBOUND])
    assert f"bolna_outbound_agent_id={OUTBOUND}" in capsys.readouterr().out
    assert (
        rows(env["url"], "SELECT bolna_outbound_agent_id FROM clinics WHERE id=%s", (CLINIC,))[0][
            "bolna_outbound_agent_id"
        ]
        == OUTBOUND
    )


def place_call(env, consents=None):
    made = book(env, consents=consents or {"consent_reminder_call": "true"})
    confirm(env)
    at(env, REMINDER_AT)
    work(env)
    return made, contact(env, made["appointment_id"], "reminder", "call")


def test_stop_waits_for_an_in_flight_call_without_spending_attempts(env):
    made, placed = place_call(env)
    execute(
        env["url"],
        "UPDATE reminders SET status='sending', sent_at=NULL WHERE id=%s",
        (placed["id"],),
    )
    headers = staff_headers(env)
    env["client"].post(f"/v1/admin/appointments/{made['appointment_id']}/cancel", headers=headers)
    [job] = jobs(env, "reminder.stop")
    work(env)
    [deferred] = jobs(env, "reminder.stop")
    assert (deferred["status"], deferred["last_error"]) == ("pending", "contact_in_flight")
    assert deferred["max_attempts"] == job["max_attempts"] + 1
    assert calls(env, "stop_scheduled_call") == []
    execute(
        env["url"], "UPDATE reminders SET status='sent', sent_at=now() WHERE id=%s", (placed["id"],)
    )
    work(env)
    assert calls(env, "stop_scheduled_call") == [(placed["provider_execution_id"],)]


def test_dead_stop_job_sends_unstopped_calls_to_review(env):
    made, placed = place_call(env)
    headers = staff_headers(env)
    env["client"].post(f"/v1/admin/appointments/{made['appointment_id']}/cancel", headers=headers)
    execute(env["url"], "UPDATE outbox SET max_attempts=1 WHERE kind='reminder.stop'")
    env["ports"].bolna.fail_next("stop_scheduled_call", 1, ProviderRetryable)
    work(env)
    [job] = jobs(env, "reminder.stop")
    assert job["status"] == "dead"
    row = contact(env, made["appointment_id"], "reminder", "call")
    assert (row["status"], row["last_error"]) == ("needs_review", "stop_failed")
    assert row["sent_at"] is not None and row["stop_outcome"] is None


def test_opt_out_stops_a_placed_call_and_keeps_other_contacts(env):
    made, placed = place_call(env, consents=ALL_CONSENTS)
    execute(
        env["url"],
        "UPDATE reminders SET status='scheduled', sent_at=NULL"
        " WHERE appointment_id=%s AND purpose='reminder' AND channel='message'",
        (made["appointment_id"],),
    )
    session = str(uuid4())
    assert verify(env, session, made["booking_reference"]).status_code == 200
    assert opt_out(env, session, made["booking_reference"], scope="calls").status_code == 200
    [job] = jobs(env, "reminder.stop")
    assert job["payload"] == {"reason": "opted_out"} and job["appointment_version"] == 1
    work(env, kinds=["reminder.stop"])
    stopped = contact(env, made["appointment_id"], "reminder", "call")
    assert (stopped["status"], stopped["stop_outcome"]) == ("sent", "stopped")
    # An opt-out stop does not cancel the appointment's other contacts.
    assert contact(env, made["appointment_id"], "reminder", "message")["status"] == "scheduled"
    # A later cancellation of the same version shares the stop job.
    headers = staff_headers(env)
    r = env["client"].post(
        f"/v1/admin/appointments/{made['appointment_id']}/cancel", headers=headers
    )
    assert r.status_code == 200, r.text
    assert len(jobs(env, "reminder.stop")) == 1
    message = contact(env, made["appointment_id"], "reminder", "message")
    assert (message["status"], message["last_error"]) == ("cancelled", "appointment_changed")


def test_housekeeping_moves_contacts_without_a_live_job_to_review(env):
    made = book(env)
    confirm(env)
    call = contact(env, made["appointment_id"], "reminder", "call")
    message = contact(env, made["appointment_id"], "reminder", "message")
    confirmation = contact(env, made["appointment_id"], "booking_confirmation", "message")
    # The call's job is done and the message's is gone; the confirmation's is still pending.
    execute(
        env["url"],
        "UPDATE outbox SET status='done' WHERE payload->>'reminder_id' = %s",
        (str(call["id"]),),
    )
    execute(
        env["url"], "DELETE FROM outbox WHERE payload->>'reminder_id' = %s", (str(message["id"]),)
    )
    with connect(env["url"]) as conn:
        assert reminders.sweep_lost(conn) == 0  # Changed too recently.
    execute(env["url"], "UPDATE reminders SET updated_at = now() - interval '1 hour'")
    with connect(env["url"]) as conn:
        assert reminders.sweep_lost(conn) == 2
    for row in (call, message):
        swept = contact(env, made["appointment_id"], row["purpose"], row["channel"])
        assert (swept["status"], swept["last_error"]) == ("needs_review", "job_lost")
    kept = contact(env, made["appointment_id"], "booking_confirmation", "message")
    assert kept["status"] == "scheduled" and kept["id"] == confirmation["id"]


def test_lead_time_edit_never_plans_a_second_reminder(env):
    made = book(env)
    confirm(env)
    execute(env["url"], "UPDATE clinics SET reminder_lead_minutes=120 WHERE id=%s", (CLINIC,))
    with connect(env["url"]) as conn:
        ctx = reminders.context(conn, CLINIC, made["appointment_id"])
        assert ctx["reminder_lead_minutes"] == 120
        reminders.lock_planning(conn, CLINIC, ctx["id"])
        assert reminders.plan_reminders(conn, ctx, service.utc_now()) == 0
        conn.commit()
        assert reminders.schedule_due(conn) == 0
    kinds = sorted((r["purpose"], r["channel"]) for r in contacts(env, made["appointment_id"]))
    assert kinds == [
        ("booking_confirmation", "message"),
        ("reminder", "call"),
        ("reminder", "message"),
    ]


def test_send_rereads_clinic_settings_after_locking(env, monkeypatch):
    made = book(env, consents={"consent_booking_confirmation": "true"})
    confirm(env)
    original = reminders.lock_budget_day

    def lock_then_disable(conn, clinic_id, day):
        original(conn, clinic_id, day)
        # Committed by staff after the send's first read, before its row locks.
        execute(env["url"], "UPDATE clinics SET outbound_enabled=false WHERE id=%s", (CLINIC,))

    monkeypatch.setattr(reminders, "lock_budget_day", lock_then_disable)
    at(env, NOW.replace(hour=9, minute=0))
    work(env)
    row = contact(env, made["appointment_id"], "booking_confirmation", "message")
    assert (row["status"], row["last_error"]) == ("suppressed", "outbound_disabled")
    assert env["ports"].messaging.sent == []


def test_planning_failure_does_not_block_confirmation(env, monkeypatch, caplog):
    def broken(conn, clinic_id, appointment_id, version):
        conn.execute("SELECT 1 / 0")

    monkeypatch.setattr(reminders, "on_confirmed", broken)
    made = book(env)
    confirm(env)
    [appointment] = rows(
        env["url"], "SELECT status FROM appointments WHERE id=%s", (made["appointment_id"],)
    )
    assert appointment["status"] == "confirmed"
    assert {j["status"] for j in jobs(env, "calendar.create")} == {"done"}
    assert contacts(env) == []
    [record] = [r for r in caplog.records if "contact planning failed" in r.getMessage()]
    assert "DivisionByZero" in record.getMessage() and "1 / 0" not in record.getMessage()


def test_backfill_is_not_starved_by_opted_out_appointments(env):
    execute(env["url"], "UPDATE clinics SET outbound_enabled=false WHERE id=%s", (CLINIC,))
    early = [
        book(env, starts_at=SLOT + timedelta(hours=n), execution_id=str(uuid4())) for n in (1, 2, 3)
    ]
    legitimate = book(
        env, name=OTHER_NAME, number=OTHER_CALLER, starts_at=SLOT + timedelta(hours=4)
    )
    confirm(env)
    assert contacts(env) == []
    with connect(env["url"]) as conn:
        for made in early:
            row = conn.execute(
                "SELECT patient_id FROM appointments WHERE id=%s", (made["appointment_id"],)
            ).fetchone()
            reminders.opt_out(
                conn,
                CLINIC,
                row["patient_id"],
                "reminder",
                "all",
                "tool",
                appointment_id=made["appointment_id"],
            )
        conn.commit()
    execute(env["url"], "UPDATE clinics SET outbound_enabled=true WHERE id=%s", (CLINIC,))
    # A batch smaller than the opted-out appointments ahead of the legitimate one (the same
    # logic as more than BACKFILL_BATCH of them): it still reaches the legitimate one.
    with connect(env["url"]) as conn:
        assert reminders.schedule_due(conn, limit=2) == 2
        assert reminders.schedule_due(conn, limit=2) == 0
    assert {
        (c["purpose"], c["channel"], c["status"])
        for c in contacts(env, legitimate["appointment_id"])
    } == {("reminder", "call", "scheduled"), ("reminder", "message", "scheduled")}
    for made in early:
        assert contacts(env, made["appointment_id"]) == []


def test_refused_reminder_is_recorded_so_backfill_skips_it(env):
    made = book(env, consents={"consent_reminder_call": "true"})
    confirm(env)
    message = contact(env, made["appointment_id"], "reminder", "message")
    assert (message["status"], message["last_error"]) == ("suppressed", "no_consent")
    with connect(env["url"]) as conn:
        assert reminders.schedule_due(conn) == 0


def test_outcome_write_is_fenced_on_the_attempt(env):
    from clinic_voice.handlers import reminders as handlers

    made = book(env, consents={"consent_booking_confirmation": "true"})
    confirm(env)
    row = contact(env, made["appointment_id"], "booking_confirmation", "message")
    # A newer attempt (2) is in flight; the stale worker of attempt 1 reports a failure.
    execute(
        env["url"], "UPDATE reminders SET status='sending', attempts=2 WHERE id=%s", (row["id"],)
    )
    with connect(env["url"]) as conn:
        assert not handlers._set(
            conn, CLINIC, row["id"], "failed", "provider_permanent", attempts=1
        )
        assert (
            contact(env, made["appointment_id"], "booking_confirmation", "message")["status"]
            == "sending"
        )
        assert handlers._set(conn, CLINIC, row["id"], "sent", None, attempts=2)
    assert (
        contact(env, made["appointment_id"], "booking_confirmation", "message")["status"] == "sent"
    )


def test_spoofed_decline_cannot_revoke_the_victims_consent(env):
    """R1: a spoofer attaches a booking to the victim's record (caller ID + name), verifies
    it, then declines reminders on another reservation in the same call. The decline only
    opts that reservation out; the victim's consent and other bookings are untouched."""
    victim = book(env)
    confirm(env)
    call = str(uuid4())
    spoofed = book(env, starts_at=SLOT + timedelta(hours=2), execution_id=call)
    assert verify(env, call, spoofed["booking_reference"]).status_code == 200
    declined = book(
        env,
        consents={key: "false" for key in ALL_CONSENTS},
        starts_at=SLOT + timedelta(hours=3),
        execution_id=call,
    )
    assert gateway.PREFERENCE_KEPT in declined["spoken"]
    confirm(env)
    assert rows(env["url"], "SELECT revoked_at FROM consents WHERE revoked_at IS NOT NULL") == []
    assert {c["status"] for c in contacts(env, victim["appointment_id"])} == {"scheduled"}
    assert {c["status"] for c in contacts(env, spoofed["appointment_id"])} == {"scheduled"}
    assert {(c["status"], c["last_error"]) for c in contacts(env, declined["appointment_id"])} == {
        ("suppressed", "opted_out")
    }
    preferences = rows(env["url"], "SELECT appointment_id FROM contact_preferences")
    assert {p["appointment_id"] for p in preferences} == {UUID(declined["appointment_id"])}


def test_reschedule_carries_booking_opt_outs_to_the_new_booking(env):
    made = book(env)
    confirm(env)
    session = str(uuid4())
    assert verify(env, session, made["booking_reference"]).status_code == 200
    assert opt_out(env, session, made["booking_reference"], scope="calls").status_code == 200
    moved = SLOT + timedelta(days=1)
    r = post(
        env,
        RESCHEDULE,
        body(
            session,
            booking_reference=made["booking_reference"],
            starts_at=moved.isoformat(),
            confirmed="true",
        ),
    )
    assert r.status_code == 200, r.text
    [new] = rows(
        env["url"],
        "SELECT id FROM appointments WHERE rescheduled_from=%s",
        (made["appointment_id"],),
    )
    confirm(env)
    call = contact(env, new["id"], "reminder", "call")
    assert (call["status"], call["last_error"]) == ("suppressed", "opted_out")
    assert contact(env, new["id"], "reminder", "message")["status"] == "scheduled"
    scoped = rows(
        env["url"],
        "SELECT channel FROM contact_preferences WHERE appointment_id=%s",
        (new["id"],),
    )
    assert scoped == [{"channel": "call"}]


def test_housekeeping_moves_unknown_contacts_without_a_job_to_review(env):
    made = book(env, consents={"consent_booking_confirmation": "true"})
    confirm(env)
    row = contact(env, made["appointment_id"], "booking_confirmation", "message")
    execute(
        env["url"],
        "UPDATE reminders SET status='unknown', updated_at = now() - interval '1 hour' WHERE id=%s",
        (row["id"],),
    )
    execute(env["url"], "UPDATE outbox SET status='dead' WHERE kind='notification.send'")
    with connect(env["url"]) as conn:
        assert reminders.sweep_lost(conn) == 1
    swept = contact(env, made["appointment_id"], "booking_confirmation", "message")
    assert (swept["status"], swept["last_error"]) == ("needs_review", "job_lost")
