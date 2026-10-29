"""Holidays, leave and breaks: staff-managed busy time that slot lookup honours."""

from uuid import uuid4

from fastapi import HTTPException

from clinic_voice.audit import audit
from clinic_voice.domain import ACTIVE_STATUSES
from clinic_voice.locks import lock_doctor_set, lock_doctors

COLUMNS = "id,doctor_id,kind,starts_at,ends_at,label"
MAX_REPORTED_CONFLICTS = 100


def list_exceptions(conn, clinic_id, starts=None, ends=None, limit=200):
    """Exceptions overlapping the optional half-open range [starts, ends)."""
    return conn.execute(
        f"SELECT {COLUMNS} FROM schedule_exceptions WHERE clinic_id=%s"  # noqa: S608
        " AND (%s::timestamptz IS NULL OR ends_at > %s)"
        " AND (%s::timestamptz IS NULL OR starts_at < %s)"
        " ORDER BY starts_at,id LIMIT %s",
        (clinic_id, starts, starts, ends, ends, limit),
    ).fetchall()


def create_exception(conn, staff, doctor_id, kind, starts_at, ends_at, label):
    """Insert an exception and report, but never cancel, overlapping active appointments.

    Takes the same doctor row lock(s) reserve() takes, so once this commits no reservation
    validated without it can land inside the window; anything that committed first is
    reported as conflicting. Clinic-wide exceptions also hold the doctor-set lock so a doctor
    created concurrently is either locked here or created after this commits.
    """
    clinic_id = staff["clinic_id"]
    if doctor_id is None:
        lock_doctor_set(conn, clinic_id, exclusive=True)
        lock_doctors(conn, clinic_id)
    elif not lock_doctors(conn, clinic_id, [doctor_id]):
        raise HTTPException(404, "Not found")
    exception_id = uuid4()
    row = conn.execute(
        "INSERT INTO schedule_exceptions"
        " (id,clinic_id,doctor_id,kind,starts_at,ends_at,label,created_by)"
        f" VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {COLUMNS}",  # noqa: S608
        (exception_id, clinic_id, doctor_id, kind, starts_at, ends_at, label, staff["id"]),
    ).fetchone()
    conflicts = conn.execute(
        "SELECT id, count(*) OVER () AS total FROM appointments WHERE clinic_id=%s"
        " AND (%s::uuid IS NULL OR doctor_id=%s) AND status = ANY(%s)"
        " AND starts_at < %s AND blocks_until > %s ORDER BY starts_at,id LIMIT %s",
        (
            clinic_id,
            doctor_id,
            doctor_id,
            list(ACTIVE_STATUSES),
            ends_at,
            starts_at,
            MAX_REPORTED_CONFLICTS,
        ),
    ).fetchall()
    audit(conn, clinic_id, staff["id"], "exception.created", exception_id)
    return {
        **row,
        "conflicting_appointment_ids": [r["id"] for r in conflicts],
        "conflict_count": conflicts[0]["total"] if conflicts else 0,
    }


def delete_exception(conn, staff, exception_id):
    if not conn.execute(
        "DELETE FROM schedule_exceptions WHERE clinic_id=%s AND id=%s RETURNING id",
        (staff["clinic_id"], exception_id),
    ).fetchone():
        raise HTTPException(404, "Not found")
    audit(conn, staff["clinic_id"], staff["id"], "exception.deleted", exception_id)
