import hashlib
import json
from datetime import UTC, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from psycopg.errors import ExclusionViolation, UniqueViolation

from clinic_voice.db import connect
from clinic_voice.domain import (
    ACTIVE_STATUSES,
    candidate_slots,
    new_booking_reference,
    utc_now,
)
from clinic_voice.locks import lock_doctors


def get_context(conn, clinic_id, doctor_id, service_id):
    context = conn.execute(
        """SELECT s.duration_minutes, s.buffer_after_minutes, c.timezone, c.horizon_days,
        c.lead_time_minutes FROM services s
        JOIN clinics c ON c.id=s.clinic_id
        JOIN doctors d ON d.clinic_id=s.clinic_id AND d.id=s.doctor_id
        WHERE s.clinic_id=%s AND s.doctor_id=%s AND s.id=%s AND s.active AND d.active""",
        (clinic_id, doctor_id, service_id),
    ).fetchone()
    if not context:
        raise HTTPException(404, "Doctor or service not found")
    return context


def busy_intervals(conn, clinic_id, doctor_id, start, end):
    """Local busy time overlapping [start, end) as (start, end) pairs.

    Appointments hold the doctor until blocks_until (end plus the buffer snapshotted at
    booking); schedule exceptions apply to their doctor or, with no doctor, the whole clinic; cached
    external calendar busy time (advisory, see calendar_sync.py) also counts.
    """
    rows = conn.execute(
        """SELECT starts_at, blocks_until AS ends_at FROM appointments
        WHERE clinic_id=%s AND doctor_id=%s AND status = ANY(%s)
        AND starts_at < %s AND blocks_until > %s
        UNION ALL
        SELECT starts_at, ends_at FROM schedule_exceptions
        WHERE clinic_id=%s AND (doctor_id IS NULL OR doctor_id=%s)
        AND starts_at < %s AND ends_at > %s
        UNION ALL
        SELECT starts_at, ends_at FROM external_busy
        WHERE clinic_id=%s AND doctor_id=%s AND starts_at < %s AND ends_at > %s""",
        (
            clinic_id,
            doctor_id,
            list(ACTIVE_STATUSES),
            end,
            start,
            clinic_id,
            doctor_id,
            end,
            start,
            clinic_id,
            doctor_id,
            end,
            start,
        ),
    ).fetchall()
    return [(r["starts_at"], r["ends_at"]) for r in rows]


def slots(conn, clinic_id, doctor_id, service_id, day, extra_busy=(), context=None):
    """Available starts for one day. extra_busy adds (start, end) pairs, e.g. external
    calendar busy time; context reuses an already-fetched get_context row."""
    if context is None:
        context = get_context(conn, clinic_id, doctor_id, service_id)
    zone = ZoneInfo(context["timezone"])
    now = utc_now()
    horizon = context["horizon_days"]
    if not 0 <= (day - now.astimezone(zone).date()).days <= horizon:
        raise HTTPException(422, f"Date must be today through {horizon} days ahead")
    hours = conn.execute(
        "SELECT opens, closes FROM working_hours WHERE clinic_id=%s AND doctor_id=%s AND weekday=%s",
        (clinic_id, doctor_id, day.weekday()),
    ).fetchall()
    buffer = timedelta(minutes=context["buffer_after_minutes"])
    day_start = datetime.combine(day, time(0), zone)
    # A late slot's buffer can run past midnight into busy time that starts the next day.
    day_end = datetime.combine(day + timedelta(days=1), time(0), zone) + buffer
    busy = busy_intervals(conn, clinic_id, doctor_id, day_start, day_end)
    return candidate_slots(
        day,
        [(r["opens"], r["closes"]) for r in hours],
        context["duration_minutes"],
        zone,
        busy + list(extra_busy),
        now,
        buffer=context["buffer_after_minutes"],
        earliest=now + timedelta(minutes=context["lead_time_minutes"]),
    )


def response(row):
    return {
        "appointment_id": row["id"],
        "booking_reference": row["booking_reference"],
        "status": row["status"],
        "starts_at": row["starts_at"],
        "ends_at": row["ends_at"],
        "spoken_confirmation": "Your request is reserved locally and awaiting calendar confirmation. It is not yet confirmed.",
    }


SPOKEN_STATUS = {
    "confirmed": "Your appointment is confirmed.",
    "pending_sync": (
        "Your request is reserved and still awaiting calendar confirmation."
        " It is not yet confirmed."
    ),
    "needs_review": (
        "Your request needs a check by clinic staff, who will follow up with you."
        " It is not yet confirmed."
    ),
    "cancelled": "This appointment has been cancelled.",
}
UNKNOWN_SPOKEN_STATUS = "I cannot see the status of this request right now. It is not confirmed."


def spoken_status(status):
    """The only place status becomes caller-facing wording. Only a persisted 'confirmed'
    (set by the worker after the calendar event was verified) may be spoken as confirmed."""
    return SPOKEN_STATUS.get(status, UNKNOWN_SPOKEN_STATUS)


