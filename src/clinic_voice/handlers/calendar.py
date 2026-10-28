"""Calendar projection handlers: calendar.create, calendar.cancel, calendar.refresh_busy.

Each handler reads in a short committed transaction, calls the provider with the connection
idle (renewing the lease between calls), then records the outcome in a fenced transaction.

Handlers are provider-agnostic: the port comes from Ports.calendar_port(conn, clinic, doctor,
account), which is a fixed adapter (FakeCalendar) or a per-account resolver (Google). The
account that wrote an event is recorded (create payload write intent, then
appointments.calendar_account_id) and later cancels and withdrawals use that account, never
the doctor's current mapping. No port (no usable account, e.g. revoked) is treated like an
unmapped calendar. A provider that rejects an account's credentials ('auth_revoked') marks
the account 'error' through Ports.account_failed.
"""

import logging
from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from clinic_voice.calendar_sync import refresh_external_busy
from clinic_voice.errors import LeaseLost, NotFound
from clinic_voice.outbox import (
    complete,
    fail_dead,
    mark_done,
    provider_failure,
    remember,
    renew,
    require_idle,
    retry,
)
from clinic_voice.providers.calendar import (
    CalendarEvent,
    EventAlreadyExists,
    ProviderError,
    ProviderPermanent,
    event_id_for,
    event_summary,
)

log = logging.getLogger("clinic_voice.worker")

# Codes a calendar.create dead-letter can carry. Requeue only reverts needs_review to
# pending_sync for a dead create job whose last_error is one of these (the review was caused
# by this job's failure, not by staff or a later reconciliation).
CALENDAR_DEAD_CODES = frozenset(
    {
        "timeout",
        "retryable",
        "provider_error",
        "provider_permanent",
        "external_conflict",
        "calendar_unmapped",
        "event_mismatch",
        "event_missing",
        "appointment_past",
        "internal",
        "lease_expired",
        "lease_lost",
    }
)


def needs_review_on_dead(conn, job):
    """A dead calendar.create leaves the caller unconfirmed: hand it to staff, but only if the
    appointment is still pending_sync at the job's version."""
    if job.appointment_id is None:
        return
    conn.execute(
        "UPDATE appointments SET status='needs_review', updated_at=now()"
        " WHERE clinic_id=%s AND id=%s AND status='pending_sync' AND version=%s",
        (job.clinic_id, job.appointment_id, job.appointment_version),
    )


# calendar.create


def _load_appointment(conn, job):
    row = conn.execute(
        """SELECT a.status, a.version, a.starts_at, a.ends_at, a.blocks_until,
        a.starts_at <= now() AS past, a.calendar_event_id, a.calendar_id, a.doctor_id,
        a.calendar_account_id,
        d.booking_calendar_id, d.busy_calendar_ids, c.timezone
        FROM appointments a
        JOIN doctors d ON d.clinic_id=a.clinic_id AND d.id=a.doctor_id
        JOIN clinics c ON c.id=a.clinic_id
        WHERE a.clinic_id=%s AND a.id=%s""",
        (job.clinic_id, job.appointment_id),
    ).fetchone()
    conn.commit()
    return row


def _superseded(appointment, job) -> bool:
    return appointment["version"] != job.appointment_version or appointment["status"] == "cancelled"


def _matches(event, appointment) -> bool:
    return (
        event.status == "confirmed"
        and event.start == appointment["starts_at"]
        and event.end == appointment["ends_at"]
    )


def _uuid(value):
    try:
        return None if value is None else UUID(str(value))
    except ValueError:
        return None


def _port(conn, job, ports, doctor_id, account_id=None):
    """((port, account_id), None), or (None, outcome) when resolution failed and the job was
    settled. With account_id, only that account may be used (dead 'calendar_unmapped' if it
    is no longer active)."""
    try:
        target = ports.calendar_port(conn, job.clinic_id, doctor_id, account_id)
    except ProviderError as error:
        return None, provider_failure(conn, job, error)
    if target is None:
        return None, fail_dead(conn, job, "calendar_unmapped")
    return target, None


def _failed(conn, job, ports, target, error) -> bool:
    """provider_failure, after flagging an account whose credentials the provider rejected."""
    if isinstance(error, ProviderPermanent) and error.code == "auth_revoked":
        ports.account_failed(conn, job.clinic_id, target[1])
    return provider_failure(conn, job, error)


def _withdraw(conn, job, ports, target, calendar_id, event_id) -> bool:
    """Cancel this job's own event because the appointment was cancelled or rescheduled.

    The cancel runs even if this worker's lease was lost: the id is unique to the superseded
    version (versions are never reused and a cancelled appointment is never restored at the
    same version), so cancelling it cannot hurt a newer booking, and a stale worker's late
    insert must not survive. Only the bookkeeping (complete/retry) is fenced. Idempotent:
    cancelling a missing event is a no-op. A provider error retries the job; the next attempt
    sees the superseded appointment and withdraws again."""
    require_idle(conn)
    try:
        target[0].cancel_event(calendar_id, event_id)
    except ProviderError as error:
        return _failed(conn, job, ports, target, error)
    log.info("withdrew superseded event job=%s", job.id)
    return complete(conn, job)


