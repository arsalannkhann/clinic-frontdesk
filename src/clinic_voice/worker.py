"""Durable outbox worker (T4).

Jobs are outbox rows. A worker claims due jobs in one short transaction (FOR UPDATE SKIP
LOCKED), which sets a lease (owner + expiry) and increments attempts, and commits before any
provider I/O. Every later state change is fenced on (id, lease_owner, attempts, processing):
if another worker reclaimed the job after this one's lease expired, the fence matches nothing
and the stale worker must stop. Handlers are idempotent so a reclaimed job reaches the same
outcome: calendar events have deterministic ids and an ambiguous write is recovered by reading
the id back, never by inserting again.

Provider I/O always runs with the connection idle (no open transaction). Errors recorded in
`last_error` are fixed short codes from this module, never provider text. Workers act across
tenants, but each job carries its clinic_id and every statement filters on it.

Registered kinds: calendar.create, calendar.cancel, calendar.refresh_busy. Kinds reserved for
later tasks (notifications, reminders, webhooks, provisioning) have no handler yet and are
dead-lettered with 'unknown_kind' if enqueued; nothing enqueues them today.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from psycopg.pq import TransactionStatus

from clinic_voice.calendar_sync import refresh_external_busy
from clinic_voice.db import connect
from clinic_voice.providers.calendar import (
    CalendarEvent,
    CalendarPort,
    EventAlreadyExists,
    ProviderError,
    ProviderPermanent,
    ProviderRetryable,
    ProviderTimeout,
    event_id_for,
    event_summary,
)

log = logging.getLogger(__name__)

DEFAULT_LEASE = timedelta(minutes=2)
BASE_DELAY = timedelta(seconds=30)
MAX_DELAY = timedelta(hours=1)
# Upper bound on a provider-requested Retry-After, so a bogus value cannot park a job forever.
MAX_PROVIDER_DELAY = timedelta(days=1)


@dataclass(frozen=True)
class Job:
    id: UUID
    clinic_id: UUID
    appointment_id: UUID | None
    appointment_version: int
    kind: str
    attempts: int
    max_attempts: int
    payload: dict[str, Any]
    lease_owner: str


@dataclass(frozen=True)
class Ports:
    calendar: CalendarPort


def backoff(attempts: int) -> timedelta:
    """min(30 s * 2^(attempts-1), 1 h), jitter-free."""
    # Clamp the exponent: 2**7 * 30 s already exceeds the cap, and large exponents overflow.
    return min(BASE_DELAY * 2 ** min(max(attempts - 1, 0), 8), MAX_DELAY)


def _job(row) -> Job:
    return Job(
        id=row["id"],
        clinic_id=row["clinic_id"],
        appointment_id=row["appointment_id"],
        appointment_version=row["appointment_version"],
        kind=row["kind"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        payload=row["payload"],
        lease_owner=row["lease_owner"],
    )


def _require_idle(conn):
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise RuntimeError("provider I/O attempted inside an open transaction")


def claim(conn, worker_id, limit=10, lease=DEFAULT_LEASE, kinds=None) -> list[Job]:
    """Lease up to `limit` due jobs (pending and due, or processing with an expired lease)
    to worker_id and commit. Uses the database clock throughout."""
    _require_idle(conn)
    rows = conn.execute(
        """WITH due AS (
            SELECT id FROM outbox
            WHERE ((status = 'pending' AND available_at <= now())
                OR (status = 'processing' AND lease_expires_at < now()))
            AND (%(kinds)s::text[] IS NULL OR kind = ANY(%(kinds)s::text[]))
            ORDER BY available_at
            LIMIT %(limit)s
            FOR UPDATE SKIP LOCKED
        )
        UPDATE outbox o SET status = 'processing', attempts = o.attempts + 1,
            lease_owner = %(worker)s, lease_expires_at = now() + %(lease)s, updated_at = now()
        FROM due WHERE o.id = due.id
        RETURNING o.*""",
        {
            "kinds": list(kinds) if kinds is not None else None,
            "limit": limit,
            "worker": worker_id,
            "lease": lease,
        },
    ).fetchall()
    conn.commit()
    rows.sort(key=lambda r: (r["available_at"], r["id"]))
    return [_job(r) for r in rows]


FENCE = "id=%s AND clinic_id=%s AND lease_owner=%s AND attempts=%s AND status='processing'"


def _fence(job):
    return (job.id, job.clinic_id, job.lease_owner, job.attempts)


def _mark_done(conn, job) -> bool:
    """Fenced update inside the caller's transaction; does not commit."""
    row = conn.execute(
        "UPDATE outbox SET status='done', lease_expires_at=NULL, last_error=NULL,"
        f" updated_at=now() WHERE {FENCE} RETURNING id",  # noqa: S608
        _fence(job),
    ).fetchone()
    return row is not None


