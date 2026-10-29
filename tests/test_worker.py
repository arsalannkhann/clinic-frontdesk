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

from clinic_voice import cli, credentials, outbox, service, staff
from clinic_voice.api import app
from clinic_voice.cli import DEMO_CLINIC_ID as CLINIC
from clinic_voice.cli import DOCTOR_ID, SERVICE_ID
from clinic_voice.config import get_settings
from clinic_voice.db import connect
from clinic_voice.handlers import HANDLERS
from clinic_voice.outbox import (
    Handler,
    Ports,
    check_ports,
    claim,
    complete,
    fail_dead,
    release,
    renew,
    retry,
    run_batch,
    run_loop,
    run_once,
)
from clinic_voice.providers.calendar import (
    CalendarEvent,
    EventAlreadyExists,
    FakeCalendar,
    ProviderPermanent,
    ProviderRetryable,
    event_id_for,
)
from clinic_voice.schemas import ReservationRequest

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
    assert [outbox.backoff(n) for n in (1, 2, 3)] == [timedelta(seconds=s) for s in (30, 60, 120)]
    assert outbox.backoff(8) == timedelta(hours=1)
    assert outbox.backoff(50) == timedelta(hours=1)


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
    # Attempts keep growing (fences rely on it); the budget is extended instead.
    assert (r.json()["status"], r.json()["attempts"], r.json()["max_attempts"]) == (
        "pending",
        2,
        10,
    )
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
    job_id = insert_job(mapped, "reminder.schedule", appointment_id)  # Reserved, no handler.
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

    monkeypatch.setitem(HANDLERS, "calendar.create", Handler(boom))
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


@pytest.mark.parametrize("cancel_fails", [False, True])
def test_cancelled_during_projection_withdraws_event(mapped, monkeypatch, cancel_fails):
    fake = FakeCalendar()
    appointment_id = book(mapped)
    event_id = event_id_for(appointment_id, 1)
    real_insert = fake.insert_event

    def insert_then_cancel_locally(calendar_id, event):
        stored = real_insert(calendar_id, event)
        with connect(mapped) as conn:  # The patient cancels while the write is in flight.
            conn.execute(
                "UPDATE appointments SET status='cancelled' WHERE id=%s", (appointment_id,)
            )
        return stored

    monkeypatch.setattr(fake, "insert_event", insert_then_cancel_locally)
    if cancel_fails:
        fake.fail_next(1, ProviderRetryable, op="cancel_event")
    run_once(mapped, "w1", Ports(fake))
    if cancel_fails:
        job = job_for(mapped, appointment_id)
        assert (job["status"], job["last_error"]) == ("pending", "retryable")
        assert fake.get_event(CAL, event_id).status == "confirmed"
        make_due(mapped)
        run_once(mapped, "w1", Ports(fake))  # Superseded on load: withdraw again.
    assert fake.get_event(CAL, event_id).status == "cancelled"
    assert fake.insert_count(event_id) == 1
    assert appointment(mapped, appointment_id)["status"] == "cancelled"
    assert job_for(mapped, appointment_id)["status"] == "done"


def test_reschedule_order_cancel_old_version_keeps_new_event(mapped):
    fake = FakeCalendar()
    appointment_id = book(mapped)
    run_once(mapped, "w1", Ports(fake))
    v1, v2 = event_id_for(appointment_id, 1), event_id_for(appointment_id, 2)
    with connect(mapped) as conn:  # A reschedule bumps the version and enqueues both jobs.
        conn.execute(
            "UPDATE appointments SET version=2, status='pending_sync' WHERE id=%s",
            (appointment_id,),
        )
        conn.execute(
            "INSERT INTO outbox (id,clinic_id,appointment_id,kind,appointment_version)"
            " VALUES (%s,%s,%s,'calendar.create',2),"
            " (%s,%s,%s,'calendar.cancel',1)",
            (uuid4(), CLINIC, appointment_id, uuid4(), CLINIC, appointment_id),
        )
    run_once(mapped, "w1", Ports(fake), kinds=["calendar.create"])  # create(v2) first.
    assert appointment(mapped, appointment_id)["calendar_event_id"] == v2
    run_once(mapped, "w1", Ports(fake), kinds=["calendar.cancel"])
    assert fake.get_event(CAL, v1).status == "cancelled"
    assert fake.get_event(CAL, v2).status == "confirmed"
    row = appointment(mapped, appointment_id)
    assert (row["status"], row["calendar_event_id"]) == ("confirmed", v2)