def _confirm(conn, job, ports, target, calendar_id, event_id) -> bool:
    """Confirm the appointment and complete the job in one transaction (both roll back if the
    lease was lost; the new owner reaches the same idempotent outcome).

    If the appointment stopped being pending_sync at this version while the event was being
    written: at the same version (confirmed elsewhere or moved to review by staff) the event id
    is recorded and the job completes; if it was cancelled or rescheduled, the job withdraws
    its own event.
    """
    scope = (job.clinic_id, job.appointment_id, job.appointment_version)
    account_id = target[1]
    confirmed = conn.execute(
        "UPDATE appointments SET status='confirmed', calendar_event_id=%s, calendar_id=%s,"
        " calendar_account_id=%s, updated_at=now()"
        " WHERE clinic_id=%s AND id=%s AND status='pending_sync' AND version=%s RETURNING id",
        (event_id, calendar_id, account_id, *scope),
    ).fetchone()
    if confirmed is None:
        current = conn.execute(
            "SELECT status, version FROM appointments WHERE clinic_id=%s AND id=%s",
            scope[:2],
        ).fetchone()
        if current is None or _superseded(current, job):
            conn.rollback()
            return _withdraw(conn, job, ports, target, calendar_id, event_id)
        conn.execute(
            "UPDATE appointments SET calendar_event_id=%s, calendar_id=%s,"
            " calendar_account_id=%s, updated_at=now()"
            " WHERE clinic_id=%s AND id=%s AND version=%s AND calendar_event_id IS NULL",
            (event_id, calendar_id, account_id, *scope),
        )
    if not mark_done(conn, job):
        conn.rollback()
        return False
    conn.commit()
    return True


def _verify(conn, job, ports, target, event, appointment, calendar_id, event_id) -> bool:
    if _matches(event, appointment):
        return _confirm(conn, job, ports, target, calendar_id, event_id)
    return fail_dead(conn, job, "event_mismatch")