def complete(conn, job) -> bool:
    """Mark the job done. False means the lease was lost: do no further side effects."""
    done = _mark_done(conn, job)
    conn.commit()
    return done


def fail_dead(conn, job, error_code) -> bool:
    """Dead-letter the job. A dead calendar job moves its appointment to needs_review, but only
    if it is still pending_sync at the job's version, in the same transaction."""
    row = conn.execute(
        "UPDATE outbox SET status='dead', lease_expires_at=NULL, last_error=%s, updated_at=now()"
        f" WHERE {FENCE} RETURNING id",  # noqa: S608
        (error_code, *_fence(job)),
    ).fetchone()
    if row is None:
        conn.rollback()
        return False
    if job.kind.startswith("calendar.") and job.appointment_id is not None:
        conn.execute(
            "UPDATE appointments SET status='needs_review', updated_at=now()"
            " WHERE clinic_id=%s AND id=%s AND status='pending_sync' AND version=%s",
            (job.clinic_id, job.appointment_id, job.appointment_version),
        )
    conn.commit()
    log.warning("job dead-lettered id=%s kind=%s code=%s", job.id, job.kind, error_code)
    return True


def retry(conn, job, error_code, delay=None) -> bool:
    """Return the job to pending after a delay (backoff by attempts unless given). Once
    attempts reach max_attempts the job is dead-lettered instead."""
    if job.attempts >= job.max_attempts:
        return fail_dead(conn, job, error_code)
    if delay is None:
        delay = backoff(job.attempts)
    delay = min(max(delay, timedelta(0)), MAX_PROVIDER_DELAY)
    row = conn.execute(
        "UPDATE outbox SET status='pending', lease_expires_at=NULL, last_error=%s,"
        f" available_at=now() + %s, updated_at=now() WHERE {FENCE} RETURNING id",  # noqa: S608
        (error_code, delay, *_fence(job)),
    ).fetchone()
    conn.commit()
    if row is not None:
        log.info("job retry scheduled id=%s kind=%s code=%s", job.id, job.kind, error_code)
    return row is not None


def _provider_failure(conn, job, error: ProviderError) -> bool:
    """Map a provider error to retry/dead. Codes are fixed here, never taken from the error."""
    if isinstance(error, ProviderTimeout):
        return retry(conn, job, "timeout")
    if isinstance(error, ProviderRetryable):
        delay = None if error.retry_after is None else timedelta(seconds=error.retry_after)
        return retry(conn, job, "retryable", delay)
    if isinstance(error, ProviderPermanent):
        return fail_dead(conn, job, "provider_permanent")
    return retry(conn, job, "provider_error")


# calendar.create


def _load_appointment(conn, job):
    """Short read transaction: appointment, its doctor's calendars and the clinic zone."""
    row = conn.execute(
        """SELECT a.status, a.version, a.starts_at, a.ends_at, a.blocks_until,
        a.calendar_event_id, d.booking_calendar_id, d.busy_calendar_ids, c.timezone
        FROM appointments a
        JOIN doctors d ON d.clinic_id=a.clinic_id AND d.id=a.doctor_id
        JOIN clinics c ON c.id=a.clinic_id
        WHERE a.clinic_id=%s AND a.id=%s""",
        (job.clinic_id, job.appointment_id),
    ).fetchone()
    conn.commit()
    return row