def test_concurrent_insert_already_exists_is_verified(mapped):
    """B reads nothing, A's insert lands, B's insert collides: B reads back and confirms."""
    fake = FakeCalendar()
    appointment_id = book(mapped)
    real_insert = fake.insert_event

    def other_worker_wins(calendar_id, event):
        real_insert(calendar_id, event)  # A's write lands first.
        raise EventAlreadyExists()

    fake.insert_event = other_worker_wins
    run_once(mapped, "w1", Ports(fake))
    assert_confirmed_once(mapped, fake, appointment_id)
    assert [c[0] for c in fake.calls][-2:] == ["insert_event", "get_event"]


def test_past_appointment_dead_letters_and_cannot_be_requeued(client, mapped):
    fake = FakeCalendar()
    appointment_id = book(mapped)
    with connect(mapped) as conn:
        conn.execute(
            "UPDATE appointments SET starts_at=starts_at - interval '20 years',"
            " ends_at=ends_at - interval '20 years', blocks_until=blocks_until - interval '20 years'"
            " WHERE id=%s",
            (appointment_id,),
        )
    run_once(mapped, "w1", Ports(fake))
    job = job_for(mapped, appointment_id)
    assert (job["status"], job["last_error"]) == ("dead", "appointment_past")
    assert appointment(mapped, appointment_id)["status"] == "needs_review"
    assert ops(fake, "insert_event") == []
    r = client.post(f"/v1/admin/outbox/{job['id']}/requeue", headers=staff_headers(client, mapped))
    assert r.status_code == 409


def test_requeue_keeps_review_not_caused_by_job(client, mapped):
    appointment_id = book(mapped)
    with connect(mapped) as conn:
        conn.execute(
            "UPDATE outbox SET status='dead', last_error='unknown_kind', attempts=1"
            " WHERE appointment_id=%s",
            (appointment_id,),
        )
        conn.execute("UPDATE appointments SET status='needs_review' WHERE id=%s", (appointment_id,))
    job = job_for(mapped, appointment_id)
    r = client.post(f"/v1/admin/outbox/{job['id']}/requeue", headers=staff_headers(client, mapped))
    assert r.status_code == 200
    assert appointment(mapped, appointment_id)["status"] == "needs_review"


# Leases


def test_exhausted_expired_lease_is_dead_lettered_not_reclaimed(mapped):
    appointment_id = book(mapped)
    with connect(mapped) as conn:
        conn.execute("UPDATE outbox SET max_attempts=1 WHERE appointment_id=%s", (appointment_id,))
        conn.commit()
        [job] = claim(conn, "w1")
    expire_leases(mapped)
    with connect(mapped) as conn:
        assert claim(conn, "w2") == []
    row = job_for(mapped, appointment_id)
    assert (row["status"], row["last_error"], row["attempts"]) == ("dead", "lease_expired", 1)
    assert appointment(mapped, appointment_id)["status"] == "needs_review"
    with connect(mapped) as conn:
        assert complete(conn, job) is False


def test_lease_renewed_before_start_and_lost_job_skipped(mapped, monkeypatch):
    fake = FakeCalendar()
    first, second = book(mapped, 10), book(mapped, 11)
    real = HANDLERS["calendar.create"]
    stolen = []

    def run(conn, job, ports):
        if not stolen:  # While job 1 runs, job 2's lease expires and w2 reclaims it.
            with connect(mapped) as other:
                other.execute(
                    "UPDATE outbox SET lease_expires_at = now() - interval '1 second'"
                    " WHERE status='processing' AND id<>%s",
                    (job.id,),
                )
                other.commit()
                stolen.extend(claim(other, "w2"))
        return real.run(conn, job, ports)

    monkeypatch.setitem(HANDLERS, "calendar.create", Handler(run, real.on_dead))
    with connect(mapped) as conn:
        assert run_batch(conn, "w1", Ports(fake), limit=2) == 2
    [taken] = stolen
    assert taken.appointment_id == second
    assert appointment(mapped, first)["status"] == "confirmed"
    row = job_for(mapped, second)
    assert (row["status"], row["lease_owner"]) == ("processing", "w2")
    assert fake.insert_count(event_id_for(second, 1)) == 0


def test_renew_extends_and_is_fenced(mapped):
    book(mapped)
    with connect(mapped) as conn:
        [job] = claim(conn, "w1", lease=timedelta(seconds=5))
        before = job_for(mapped, job.appointment_id)["lease_expires_at"]
        longer = outbox.Job(**{**job.__dict__, "lease": timedelta(minutes=10)})
        assert renew(conn, longer) is True
        assert job_for(mapped, job.appointment_id)["lease_expires_at"] > before
        stale = outbox.Job(**{**job.__dict__, "lease_owner": "someone-else"})
        assert renew(conn, stale) is False