def create(conn, job, ports) -> bool:
    appointment = _load_appointment(conn, job)
    if appointment is None:
        return complete(conn, job)
    event_id = event_id_for(job.appointment_id, job.appointment_version)
    # Write intent: an earlier attempt records its target calendar in the payload before
    # inserting, so retries recover (or withdraw) in that calendar even if the doctor's
    # booking calendar was remapped since. Otherwise use the calendar recorded with this
    # event, else the doctor's current booking calendar.
    # The account is recorded the same way: the write intent's account, else the account
    # recorded with this event, else (nothing written yet) the doctor's current mapping.
    payload = job.payload or {}
    intended = payload.get("calendar_id")
    own_event = appointment["calendar_event_id"] == event_id
    recorded = appointment["calendar_id"] if own_event else None
    calendar_id = intended or recorded or appointment["booking_calendar_id"]
    owner = _uuid(payload.get("calendar_account_id")) or (
        appointment["calendar_account_id"] if own_event else None
    )
    if _superseded(appointment, job):
        # Only an attempt of this job that recorded write intent can have written this id.
        if intended:
            target, outcome = _port(conn, job, ports, appointment["doctor_id"], owner)
            if target is None:
                return outcome
            return _withdraw(conn, job, ports, target, intended, event_id)
        return complete(conn, job)
    if appointment["status"] != "pending_sync":
        return complete(conn, job)  # Confirmed or under review at this version.
    if not calendar_id:
        return fail_dead(conn, job, "calendar_unmapped")
    target, outcome = _port(conn, job, ports, appointment["doctor_id"], owner)
    if target is None:
        return outcome
    port = target[0]
    require_idle(conn)
    try:
        existing = port.get_event(calendar_id, event_id)
        if existing is not None:
            return _verify(conn, job, ports, target, existing, appointment, calendar_id, event_id)
        if appointment["past"]:
            return fail_dead(conn, job, "appointment_past")
        # Re-check external busy just before writing (ADR 0004), over the same hold slot
        # math uses. The booking calendar is excluded: it holds this system's own
        # projections, including events of locally cancelled appointments awaiting cancel.
        busy_calendars = [c for c in appointment["busy_calendar_ids"] if c != calendar_id]
        if busy_calendars:
            if not renew(conn, job):
                return False
            busy = port.free_busy(
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
        intent = {"calendar_id": calendar_id}
        if target[1] is not None:
            intent["calendar_account_id"] = str(target[1])
        job = remember(conn, job, **intent)  # Also renews the lease.
        if job is None:
            return False
        try:
            stored = port.insert_event(calendar_id, event)
        except EventAlreadyExists:
            # Another attempt's write landed between our read and insert: read and verify.
            if not renew(conn, job):
                return False
            stored = port.get_event(calendar_id, event_id)
            if stored is None:
                return retry(conn, job, "event_missing")
            return _verify(conn, job, ports, target, stored, appointment, calendar_id, event_id)
        return _verify(conn, job, ports, target, stored, appointment, calendar_id, event_id)
    except ProviderError as error:
        return _failed(conn, job, ports, target, error)


# calendar.cancel


def cancel(conn, job, ports) -> bool:
    """Cancel the event of the job's appointment version, idempotently.

    Event id: payload['event_id'] if given; else the appointment's recorded calendar_event_id
    only when the appointment is still at the job's version (after a reschedule it names the
    new version's event, which must survive); else event_id_for(appointment, job version).
    Calendar: payload['calendar_id']; else the calendar recorded with that event; else the
    write intent of that version's calendar.create job; else the doctor's current booking
    calendar. With no calendar and no recorded event nothing was projected and the job
    completes; with a recorded event but no calendar, staff must review.
    Account: payload['calendar_account_id']; else the account recorded with that event; else
    the create job's write-intent account; only if nothing was recorded, the doctor's
    current mapping. A recorded account that is no longer active dead-letters the job
    ('calendar_unmapped') for staff: the event may still exist.
    """
    row = conn.execute(
        """SELECT a.calendar_event_id, a.calendar_id, a.version, a.doctor_id,
        a.calendar_account_id, d.booking_calendar_id, o.payload->>'calendar_id' AS intended,
        o.payload->>'calendar_account_id' AS intended_account
        FROM appointments a
        JOIN doctors d ON d.clinic_id=a.clinic_id AND d.id=a.doctor_id
        LEFT JOIN outbox o ON o.clinic_id=a.clinic_id AND o.appointment_id=a.id
            AND o.kind='calendar.create' AND o.appointment_version=%s
        WHERE a.clinic_id=%s AND a.id=%s""",
        (job.appointment_version, job.clinic_id, job.appointment_id),
    ).fetchone()
    conn.commit()
    payload = job.payload or {}
    recorded = payload.get("event_id")
    if not recorded and row and row["version"] == job.appointment_version:
        recorded = row["calendar_event_id"]
    event_id = recorded or event_id_for(job.appointment_id, job.appointment_version)
    calendar_id = payload.get("calendar_id")
    if not calendar_id and row:
        if row["calendar_id"] and row["calendar_event_id"] == event_id:
            calendar_id = row["calendar_id"]
        else:
            calendar_id = row["intended"] or row["booking_calendar_id"]
    if not calendar_id:
        if recorded:
            return fail_dead(conn, job, "calendar_unmapped")
        return complete(conn, job)
    account_id = _uuid(payload.get("calendar_account_id"))
    if account_id is None and row:
        if row["calendar_account_id"] and row["calendar_event_id"] == event_id:
            account_id = row["calendar_account_id"]
        else:
            account_id = _uuid(row["intended_account"])
    target, outcome = _port(conn, job, ports, row["doctor_id"] if row else None, account_id)
    if target is None:
        return outcome
    require_idle(conn)
    try:
        target[0].cancel_event(calendar_id, event_id)
    except ProviderError as error:
        return _failed(conn, job, ports, target, error)
    return complete(conn, job)


# calendar.refresh_busy


def refresh_busy(conn, job, ports) -> bool:
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
    target, outcome = _port(conn, job, ports, doctor_id)
    if target is None:
        return outcome
    try:
        written = refresh_external_busy(
            conn,
            target[0],
            job.clinic_id,
            doctor_id,
            start,
            end,
            heartbeat=lambda: renew(conn, job),
        )
    except LeaseLost:
        conn.rollback()
        return retry(conn, job, "lease_lost")  # Fenced: normally False, the new owner continues.
    except ProviderError as error:
        conn.rollback()
        return _failed(conn, job, ports, target, error)
    except NotFound:
        conn.rollback()
        return fail_dead(conn, job, "not_found")
    if not written:
        return complete(conn, job)  # A newer refresh already covered the window.
    # The cache writes and the completion commit together; a lost lease discards both.
    if not mark_done(conn, job):
        conn.rollback()
        return False
    conn.commit()
    return True


# calendar.revoke_grant


def revoke_grant(conn, job, ports) -> bool:
    """Payload {account_id}. Revoke a revoked account's refresh token at the provider, then
    clear the stored ciphertext in the same transaction that completes the job. Provider
    failures retry; after max_attempts the job is dead and the ciphertext stays, so staff
    can requeue (or remove the grant in the Google account and requeue: an already invalid
    token counts as revoked)."""
    account_id = _uuid((job.payload or {}).get("account_id"))
    if account_id is None:
        return fail_dead(conn, job, "bad_payload")
    revoker = ports.grant_revoker()
    if revoker is None:
        return fail_dead(conn, job, "no_revoker")
    require_idle(conn)
    try:
        revoker(conn, job.clinic_id, account_id)
    except ProviderError as error:
        return provider_failure(conn, job, error)
    conn.execute(
        "UPDATE calendar_accounts SET refresh_token_ciphertext=NULL, key_id=NULL"
        " WHERE clinic_id=%s AND id=%s AND status='revoked'",
        (job.clinic_id, account_id),
    )
    if not mark_done(conn, job):
        conn.rollback()
        return False
    conn.commit()
    return True