def _matches(event, appointment) -> bool:
    return (
        event.status == "confirmed"
        and event.start == appointment["starts_at"]
        and event.end == appointment["ends_at"]
    )


def _confirm(conn, job, event_id) -> bool:
    """Confirm the appointment and complete the job in one transaction; roll back both if the
    lease was lost (the new owner reaches the same idempotent outcome).

    If the appointment stopped being pending_sync while the event was written (for example it
    was cancelled), it is not confirmed, but the event id is still recorded so a cancel job can
    find the projection.
    """
    scope = (job.clinic_id, job.appointment_id, job.appointment_version)
    confirmed = conn.execute(
        "UPDATE appointments SET status='confirmed', calendar_event_id=%s, updated_at=now()"
        " WHERE clinic_id=%s AND id=%s AND status='pending_sync' AND version=%s RETURNING id",
        (event_id, *scope),
    ).fetchone()
    if confirmed is None:
        conn.execute(
            "UPDATE appointments SET calendar_event_id=%s, updated_at=now()"
            " WHERE clinic_id=%s AND id=%s AND version=%s AND calendar_event_id IS NULL",
            (event_id, *scope),
        )
        log.warning("appointment changed during projection id=%s", job.appointment_id)
    if not _mark_done(conn, job):
        conn.rollback()
        return False
    conn.commit()
    return True


def _verify_existing(conn, job, event, appointment, event_id) -> bool:
    if _matches(event, appointment):
        return _confirm(conn, job, event_id)
    return fail_dead(conn, job, "event_mismatch")


def handle_calendar_create(conn, job, ports) -> bool:
    appointment = _load_appointment(conn, job)
    if (
        appointment is None
        or appointment["status"] != "pending_sync"
        or appointment["version"] != job.appointment_version
    ):
        return complete(conn, job)  # Superseded or no longer pending: nothing to project.
    calendar_id = appointment["booking_calendar_id"]
    if not calendar_id:
        return fail_dead(conn, job, "calendar_unmapped")
    event_id = event_id_for(job.appointment_id, job.appointment_version)
    _require_idle(conn)
    try:
        existing = ports.calendar.get_event(calendar_id, event_id)
        if existing is not None:
            return _verify_existing(conn, job, existing, appointment, event_id)
        # Re-check external busy just before writing (ADR 0004). The booking calendar is
        # excluded: it holds this system's own projections, which the ledger already orders.
        busy_calendars = [c for c in appointment["busy_calendar_ids"] if c != calendar_id]
        if busy_calendars:
            busy = ports.calendar.free_busy(
                busy_calendars, appointment["starts_at"], appointment["blocks_until"]
            )
            if any(
                s < appointment["blocks_until"] and e > appointment["starts_at"] for s, e in busy
            ):
                return fail_dead(conn, job, "external_conflict")
        zone = ZoneInfo(appointment["timezone"])
        event = CalendarEvent(
            event_id,
            appointment["starts_at"].astimezone(zone),
            appointment["ends_at"].astimezone(zone),
            event_summary(job.appointment_id),
        )
        try:
            stored = ports.calendar.insert_event(calendar_id, event)
        except EventAlreadyExists:
            stored = ports.calendar.get_event(calendar_id, event_id)
            if stored is None:
                return retry(conn, job, "event_missing")
            return _verify_existing(conn, job, stored, appointment, event_id)
        if not _matches(stored, appointment):
            return fail_dead(conn, job, "event_mismatch")
        return _confirm(conn, job, event_id)
    except ProviderError as error:
        return _provider_failure(conn, job, error)


# calendar.cancel