def test_check_ports_requires_short_declared_timeouts():
    def adapter(**attrs):
        return type("Adapter", (), attrs)()

    lease = timedelta(minutes=2)
    check_ports(Ports(FakeCalendar()), lease)  # The in-process fake is explicitly allowed.
    check_ports(Ports(adapter(timeout_seconds=10)), lease)
    for bad in (adapter(), adapter(timeout_seconds=None), adapter(timeout_seconds=0)):
        with pytest.raises(ValueError, match="must declare"):
            check_ports(Ports(bad), lease)
    with pytest.raises(ValueError, match="too long"):
        check_ports(Ports(adapter(timeout_seconds=60)), lease)
    with pytest.raises(ValueError):
        run_once("postgresql://unused", "w1", Ports(adapter(timeout_seconds=90)))


def test_stale_worker_late_insert_is_withdrawn(mapped):
    """w1 loses its lease mid-insert; the appointment is cancelled; w2 reclaims and withdraws
    (a no-op, nothing written yet); then w1's late insert lands. w1 must still cancel it."""
    fake = FakeCalendar()
    appointment_id = book(mapped)
    event_id = event_id_for(appointment_id, 1)
    real_insert = fake.insert_event
    seen = []

    def slow_insert(calendar_id, event):
        if not seen:
            seen.append(calendar_id)
            with connect(mapped) as conn:
                conn.execute(
                    "UPDATE appointments SET status='cancelled' WHERE id=%s", (appointment_id,)
                )
            expire_leases(mapped)
            assert run_once(mapped, "w2", Ports(fake)) == 1
            assert job_for(mapped, appointment_id)["status"] == "done"
            assert fake.get_event(CAL, event_id) is None  # w2's withdraw was a no-op.
        return real_insert(calendar_id, event)

    fake.insert_event = slow_insert
    run_once(mapped, "w1", Ports(fake))
    assert fake.insert_count(event_id) == 1
    assert fake.get_event(CAL, event_id).status == "cancelled"
    assert [c[0] for c in fake.calls if c[0] == "cancel_event"] == ["cancel_event"] * 2
    job = job_for(mapped, appointment_id)
    assert (job["status"], job["lease_owner"], job["attempts"]) == ("done", "w2", 2)


def test_remap_between_attempts_keeps_original_calendar(mapped):
    fake = FakeCalendar()
    fake.timeout_after_write = True
    appointment_id = book(mapped)
    event_id = event_id_for(appointment_id, 1)
    run_once(mapped, "w1", Ports(fake))
    assert job_for(mapped, appointment_id)["payload"] == {"calendar_id": CAL}
    map_doctor(mapped, booking="new-booking@example.test")  # Staff remap before the retry.
    make_due(mapped)
    run_once(mapped, "w1", Ports(fake))
    assert_confirmed_once(mapped, fake, appointment_id)
    assert appointment(mapped, appointment_id)["calendar_id"] == CAL
    assert all(c[1] != "new-booking@example.test" for c in fake.calls)
    job_id = insert_job(mapped, "calendar.cancel", appointment_id)
    run_once(mapped, "w1", Ports(fake))
    assert job_by_id(mapped, job_id)["status"] == "done"
    assert fake.get_event(CAL, event_id).status == "cancelled"


def test_cancel_uses_create_write_intent_when_unrecorded(mapped):
    fake = FakeCalendar()
    fake.timeout_after_write = True
    appointment_id = book(mapped)
    run_once(mapped, "w1", Ports(fake))  # Written but never recorded on the appointment.
    with connect(mapped) as conn:
        conn.execute("UPDATE appointments SET status='cancelled' WHERE id=%s", (appointment_id,))
        conn.execute("UPDATE outbox SET status='done' WHERE appointment_id=%s", (appointment_id,))
    map_doctor(mapped, booking="new-booking@example.test")
    insert_job(mapped, "calendar.cancel", appointment_id)
    run_once(mapped, "w1", Ports(fake))
    assert fake.get_event(CAL, event_id_for(appointment_id, 1)).status == "cancelled"


