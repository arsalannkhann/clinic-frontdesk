"""Durable outbox worker, verified confirmation, status tool and dead-letter administration.

All provider behaviour comes from FakeCalendar. "Now" for slot math is pinned like
tests/test_calendar.py; lease and retry times use the database clock, and tests move rows in
time with SQL instead of sleeping.
"""

from datetime import datetime, time, timedelta
from threading import Barrier, Thread
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from clinic_voice import cli, credentials, service, staff, worker
from clinic_voice.api import app
from clinic_voice.cli import DEMO_CLINIC_ID as CLINIC
from clinic_voice.cli import DOCTOR_ID, SERVICE_ID
from clinic_voice.config import get_settings
from clinic_voice.db import connect
from clinic_voice.providers.calendar import (
    CalendarEvent,
    FakeCalendar,
    ProviderPermanent,
    ProviderRetryable,
    event_id_for,
)
from clinic_voice.schemas import ReservationRequest
from clinic_voice.worker import Ports, claim, complete, fail_dead, retry, run_once

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2031, 3, 3, 8, 30, tzinfo=IST)
DAY = NOW.date() + timedelta(days=2)
PASSWORD = "synthetic-password-01"
CAL = "booking@example.test"
BUSY = "busy@example.test"


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setattr(staff, "SCRYPT_N", 2**10)
    monkeypatch.setattr(service, "utc_now", lambda: NOW)


@pytest.fixture
def client(database, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database)
    get_settings.cache_clear()
    with TestClient(app) as value:
        yield value
    get_settings.cache_clear()


@pytest.fixture
def mapped(database):
    map_doctor(database)
    return database


def at(hour, minute=0, day=DAY):
    return datetime.combine(day, time(hour, minute), IST)


def map_doctor(database, booking=CAL, busy=(BUSY,)):
    with connect(database) as conn:
        conn.execute(
            "UPDATE doctors SET booking_calendar_id=%s, busy_calendar_ids=%s WHERE id=%s",
            (booking, list(busy), DOCTOR_ID),
        )


def book(database, hour=10, minute=0):
    request = ReservationRequest(
        doctor_id=DOCTOR_ID, service_id=SERVICE_ID, starts_at=at(hour, minute)
    )
    return service.reserve(database, CLINIC, request, f"key-{uuid4().hex}")["appointment_id"]


def appointment(database, appointment_id):
    with connect(database) as conn:
        return conn.execute("SELECT * FROM appointments WHERE id=%s", (appointment_id,)).fetchone()


def job_for(database, appointment_id, kind="calendar.create"):
    with connect(database) as conn:
        return conn.execute(
            "SELECT * FROM outbox WHERE appointment_id=%s AND kind=%s", (appointment_id, kind)
        ).fetchone()


def make_due(database):
    with connect(database) as conn:
        conn.execute(
            "UPDATE outbox SET available_at = now() - interval '1 second' WHERE status='pending'"
        )


def expire_leases(database):
    with connect(database) as conn:
        conn.execute(
            "UPDATE outbox SET lease_expires_at = now() - interval '1 second'"
            " WHERE status='processing'"
        )


def insert_job(database, kind, appointment_id=None, payload=None, clinic=CLINIC):
    job_id = uuid4()
    with connect(database) as conn:
        conn.execute(
            "INSERT INTO outbox (id,clinic_id,appointment_id,kind,payload) VALUES (%s,%s,%s,%s,%s)",
            (job_id, clinic, appointment_id, kind, Jsonb(payload or {})),
        )
    return job_id


def job_by_id(database, job_id):
    with connect(database) as conn:
        return conn.execute("SELECT * FROM outbox WHERE id=%s", (job_id,)).fetchone()


def ops(fake, op):
    return [c for c in fake.calls if c[0] == op]


def assert_confirmed_once(database, fake, appointment_id):
    row = appointment(database, appointment_id)
    event_id = event_id_for(appointment_id, 1)
    assert row["status"] == "confirmed"
    assert row["calendar_event_id"] == event_id
    assert fake.insert_count(event_id) == 1
    assert job_for(database, appointment_id)["status"] == "done"


