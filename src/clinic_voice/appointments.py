"""Appointment changes (cancel, reschedule) and staff appointment views (T7).

A change ends an appointment: status becomes 'cancelled' and its version is bumped, so any
calendar.create job of the old version finds itself superseded and withdraws (or skips) its
event (handlers/calendar.py), and later reminder jobs keyed to a version no-op. A
calendar.cancel job for the old version's event is enqueued in the same transaction. The
ledger is authoritative: a cancelled appointment frees its slot at once, and the calendar
projection follows durably (no intermediate 'cancellation_pending' status, ADR 0006).

A reschedule is a new appointment (new id, version chain, booking reference) linked by
rescheduled_from, for the same patient, doctor and service. The old appointment is
cancelled first inside the same transaction so the new slot may overlap the old hold; any
failure rolls back both. Each appointment is changed at most once (appointment_changes is
unique per appointment), which with the row lock makes concurrent changes resolve to one
outcome.

Lock order (locks.py): reschedule takes the doctor row (3) before the appointment row (5);
cancel takes only the appointment row (5); staff resolve then takes the outbox row (6).
"""

from uuid import uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from clinic_voice.audit import audit
from clinic_voice.domain import ACTIVE_STATUSES
from clinic_voice.patients import decrypt_field

NOT_FOUND = "Not found"


def lock_appointment(conn, clinic_id, appointment_id):
    return conn.execute(
        "SELECT * FROM appointments WHERE clinic_id=%s AND id=%s FOR UPDATE",
        (clinic_id, appointment_id),
    ).fetchone()


def cancel_locked(conn, row):
    """Cancel a locked, active appointment row in the caller's transaction: status
    'cancelled', version + 1, and a calendar.cancel job for the old version's event. Returns
    the updated row."""
    clinic_id = row["clinic_id"]
    updated = conn.execute(
        "UPDATE appointments SET status='cancelled', version=version + 1, updated_at=now()"
        " WHERE clinic_id=%s AND id=%s AND version=%s AND status = ANY(%s) RETURNING *",
        (clinic_id, row["id"], row["version"], list(ACTIVE_STATUSES)),
    ).fetchone()
    if updated is None:
        raise RuntimeError("appointment changed while locked")
    # The event of the old version: name it explicitly when the worker recorded it. With no
    # recorded event the handler derives the deterministic id and the create job's intent.
    payload = {}
    if row["calendar_event_id"]:
        payload["event_id"] = row["calendar_event_id"]
        if row["calendar_id"]:
            payload["calendar_id"] = row["calendar_id"]
        if row["calendar_account_id"]:
            payload["calendar_account_id"] = str(row["calendar_account_id"])
    conn.execute(
        "INSERT INTO outbox (id,clinic_id,appointment_id,kind,appointment_version,payload)"
        " VALUES (%s,%s,%s,'calendar.cancel',%s,%s)",
        (uuid4(), clinic_id, row["id"], row["version"], Jsonb(payload)),
    )
    return updated