def handle_calendar_cancel(conn, job, ports) -> bool:
    """Cancel the projected event idempotently.

    Event id: payload['event_id'], else the appointment's recorded calendar_event_id, else the
    deterministic id for the job's version (a create may have written it without recording
    it; cancelling a missing event is a no-op). Calendar: payload['calendar_id'], else the
    doctor's booking calendar. With no calendar and nothing recorded, nothing was projected
    and the job completes; with a recorded event but no calendar, staff must review.
    """
    row = conn.execute(
        """SELECT a.calendar_event_id, d.booking_calendar_id FROM appointments a
        JOIN doctors d ON d.clinic_id=a.clinic_id AND d.id=a.doctor_id
        WHERE a.clinic_id=%s AND a.id=%s""",
        (job.clinic_id, job.appointment_id),
    ).fetchone()
    conn.commit()
    payload = job.payload or {}
    recorded = payload.get("event_id") or (row["calendar_event_id"] if row else None)
    calendar_id = payload.get("calendar_id") or (row["booking_calendar_id"] if row else None)
    if not calendar_id:
        if recorded:
            return fail_dead(conn, job, "calendar_unmapped")
        return complete(conn, job)
    event_id = recorded or event_id_for(job.appointment_id, job.appointment_version)
    _require_idle(conn)
    try:
        ports.calendar.cancel_event(calendar_id, event_id)
    except ProviderError as error:
        return _provider_failure(conn, job, error)
    return complete(conn, job)


# calendar.refresh_busy


def handle_refresh_busy(conn, job, ports) -> bool:
    """Payload {doctor_id, start, end} (ISO 8601, offset-aware). Refreshes the advisory
    external busy cache for that doctor of the job's clinic."""
    payload = job.payload or {}
    try:
        doctor_id = UUID(str(payload["doctor_id"]))
        start = datetime.fromisoformat(payload["start"])
        end = datetime.fromisoformat(payload["end"])
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return fail_dead(conn, job, "bad_payload")
    try:
        written = refresh_external_busy(conn, ports.calendar, job.clinic_id, doctor_id, start, end)
    except ProviderError as error:
        conn.rollback()
        return _provider_failure(conn, job, error)
    except HTTPException:
        conn.rollback()
        return fail_dead(conn, job, "not_found")
    if not written:
        return complete(conn, job)  # A newer refresh already covered the window.
    # The cache writes and the completion commit together; a lost lease discards both.
    if not _mark_done(conn, job):
        conn.rollback()
        return False
    conn.commit()
    return True


Handler = Callable[[Any, Job, Ports], bool]

HANDLERS: dict[str, Handler] = {
    "calendar.create": handle_calendar_create,
    "calendar.cancel": handle_calendar_cancel,
    "calendar.refresh_busy": handle_refresh_busy,
}


def process(conn, job, ports) -> bool:
    """Run one claimed job. Never raises for handler failures: unexpected exceptions retry
    the job with code 'internal'. Returns the handler's fence result."""
    handler = HANDLERS.get(job.kind)
    if handler is None:
        return fail_dead(conn, job, "unknown_kind")
    try:
        return handler(conn, job, ports)
    except Exception as error:
        # Log the type only: exception text may carry provider or database detail.
        log.error(
            "job handler failed id=%s kind=%s error=%s", job.id, job.kind, type(error).__name__
        )
        if not conn.closed:
            conn.rollback()
            return retry(conn, job, "internal")
        return False


def run_once(url, worker_id, ports, limit=10, lease=DEFAULT_LEASE, kinds=None) -> int:
    """Claim and process one batch; returns how many jobs were claimed."""
    with connect(url) as conn:
        jobs = claim(conn, worker_id, limit, lease, kinds)
        for job in jobs:
            try:
                process(conn, job, ports)
            except Exception as error:
                # Recording the retry itself failed (e.g. the database went away). The lease
                # expires and another claim picks the job up.
                log.error(
                    "job bookkeeping failed id=%s kind=%s error=%s",
                    job.id,
                    job.kind,
                    type(error).__name__,
                )
                if conn.closed:
                    break
                conn.rollback()
    return len(jobs)