# Happy path and recovery


def test_happy_path_confirms_with_one_event(mapped):
    fake = FakeCalendar()
    appointment_id = book(mapped)
    assert run_once(mapped, "w1", Ports(fake)) == 1
    assert_confirmed_once(mapped, fake, appointment_id)
    event_id = event_id_for(appointment_id, 1)
    # Recovery read first, then the external busy re-check, then the write.
    assert [c[0] for c in fake.calls] == ["get_event", "free_busy", "insert_event"]
    assert fake.calls[1][1] == (BUSY,)  # The booking calendar is not re-checked.
    stored = fake.get_event(CAL, event_id)
    assert (stored.start, stored.end) == (at(10), at(10, 15))
    job = job_for(mapped, appointment_id)
    assert (job["attempts"], job["lease_expires_at"], job["last_error"]) == (1, None, None)
    assert run_once(mapped, "w1", Ports(fake)) == 0


def test_two_concurrent_workers_process_each_job_once(mapped):
    fake = FakeCalendar()
    ids = [book(mapped, 10, 15 * i) for i in range(4)] + [
        book(mapped, 11, 15 * i) for i in range(4)
    ]
    barrier = Barrier(2)
    errors = []

    def drain(name):
        try:
            barrier.wait(5)
            while run_once(mapped, name, Ports(fake), limit=2):
                pass
        except BaseException as error:  # noqa: BLE001 - re-raised in the test thread
            errors.append(error)

    threads = [Thread(target=drain, args=(f"w{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not errors
    for appointment_id in ids:
        assert_confirmed_once(mapped, fake, appointment_id)
        event_id = event_id_for(appointment_id, 1)
        assert sum(1 for c in ops(fake, "insert_event") if c[2] == event_id) == 1
        assert sum(1 for c in ops(fake, "get_event") if c[2] == event_id) == 1
        assert job_for(mapped, appointment_id)["attempts"] == 1  # Claimed exactly once.


def test_timeout_after_write_recovers_by_reading_event(mapped):
    fake = FakeCalendar()
    fake.timeout_after_write = True
    appointment_id = book(mapped)
    run_once(mapped, "w1", Ports(fake))
    job = job_for(mapped, appointment_id)
    assert (job["status"], job["last_error"], job["attempts"]) == ("pending", "timeout", 1)
    assert appointment(mapped, appointment_id)["status"] == "pending_sync"
    make_due(mapped)
    run_once(mapped, "w2", Ports(fake))
    assert_confirmed_once(mapped, fake, appointment_id)
    assert len(ops(fake, "insert_event")) == 1  # The second attempt found the event.


def test_worker_dies_after_provider_success(mapped):
    fake = FakeCalendar()
    appointment_id = book(mapped)
    event_id = event_id_for(appointment_id, 1)
    row = appointment(mapped, appointment_id)
    with connect(mapped) as conn:
        [job] = claim(conn, "w1")
    # w1 writes the event, then "dies" before recording anything.
    fake.insert_event(CAL, CalendarEvent(event_id, row["starts_at"], row["ends_at"], "s"))
    assert run_once(mapped, "w2", Ports(fake)) == 0  # Lease still held by w1.
    expire_leases(mapped)
    assert run_once(mapped, "w2", Ports(fake)) == 1
    assert_confirmed_once(mapped, fake, appointment_id)
    assert job_for(mapped, appointment_id)["attempts"] == 2
    with connect(mapped) as conn:
        assert complete(conn, job) is False  # The dead worker's late completion is fenced.


def test_timeout_before_write_retries_with_backoff_then_inserts_once(mapped):
    fake = FakeCalendar()
    fake.timeout_before_write = True
    appointment_id = book(mapped)
    run_once(mapped, "w1", Ports(fake))
    job = job_for(mapped, appointment_id)
    assert (job["status"], job["last_error"]) == ("pending", "timeout")
    assert job["available_at"] - job["updated_at"] == timedelta(seconds=30)
    assert run_once(mapped, "w1", Ports(fake)) == 0  # Not due yet.
    make_due(mapped)
    run_once(mapped, "w1", Ports(fake))
    assert_confirmed_once(mapped, fake, appointment_id)
    assert len(ops(fake, "insert_event")) == 2  # One timed out without storing.


def test_backoff_schedule():
    assert [worker.backoff(n) for n in (1, 2, 3)] == [timedelta(seconds=s) for s in (30, 60, 120)]
    assert worker.backoff(8) == timedelta(hours=1)
    assert worker.backoff(50) == timedelta(hours=1)


class SlowDown(ProviderRetryable):
    def __init__(self):
        super().__init__("rate_limited_with_provider_detail", retry_after=120)


def test_retry_after_from_provider_is_used(mapped):
    fake = FakeCalendar()
    fake.fail_next(1, SlowDown)
    appointment_id = book(mapped)
    run_once(mapped, "w1", Ports(fake))
    job = job_for(mapped, appointment_id)
    assert job["available_at"] - job["updated_at"] == timedelta(seconds=120)
    assert job["last_error"] == "retryable"  # Fixed code, never the provider's text.


def test_permanent_error_dead_letters(mapped):
    fake = FakeCalendar()
    fake.fail_next(1, ProviderPermanent)
    appointment_id = book(mapped)
    run_once(mapped, "w1", Ports(fake))
    assert job_for(mapped, appointment_id)["last_error"] == "provider_permanent"
    assert appointment(mapped, appointment_id)["status"] == "needs_review"


# Review outcomes


def test_external_conflict_moves_to_review_without_insert(mapped):
    fake = FakeCalendar()
    fake.busy[BUSY] = [(at(10, 5), at(10, 10))]
    appointment_id = book(mapped)
    run_once(mapped, "w1", Ports(fake))
    job = job_for(mapped, appointment_id)
    assert (job["status"], job["last_error"]) == ("dead", "external_conflict")
    assert appointment(mapped, appointment_id)["status"] == "needs_review"
    assert ops(fake, "insert_event") == []


def test_unmapped_calendar_moves_to_review(database):
    fake = FakeCalendar()
    appointment_id = book(database)
    run_once(database, "w1", Ports(fake))
    assert job_for(database, appointment_id)["last_error"] == "calendar_unmapped"
    assert appointment(database, appointment_id)["status"] == "needs_review"
    assert fake.calls == []


@pytest.mark.parametrize("cancelled", [False, True])
def test_mismatched_or_cancelled_existing_event_moves_to_review(mapped, cancelled):
    fake = FakeCalendar()
    appointment_id = book(mapped)
    event_id = event_id_for(appointment_id, 1)
    if cancelled:
        fake.insert_event(CAL, CalendarEvent(event_id, at(10), at(10, 15), "s"))
        fake.cancel_event(CAL, event_id)
    else:
        fake.insert_event(CAL, CalendarEvent(event_id, at(12), at(12, 15), "s"))
    fake.calls.clear()
    run_once(mapped, "w1", Ports(fake))
    assert job_for(mapped, appointment_id)["last_error"] == "event_mismatch"
    row = appointment(mapped, appointment_id)
    assert (row["status"], row["calendar_event_id"]) == ("needs_review", None)
    assert ops(fake, "insert_event") == []


def test_dead_letter_after_max_attempts_and_requeue(client, mapped):
    fake = FakeCalendar()
    fake.fail_next(2, ProviderRetryable)
    appointment_id = book(mapped)
    with connect(mapped) as conn:
        conn.execute("UPDATE outbox SET max_attempts=2 WHERE appointment_id=%s", (appointment_id,))
    run_once(mapped, "w1", Ports(fake))
    assert job_for(mapped, appointment_id)["status"] == "pending"
    make_due(mapped)
    run_once(mapped, "w1", Ports(fake))
    job = job_for(mapped, appointment_id)
    assert (job["status"], job["attempts"], job["last_error"]) == ("dead", 2, "retryable")
    assert appointment(mapped, appointment_id)["status"] == "needs_review"

    owner = staff_headers(client, mapped)
    listed = client.get("/v1/admin/outbox", headers=owner, params={"status": "dead"})
    assert listed.status_code == 200
    [entry] = listed.json()
    assert entry["id"] == str(job["id"]) and entry["last_error"] == "retryable"
    assert "payload" not in entry and "lease_owner" not in entry
    assert client.get("/v1/admin/outbox?status=pending", headers=owner).json() == []
    assert client.get("/v1/admin/outbox?status=done", headers=owner).status_code == 422

    viewer = staff_headers(client, mapped, role="viewer")
    assert client.get("/v1/admin/outbox", headers=viewer).status_code == 403
    assert client.post(f"/v1/admin/outbox/{job['id']}/requeue", headers=viewer).status_code == 403
    other = staff_headers(client, mapped, clinic=foreign_clinic(mapped))
    assert client.get("/v1/admin/outbox", headers=other).json() == []
    assert client.post(f"/v1/admin/outbox/{job['id']}/requeue", headers=other).status_code == 404
    missing = client.post(f"/v1/admin/outbox/{uuid4()}/requeue", headers=owner)
    assert missing.status_code == 404

    admin = staff_headers(client, mapped, role="admin")
    r = client.post(f"/v1/admin/outbox/{job['id']}/requeue", headers=admin)
    assert r.status_code == 200
    assert (r.json()["status"], r.json()["attempts"]) == ("pending", 0)
    assert appointment(mapped, appointment_id)["status"] == "pending_sync"
    again = client.post(f"/v1/admin/outbox/{job['id']}/requeue", headers=admin)
    assert again.status_code == 409
    with connect(mapped) as conn:
        actions = [
            r["action"]
            for r in conn.execute(
                "SELECT action FROM audit_events WHERE target_id=%s", (job["id"],)
            ).fetchall()
        ]
    assert actions == ["outbox.requeued"]

    run_once(mapped, "w1", Ports(fake))
    assert_confirmed_once(mapped, fake, appointment_id)


# Fencing and superseded jobs


def test_stale_worker_cannot_touch_reclaimed_job(mapped):
    appointment_id = book(mapped)
    with connect(mapped) as conn:
        [stale] = claim(conn, "w1")
    expire_leases(mapped)
    with connect(mapped) as conn:
        [fresh] = claim(conn, "w2")
    assert (fresh.id, fresh.attempts) == (stale.id, 2)
    before = job_by_id(mapped, stale.id)
    with connect(mapped) as conn:
        assert complete(conn, stale) is False
        assert retry(conn, stale, "timeout") is False
        assert fail_dead(conn, stale, "provider_permanent") is False
    after = job_by_id(mapped, stale.id)
    assert after == before
    assert (after["status"], after["lease_owner"]) == ("processing", "w2")
    assert appointment(mapped, appointment_id)["status"] == "pending_sync"
    with connect(mapped) as conn:
        assert complete(conn, fresh) is True


def test_same_worker_reclaim_is_fenced_by_attempts(mapped):
    book(mapped)
    with connect(mapped) as conn:
        [first] = claim(conn, "w1")
    expire_leases(mapped)
    with connect(mapped) as conn:
        [second] = claim(conn, "w1")
        assert complete(conn, first) is False
        assert complete(conn, second) is True


@pytest.mark.parametrize("change", ["version=2", "status='cancelled'"])
def test_superseded_appointment_completes_without_provider_calls(mapped, change):
    fake = FakeCalendar()
    appointment_id = book(mapped)
    with connect(mapped) as conn:
        conn.execute(f"UPDATE appointments SET {change} WHERE id=%s", (appointment_id,))  # noqa: S608
    run_once(mapped, "w1", Ports(fake))
    assert job_for(mapped, appointment_id)["status"] == "done"
    assert fake.calls == []
    assert appointment(mapped, appointment_id)["status"] != "confirmed"


def test_unknown_kind_dead_letters_without_touching_appointment(mapped):
    appointment_id = book(mapped)
    job_id = insert_job(mapped, "notification.send", appointment_id)
    with connect(mapped) as conn:
        conn.execute(
            "UPDATE outbox SET available_at = now() + interval '1 hour'"
            " WHERE appointment_id=%s AND kind='calendar.create'",
            (appointment_id,),
        )
    run_once(mapped, "w1", Ports(FakeCalendar()))
    assert (job_by_id(mapped, job_id)["status"], job_by_id(mapped, job_id)["last_error"]) == (
        "dead",
        "unknown_kind",
    )
    assert appointment(mapped, appointment_id)["status"] == "pending_sync"


def test_unexpected_handler_error_retries_as_internal(mapped, monkeypatch):
    def boom(conn, job, ports):
        raise ValueError("detail that must not be stored")

    monkeypatch.setitem(worker.HANDLERS, "calendar.create", boom)
    appointment_id = book(mapped)
    assert run_once(mapped, "w1", Ports(FakeCalendar())) == 1
    job = job_for(mapped, appointment_id)
    assert (job["status"], job["last_error"]) == ("pending", "internal")


def test_calendar_job_requires_appointment(database):
    from psycopg.errors import CheckViolation

    with pytest.raises(CheckViolation):
        insert_job(database, "calendar.create")


# Other calendar handlers


def test_cancel_job_cancels_event_idempotently(mapped):
    fake = FakeCalendar()
    appointment_id = book(mapped)
    run_once(mapped, "w1", Ports(fake))
    event_id = event_id_for(appointment_id, 1)
    first = insert_job(mapped, "calendar.cancel", appointment_id)
    run_once(mapped, "w1", Ports(fake))
    assert job_by_id(mapped, first)["status"] == "done"
    assert fake.get_event(CAL, event_id).status == "cancelled"
    # A replay with an explicit event id is still harmless.
    with connect(mapped) as conn:
        conn.execute("DELETE FROM outbox WHERE id=%s", (first,))
    second = insert_job(mapped, "calendar.cancel", appointment_id, {"event_id": event_id})
    run_once(mapped, "w1", Ports(fake))
    assert job_by_id(mapped, second)["status"] == "done"


@pytest.mark.parametrize("is_mapped", [True, False])
def test_cancel_without_recorded_event(database, is_mapped):
    if is_mapped:
        map_doctor(database)
    fake = FakeCalendar()
    appointment_id = book(database)
    with connect(database) as conn:
        conn.execute("UPDATE outbox SET status='done' WHERE appointment_id=%s", (appointment_id,))
    job_id = insert_job(database, "calendar.cancel", appointment_id)
    run_once(database, "w1", Ports(fake))
    assert job_by_id(database, job_id)["status"] == "done"
    # Mapped: the deterministic id is cancelled defensively (a no-op when absent).
    expected = [("cancel_event", CAL, event_id_for(appointment_id, 1))] if is_mapped else []
    assert fake.calls == expected


def test_cancelled_during_projection_records_event_id(mapped, monkeypatch):
    fake = FakeCalendar()
    appointment_id = book(mapped)
    real_insert = fake.insert_event

    def insert_then_cancel_locally(calendar_id, event):
        stored = real_insert(calendar_id, event)
        with connect(mapped) as conn:  # The patient cancels while the write is in flight.
            conn.execute(
                "UPDATE appointments SET status='cancelled' WHERE id=%s", (appointment_id,)
            )
        return stored

    monkeypatch.setattr(fake, "insert_event", insert_then_cancel_locally)
    run_once(mapped, "w1", Ports(fake))
    row = appointment(mapped, appointment_id)
    assert row["status"] == "cancelled"
    assert row["calendar_event_id"] == event_id_for(appointment_id, 1)
    assert job_for(mapped, appointment_id)["status"] == "done"


def test_refresh_busy_job_updates_cache(mapped):
    fake = FakeCalendar()
    fake.busy[BUSY] = [(at(10), at(11))]
    payload = {"doctor_id": str(DOCTOR_ID), "start": at(0).isoformat(), "end": at(23).isoformat()}
    job_id = insert_job(mapped, "calendar.refresh_busy", None, payload)
    run_once(mapped, "w1", Ports(fake))
    assert job_by_id(mapped, job_id)["status"] == "done"
    with connect(mapped) as conn:
        rows = conn.execute(
            "SELECT starts_at, ends_at FROM external_busy WHERE clinic_id=%s", (CLINIC,)
        ).fetchall()
    assert [(r["starts_at"], r["ends_at"]) for r in rows] == [(at(10), at(11))]


def test_refresh_busy_errors(mapped):
    fake = FakeCalendar()
    fake.fail_next(1, ProviderRetryable, op="free_busy")
    payload = {"doctor_id": str(DOCTOR_ID), "start": at(0).isoformat(), "end": at(23).isoformat()}
    retried = insert_job(mapped, "calendar.refresh_busy", None, payload)
    bad = insert_job(mapped, "calendar.refresh_busy", None, {"doctor_id": "nope"})
    foreign = insert_job(
        mapped, "calendar.refresh_busy", None, {**payload, "doctor_id": str(uuid4())}
    )
    run_once(mapped, "w1", Ports(fake))
    assert job_by_id(mapped, retried)["status"] == "pending"
    assert job_by_id(mapped, bad)["last_error"] == "bad_payload"
    assert job_by_id(mapped, foreign)["last_error"] == "not_found"


# Provider I/O never runs inside a transaction


class IdleCheckingCalendar(FakeCalendar):
    """Before every provider call, record any other backend of the test database that is
    not idle (inside a transaction or running a statement)."""

    def __init__(self, database):
        super().__init__()
        self.checker = connect(database)
        self.checker.autocommit = True  # Fresh pg_stat_activity snapshot per query.
        self.busy_backends = []
        self.checks = 0

    def _check(self):
        self.checks += 1
        rows = self.checker.execute(
            "SELECT state FROM pg_stat_activity WHERE datname=current_database()"
            " AND pid <> pg_backend_pid() AND backend_type='client backend' AND state <> 'idle'"
        ).fetchall()
        self.busy_backends.extend(r["state"] for r in rows)

    def get_event(self, *args):
        self._check()
        return super().get_event(*args)

    def insert_event(self, *args):
        self._check()
        return super().insert_event(*args)

    def cancel_event(self, *args):
        self._check()
        return super().cancel_event(*args)

    def free_busy(self, *args):
        self._check()
        return super().free_busy(*args)


def test_provider_io_runs_with_no_open_transaction(mapped):
    fake = IdleCheckingCalendar(mapped)
    try:
        appointment_id = book(mapped)
        fake.timeout_after_write = True
        run_once(mapped, "w1", Ports(fake))
        make_due(mapped)
        run_once(mapped, "w1", Ports(fake))
        insert_job(mapped, "calendar.cancel", appointment_id)
        payload = {
            "doctor_id": str(DOCTOR_ID),
            "start": at(0).isoformat(),
            "end": at(23).isoformat(),
        }
        insert_job(mapped, "calendar.refresh_busy", None, payload)
        run_once(mapped, "w1", Ports(fake))
    finally:
        fake.checker.close()
    assert fake.checks >= 7
    assert fake.busy_backends == []


def test_require_idle_guard(database):
    with connect(database) as conn:
        conn.execute("SELECT 1")
        with pytest.raises(RuntimeError):
            claim(conn, "w1")


# Appointment status tool


def tool_headers(database, clinic=CLINIC):
    with connect(database) as conn:
        _, token = credentials.issue(conn, clinic, "synthetic")
    return {"Authorization": "Bearer " + token}


def test_only_persisted_confirmed_is_spoken_as_confirmed():
    for status in ("confirmed", "pending_sync", "needs_review", "cancelled", "unexpected", None):
        text = service.spoken_status(status).lower()
        says_confirmed = "confirmed" in text and "not yet confirmed" not in text
        says_confirmed = says_confirmed and "not confirmed" not in text
        assert says_confirmed == (status == "confirmed"), status
    assert "not yet confirmed" in service.spoken_status("pending_sync")
    assert "staff" in service.spoken_status("needs_review")
    assert "cancelled" in service.spoken_status("cancelled")


def test_status_tool_follows_worker_outcome(client, mapped):
    h = tool_headers(mapped)
    appointment_id = book(mapped)

    def status():
        r = client.post(
            "/v1/tools/appointment-status", headers=h, json={"appointment_id": str(appointment_id)}
        )
        assert r.status_code == 200, r.text
        return r.json()

    pending = status()
    assert pending["status"] == "pending_sync"
    assert pending["spoken_status"] == service.spoken_status("pending_sync")
    fake = FakeCalendar()
    fake.timeout_after_write = True  # The event exists but is not verified yet.
    run_once(mapped, "w1", Ports(fake))
    assert status()["spoken_status"] == service.spoken_status("pending_sync")
    make_due(mapped)
    run_once(mapped, "w1", Ports(fake))
    confirmed = status()
    assert confirmed["status"] == "confirmed"
    assert confirmed["spoken_status"] == service.spoken_status("confirmed")


def test_status_tool_needs_review_and_tenant_scope(client, mapped):
    appointment_id = book(mapped)
    with connect(mapped) as conn:
        conn.execute("UPDATE appointments SET status='needs_review' WHERE id=%s", (appointment_id,))
    body = {"appointment_id": str(appointment_id)}
    r = client.post("/v1/tools/appointment-status", headers=tool_headers(mapped), json=body)
    assert "not yet confirmed" in r.json()["spoken_status"]
    other = tool_headers(mapped, foreign_clinic(mapped))
    assert client.post("/v1/tools/appointment-status", headers=other, json=body).status_code == 404
    missing = {"appointment_id": str(uuid4())}
    r = client.post("/v1/tools/appointment-status", headers=tool_headers(mapped), json=missing)
    assert r.status_code == 404
    assert client.post("/v1/tools/appointment-status", json=body).status_code == 401
    extra = {**body, "clinic_id": str(CLINIC)}
    r = client.post("/v1/tools/appointment-status", headers=tool_headers(mapped), json=extra)
    assert r.status_code == 422


# CLI


def test_cli_worker_once_with_fake_calendar(mapped, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", mapped)
    get_settings.cache_clear()
    appointment_id = book(mapped)
    try:
        cli.main(["worker", "--once", "--calendar", "fake", "--worker-id", "cli-test"])
    finally:
        get_settings.cache_clear()
    captured = capsys.readouterr()
    assert "processed 1 job(s)" in captured.out
    assert "not written to any real calendar" in captured.err
    row = appointment(mapped, appointment_id)
    assert row["status"] == "confirmed"
    assert job_for(mapped, appointment_id)["lease_owner"] == "cli-test"


def test_cli_worker_refuses_without_provider(database, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database)
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit, match="No calendar provider configured"):
            cli.main(["worker", "--once"])
    finally:
        get_settings.cache_clear()


# Helpers for staff and foreign tenants


def staff_headers(client, database, role="owner", clinic=CLINIC):
    email = f"{role}-{uuid4().hex[:8]}@example.test"
    with connect(database) as conn:
        staff.create_user(conn, clinic, email, role, PASSWORD)
    r = client.post("/v1/staff/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def foreign_clinic(database):
    clinic = uuid4()
    with connect(database) as conn:
        conn.execute("INSERT INTO clinics (id,name) VALUES (%s,'Synthetic Other')", (clinic,))
    return clinic