def test_refresh_busy_aborts_when_lease_lost_between_fetches(mapped):
    fake = FakeCalendar()
    fake.busy[BUSY] = [(at(10), at(11))]
    real_free_busy = fake.free_busy

    def steal_after_first(calendar_ids, start, end):
        result = real_free_busy(calendar_ids, start, end)
        if len(ops(fake, "free_busy")) == 1:
            expire_leases(mapped)
            with connect(mapped) as other:
                assert len(claim(other, "w2")) == 1
        return result

    fake.free_busy = steal_after_first
    payload = {"doctor_id": str(DOCTOR_ID), "start": at(0).isoformat(), "end": at(23).isoformat()}
    job_id = insert_job(mapped, "calendar.refresh_busy", None, payload)
    run_once(mapped, "w1", Ports(fake))
    assert len(ops(fake, "free_busy")) == 1  # Aborted before the booking calendar fetch.
    row = job_by_id(mapped, job_id)
    assert (row["status"], row["lease_owner"]) == ("processing", "w2")
    with connect(mapped) as conn:
        assert conn.execute("SELECT count(*) AS n FROM external_busy").fetchone()["n"] == 0


def test_refresh_heartbeat_false_raises_before_writing(mapped):
    from clinic_voice import calendar_sync
    from clinic_voice.errors import LeaseLost

    fake = FakeCalendar()
    with connect(mapped) as conn, pytest.raises(LeaseLost):
        calendar_sync.refresh_external_busy(
            conn, fake, CLINIC, DOCTOR_ID, at(0), at(23), heartbeat=lambda: False
        )
    assert len(ops(fake, "free_busy")) == 1


# Run loop


def test_stop_releases_unstarted_jobs(mapped, monkeypatch):
    from threading import Event

    fake = FakeCalendar()
    ids = [book(mapped, 10, 15 * i) for i in range(3)]
    stop = Event()
    real = HANDLERS["calendar.create"]

    def run(conn, job, ports):
        stop.set()  # SIGTERM arrives while the first job runs.
        return real.run(conn, job, ports)

    monkeypatch.setitem(HANDLERS, "calendar.create", Handler(run, real.on_dead))
    with connect(mapped) as conn:
        assert run_batch(conn, "w1", Ports(fake), stop=stop) == 3
    states = [job_for(mapped, i) for i in ids]
    assert [s["status"] for s in states] == ["done", "pending", "pending"]
    # attempts never decrease; the unused claim is refunded through max_attempts instead.
    assert [(s["attempts"], s["max_attempts"]) for s in states] == [(1, 8), (1, 9), (1, 9)]
    assert all(s["lease_expires_at"] is None for s in states[1:])
    with connect(mapped) as conn:
        [job] = claim(conn, "w9", limit=1)
        assert job.attempts == 2  # The released job's next claim gets a fresh fence value.
        assert release(conn, outbox.Job(**{**job.__dict__, "attempts": 7})) is False


def test_run_loop_reuses_connection_across_idle_polls(mapped):
    from threading import Event

    stop, opened, waits = Event(), [], []

    def connect_fn(url):
        opened.append(url)
        return connect(url)

    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            stop.set()

    run_loop(
        mapped, "w1", Ports(FakeCalendar()), stop, interval=0.5, connect_fn=connect_fn, wait=wait
    )
    assert (len(opened), waits) == (1, [0.5, 0.5, 0.5])


def test_run_loop_backs_off_on_database_errors():
    from threading import Event

    from psycopg import OperationalError

    stop, waits = Event(), []

    def connect_fn(url):
        raise OperationalError("synthetic")

    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 7:
            stop.set()

    run_loop(
        "postgresql://unused", "w1", Ports(FakeCalendar()), stop, connect_fn=connect_fn, wait=wait
    )
    assert waits == [1, 2, 4, 8, 16, 30, 30]


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


PRODUCTION_LIKE_URL = "postgresql://clinic:unused@127.0.0.1:1/clinic_voice"


def test_cli_fake_calendar_refused_outside_dev_databases(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", PRODUCTION_LIKE_URL)
    monkeypatch.delenv("APP_ENV", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit, match="Refusing --calendar fake"):
            cli.main(["worker", "--once", "--calendar", "fake"])
    finally:
        get_settings.cache_clear()


def test_cli_fake_calendar_allowed_with_app_env_development(monkeypatch):
    from psycopg import OperationalError

    monkeypatch.setenv("DATABASE_URL", PRODUCTION_LIKE_URL)
    monkeypatch.setenv("APP_ENV", "development")
    get_settings.cache_clear()
    try:
        # Past the safety check, the worker tries to connect (nothing listens on port 1).
        with pytest.raises(OperationalError):
            cli.main(["worker", "--once", "--calendar", "fake"])
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