def appointment_status(conn, clinic_id, appointment_id):
    row = conn.execute(
        "SELECT id, status, starts_at, ends_at FROM appointments WHERE clinic_id=%s AND id=%s",
        (clinic_id, appointment_id),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Appointment not found")
    return {
        "appointment_id": row["id"],
        "status": row["status"],
        "starts_at": row["starts_at"],
        "ends_at": row["ends_at"],
        "spoken_status": spoken_status(row["status"]),
    }


def replay(conn, clinic_id, key, fingerprint):
    row = conn.execute(
        "SELECT * FROM appointments WHERE clinic_id=%s AND idempotency_key=%s", (clinic_id, key)
    ).fetchone()
    if row:
        if row["request_hash"] != fingerprint:
            raise HTTPException(409, "Idempotency key reused with different request")
        return response(row)


SESSION_LIMIT_MESSAGE = (
    "I cannot hold any more times on this call. Clinic staff will help with further bookings."
)


def fingerprint(canonical: dict) -> str:
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


REFERENCE_CONSTRAINT = "appointments_booking_reference_key"
REFERENCE_ATTEMPTS = 5
INSERT_APPOINTMENT = """INSERT INTO appointments (id,clinic_id,doctor_id,service_id,starts_at,
    ends_at,blocks_until,status,idempotency_key,request_hash,call_session_id,patient_id,
    rescheduled_from,booking_reference)
    VALUES (%s,%s,%s,%s,%s,%s,%s,'pending_sync',%s,%s,%s,%s,%s,%s) RETURNING *"""


def _insert_appointment(conn, values):
    """Insert with a fresh CSPRNG booking reference, retrying a reference collision inside a
    savepoint. Any other constraint violation propagates (the caller's transaction fails)."""
    for _ in range(REFERENCE_ATTEMPTS):
        try:
            with conn.transaction():
                return conn.execute(
                    INSERT_APPOINTMENT, (*values, new_booking_reference())
                ).fetchone()
        except UniqueViolation as error:
            if error.diag.constraint_name != REFERENCE_CONSTRAINT:
                raise
    raise HTTPException(503, "Could not allocate a booking reference; retry the same action")


def insert_reservation(
    conn,
    clinic_id,
    doctor_id,
    service_id,
    starts_at,
    key,
    request_hash,
    *,
    call_session_id=None,
    patient_id=None,
    rescheduled_from=None,
    attach_patient=None,
):
    """Check the slot and insert a pending_sync appointment plus its calendar.create job in
    the caller's transaction. The caller must hold the doctor row lock (locks.py, level 3).
    attach_patient(conn, starts_at) -> patient id | None runs after the slot check."""
    context = get_context(conn, clinic_id, doctor_id, service_id)
    day = starts_at.astimezone(ZoneInfo(context["timezone"])).date()
    available = slots(conn, clinic_id, doctor_id, service_id, day, context=context)
    # Compare instants, never zone-local wall times.
    if starts_at.astimezone(UTC) not in {s.astimezone(UTC) for s in available}:
        raise HTTPException(409, "Slot unavailable or outside working hours")
    if attach_patient is not None:
        patient_id = attach_patient(conn, starts_at)
    appointment_id = uuid4()
    ends_at = starts_at + timedelta(minutes=context["duration_minutes"])
    row = _insert_appointment(
        conn,
        (
            appointment_id,
            clinic_id,
            doctor_id,
            service_id,
            starts_at,
            ends_at,
            ends_at + timedelta(minutes=context["buffer_after_minutes"]),
            key,
            request_hash,
            call_session_id,
            patient_id,
            rescheduled_from,
        ),
    )
    conn.execute(
        "INSERT INTO outbox (id,clinic_id,appointment_id,kind) VALUES (%s,%s,%s,'calendar.create')",
        (uuid4(), clinic_id, appointment_id),
    )
    return row


def reserve(
    url,
    clinic_id,
    request,
    key,
    call_session_id=None,
    max_session_active=None,
    attach_patient=None,
):
    """Reserve a slot idempotently. call_session_id links the appointment to the Bolna call
    session that created it (the gateway derives `key` from that session's execution id).
    max_session_active caps that session's pending_sync/confirmed appointments (409 beyond
    it); replays are never capped. attach_patient (gateway only) creates or attaches the
    patient record inside the reservation transaction; a replay never calls it."""
    canonical = request.model_dump(mode="json")
    # Normalize offset representations so the same instant retries consistently.
    canonical["starts_at"] = request.starts_at.astimezone(UTC).isoformat()
    request_hash = fingerprint(canonical)
    try:
        with connect(url) as conn:
            # Serialize the same logical action even if the winner commits between reads.
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"{clinic_id}:{key}",)
            )
            previous = replay(conn, clinic_id, key, request_hash)
            if previous:
                return previous
            if call_session_id is not None and max_session_active is not None:
                # Lock order 1a (locks.py): serializes concurrent distinct reservations of
                # one session so the cap holds.
                conn.execute(
                    "SELECT id FROM call_sessions WHERE clinic_id=%s AND id=%s FOR NO KEY UPDATE",
                    (clinic_id, call_session_id),
                )
                held = conn.execute(
                    """SELECT count(*) AS n FROM appointments WHERE clinic_id=%s
                    AND call_session_id=%s AND status IN ('pending_sync', 'confirmed')""",
                    (clinic_id, call_session_id),
                ).fetchone()["n"]
                if held >= max_session_active:
                    raise HTTPException(409, SESSION_LIMIT_MESSAGE)
            # Short DB-only lock orders competing writes for the same doctor (see locks.py).
            # GiST exclusion alone can deadlock simultaneous overlapping inserts.
            lock_doctors(conn, clinic_id, [request.doctor_id])
            row = insert_reservation(
                conn,
                clinic_id,
                request.doctor_id,
                request.service_id,
                request.starts_at,
                key,
                request_hash,
                call_session_id=call_session_id,
                attach_patient=attach_patient,
            )
            result = response(row)
        return result
    except (ExclusionViolation, UniqueViolation):
        # A competing retry may have won either constraint. Read its committed result.
        with connect(url) as conn:
            previous = replay(conn, clinic_id, key, request_hash)
            if previous:
                return previous
        raise HTTPException(409, "Slot unavailable") from None
