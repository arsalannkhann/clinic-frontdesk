from uuid import uuid4

from fastapi import HTTPException

from clinic_voice.audit import audit
from clinic_voice.locks import lock_doctor_set, lock_doctors

NOT_FOUND = "Not found"


def _update(conn, table, columns, clinic_id, row_id, changes):
    """Update allow-listed columns of one clinic-scoped row; 404 if it is not in the clinic."""
    if not changes or not set(changes) <= set(columns):
        raise ValueError("invalid update columns")
    sets = ", ".join(f"{name}=%s" for name in changes)
    row = conn.execute(
        f"UPDATE {table} SET {sets} WHERE clinic_id=%s AND id=%s RETURNING *",  # noqa: S608
        (*changes.values(), clinic_id, row_id),
    ).fetchone()
    if not row:
        raise HTTPException(404, NOT_FOUND)
    return row


def get_clinic(conn, clinic_id):
    return conn.execute(
        "SELECT id,name,timezone,lead_time_minutes,horizon_days FROM clinics WHERE id=%s",
        (clinic_id,),
    ).fetchone()


def update_clinic(conn, staff, changes):
    """Update clinic settings.

    Timezone and horizon changes are not serialized with in-flight reservations; a booking
    validated against the old value may commit just after the change (low risk).
    """
    clinic_id = staff["clinic_id"]
    columns = ("name", "timezone", "lead_time_minutes", "horizon_days")
    if not changes or not set(changes) <= set(columns):
        raise ValueError("invalid update columns")
    row = conn.execute(
        f"UPDATE clinics SET {', '.join(f'{k}=%s' for k in changes)} WHERE id=%s"  # noqa: S608
        " RETURNING id,name,timezone,lead_time_minutes,horizon_days",
        (*changes.values(), clinic_id),
    ).fetchone()
    audit(conn, clinic_id, staff["id"], "clinic.updated", clinic_id)
    return row


DOCTOR_COLUMNS = "id,name,active,booking_calendar_id,busy_calendar_ids"


def list_doctors(conn, clinic_id):
    return conn.execute(
        f"SELECT {DOCTOR_COLUMNS} FROM doctors WHERE clinic_id=%s ORDER BY name,id",  # noqa: S608
        (clinic_id,),
    ).fetchall()


def create_doctor(conn, staff, name):
    # Shared doctor-set lock: a clinic-wide exception being created holds it exclusively.
    lock_doctor_set(conn, staff["clinic_id"], exclusive=False)
    doctor_id = uuid4()
    row = conn.execute(
        f"INSERT INTO doctors (id,clinic_id,name) VALUES (%s,%s,%s) RETURNING {DOCTOR_COLUMNS}",  # noqa: S608
        (doctor_id, staff["clinic_id"], name),
    ).fetchone()
    audit(conn, staff["clinic_id"], staff["id"], "doctor.created", doctor_id)
    return row


def update_doctor(conn, staff, doctor_id, changes):
    columns = ("name", "active", "booking_calendar_id", "busy_calendar_ids")
    row = _update(conn, "doctors", columns, staff["clinic_id"], doctor_id, changes)
    audit(conn, staff["clinic_id"], staff["id"], "doctor.updated", doctor_id)
    return {k: row[k] for k in DOCTOR_COLUMNS.split(",")}


SERVICE_COLUMNS = "id,doctor_id,name,duration_minutes,buffer_after_minutes,active"


def list_services(conn, clinic_id):
    return conn.execute(
        f"SELECT {SERVICE_COLUMNS} FROM services WHERE clinic_id=%s ORDER BY name,id",  # noqa: S608
        (clinic_id,),
    ).fetchall()


def create_service(conn, staff, doctor_id, name, duration, buffer):
    clinic_id = staff["clinic_id"]
    if not conn.execute(
        "SELECT 1 FROM doctors WHERE clinic_id=%s AND id=%s", (clinic_id, doctor_id)
    ).fetchone():
        raise HTTPException(404, NOT_FOUND)
    service_id = uuid4()
    row = conn.execute(
        "INSERT INTO services (id,clinic_id,doctor_id,name,duration_minutes,buffer_after_minutes)"
        f" VALUES (%s,%s,%s,%s,%s,%s) RETURNING {SERVICE_COLUMNS}",  # noqa: S608
        (service_id, clinic_id, doctor_id, name, duration, buffer),
    ).fetchone()
    audit(conn, clinic_id, staff["id"], "service.created", service_id)
    return row


def update_service(conn, staff, service_id, changes):
    # Lock the owning doctor first, the same order reserve() uses, so edits and bookings serialize.
    # doctor_id is immutable, so reading it before taking the lock is safe.
    owner = conn.execute(
        "SELECT doctor_id FROM services WHERE clinic_id=%s AND id=%s",
        (staff["clinic_id"], service_id),
    ).fetchone()
    if not owner or not lock_doctors(conn, staff["clinic_id"], [owner["doctor_id"]]):
        raise HTTPException(404, NOT_FOUND)
    row = _update(
        conn,
        "services",
        ("name", "duration_minutes", "buffer_after_minutes", "active"),
        staff["clinic_id"],
        service_id,
        changes,
    )
    audit(conn, staff["clinic_id"], staff["id"], "service.updated", service_id)
    return {k: row[k] for k in SERVICE_COLUMNS.split(",")}


def _hours_out(rows):
    return [
        {
            "weekday": r["weekday"],
            "opens": r["opens"].strftime("%H:%M"),
            "closes": r["closes"].strftime("%H:%M"),
        }
        for r in rows
    ]


def _require_doctor(conn, clinic_id, doctor_id):
    if not conn.execute(
        "SELECT id FROM doctors WHERE clinic_id=%s AND id=%s", (clinic_id, doctor_id)
    ).fetchone():
        raise HTTPException(404, NOT_FOUND)


def get_hours(conn, clinic_id, doctor_id):
    _require_doctor(conn, clinic_id, doctor_id)
    return _hours_out(
        conn.execute(
            "SELECT weekday,opens,closes FROM working_hours WHERE clinic_id=%s AND doctor_id=%s"
            " ORDER BY weekday,opens",
            (clinic_id, doctor_id),
        ).fetchall()
    )


def replace_hours(conn, staff, doctor_id, windows):
    clinic_id = staff["clinic_id"]
    # Same row lock the reservation path takes, so a replacement and a booking serialize.
    if not lock_doctors(conn, clinic_id, [doctor_id]):
        raise HTTPException(404, NOT_FOUND)
    conn.execute(
        "DELETE FROM working_hours WHERE clinic_id=%s AND doctor_id=%s", (clinic_id, doctor_id)
    )
    for w in windows:
        conn.execute(
            "INSERT INTO working_hours (clinic_id,doctor_id,weekday,opens,closes)"
            " VALUES (%s,%s,%s,%s,%s)",
            (clinic_id, doctor_id, w.weekday, w.opens, w.closes),
        )
    audit(conn, clinic_id, staff["id"], "hours.replaced", doctor_id)
    return get_hours(conn, clinic_id, doctor_id)