def record_change(
    conn,
    row,
    action,
    source,
    *,
    new_appointment_id=None,
    call_session_id=None,
    staff_user_id=None,
    key=None,
):
    conn.execute(
        """INSERT INTO appointment_changes (id,clinic_id,appointment_id,action,from_version,
        new_appointment_id,source,call_session_id,staff_user_id,idempotency_key)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            uuid4(),
            row["clinic_id"],
            row["id"],
            action,
            row["version"],
            new_appointment_id,
            source,
            call_session_id,
            staff_user_id,
            key,
        ),
    )


# Staff


STAFF_COLUMNS = (
    "a.id, a.booking_reference, a.doctor_id, a.service_id, a.starts_at, a.ends_at, a.status,"
    " a.rescheduled_from, a.created_at"
)


def _out(row, keyring=None, with_patient=False):
    result = {
        key: row[key]
        for key in (
            "id",
            "booking_reference",
            "doctor_id",
            "service_id",
            "starts_at",
            "ends_at",
            "status",
            "rescheduled_from",
            "created_at",
        )
    }
    result["patient_name"] = None
    result["phone_last2"] = None
    if with_patient and keyring is not None and row.get("patient_id") is not None:
        clinic_id, patient_id = row["clinic_id"], row["patient_id"]
        result["patient_name"] = decrypt_field(
            keyring, clinic_id, patient_id, "name", row["name_key_id"], row["name_ciphertext"]
        )
        phone = decrypt_field(
            keyring, clinic_id, patient_id, "phone", row["phone_key_id"], row["phone_ciphertext"]
        )
        result["phone_last2"] = phone[-2:] if phone else None
    return result


def list_appointments(conn, staff, keyring, starts, ends, status, limit):
    """Appointments overlapping the optional [starts, ends) range, by start. Names and the
    last two phone digits only for owner/admin (and only with a keyring). Every appointment
    whose patient details were decrypted is audited as 'appointment.patient_details_viewed'
    with its id as target."""
    clinic_id = staff["clinic_id"]
    with_patient = staff["role"] in ("owner", "admin")
    rows = conn.execute(
        f"""SELECT {STAFF_COLUMNS}, a.clinic_id, a.patient_id, p.name_ciphertext,
        p.name_key_id, p.phone_ciphertext, p.phone_key_id
        FROM appointments a
        LEFT JOIN patients p ON p.clinic_id=a.clinic_id AND p.id=a.patient_id
        WHERE a.clinic_id=%(clinic)s
        AND (%(ends)s::timestamptz IS NULL OR a.starts_at < %(ends)s)
        AND (%(starts)s::timestamptz IS NULL OR a.ends_at > %(starts)s)
        AND (%(status)s::text IS NULL OR a.status = %(status)s)
        ORDER BY a.starts_at, a.id LIMIT %(limit)s""",  # noqa: S608
        {"clinic": clinic_id, "starts": starts, "ends": ends, "status": status, "limit": limit},
    ).fetchall()
    result = [_out(row, keyring, with_patient) for row in rows]
    if with_patient and keyring is not None:
        for row in rows:
            if row["patient_id"] is not None:
                audit(conn, clinic_id, staff["id"], "appointment.patient_details_viewed", row["id"])
    return result


def _staff_row(conn, clinic_id, appointment_id):
    row = conn.execute(
        f"SELECT {STAFF_COLUMNS} FROM appointments a WHERE a.clinic_id=%s AND a.id=%s",  # noqa: S608
        (clinic_id, appointment_id),
    ).fetchone()
    return _out(row)


def _locked_active(conn, clinic_id, appointment_id):
    row = lock_appointment(conn, clinic_id, appointment_id)
    if row is None:
        raise HTTPException(404, NOT_FOUND)
    if row["status"] not in ACTIVE_STATUSES:
        raise HTTPException(409, "Appointment is not active")
    return row


def _staff_cancel(conn, staff, row, action):
    cancel_locked(conn, row)
    record_change(conn, row, "cancel", "staff", staff_user_id=staff["id"])
    audit(conn, staff["clinic_id"], staff["id"], action, row["id"])


def staff_cancel(conn, staff, appointment_id):
    clinic_id = staff["clinic_id"]
    row = _locked_active(conn, clinic_id, appointment_id)
    _staff_cancel(conn, staff, row, "appointment.cancelled")
    return _staff_row(conn, clinic_id, appointment_id)


def resolve(conn, staff, appointment_id, action):
    """Settle a needs_review appointment: 'cancel' cancels it like staff_cancel; 'requeue'
    requeues the dead calendar.create job of its current version through the T4 requeue
    path (which returns it to pending_sync), only when that job's failure caused the review."""
    from clinic_voice.admin import requeue_outbox
    from clinic_voice.handlers.calendar import CALENDAR_DEAD_CODES

    clinic_id = staff["clinic_id"]
    row = lock_appointment(conn, clinic_id, appointment_id)
    if row is None:
        raise HTTPException(404, NOT_FOUND)
    if row["status"] != "needs_review":
        raise HTTPException(409, "Appointment does not need review")
    if action == "cancel":
        _staff_cancel(conn, staff, row, "appointment.resolved_cancel")
        return _staff_row(conn, clinic_id, appointment_id)
    job = conn.execute(
        "SELECT id, last_error FROM outbox WHERE clinic_id=%s AND appointment_id=%s"
        " AND kind='calendar.create' AND appointment_version=%s AND status='dead'",
        (clinic_id, appointment_id, row["version"]),
    ).fetchone()
    if job is None or job["last_error"] not in CALENDAR_DEAD_CODES:
        raise HTTPException(409, "No failed calendar job to requeue; cancel instead")
    requeue_outbox(conn, staff, job["id"])
    audit(conn, clinic_id, staff["id"], "appointment.resolved_requeue", appointment_id)
    return _staff_row(conn, clinic_id, appointment_id)
