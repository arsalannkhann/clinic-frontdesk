from uuid import uuid4

from fastapi import HTTPException

from clinic_voice.audit import audit

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


def list_doctors(conn, clinic_id):
    return conn.execute(
        "SELECT id,name,active FROM doctors WHERE clinic_id=%s ORDER BY name,id", (clinic_id,)
    ).fetchall()


def create_doctor(conn, staff, name):
    doctor_id = uuid4()
    row = conn.execute(
        "INSERT INTO doctors (id,clinic_id,name) VALUES (%s,%s,%s) RETURNING id,name,active",
        (doctor_id, staff["clinic_id"], name),
    ).fetchone()
    audit(conn, staff["clinic_id"], staff["id"], "doctor.created", doctor_id)
    return row


def update_doctor(conn, staff, doctor_id, changes):
    row = _update(conn, "doctors", ("name", "active"), staff["clinic_id"], doctor_id, changes)
    audit(conn, staff["clinic_id"], staff["id"], "doctor.updated", doctor_id)
    return {"id": row["id"], "name": row["name"], "active": row["active"]}


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
    if not conn.execute(
        "SELECT d.id FROM services s JOIN doctors d ON d.clinic_id=s.clinic_id AND d.id=s.doctor_id"
        " WHERE s.clinic_id=%s AND s.id=%s FOR UPDATE OF d",
        (staff["clinic_id"], service_id),
    ).fetchone():
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


def _require_doctor(conn, clinic_id, doctor_id, lock=False):
    row = conn.execute(
        "SELECT id FROM doctors WHERE clinic_id=%s AND id=%s" + (" FOR UPDATE" if lock else ""),
        (clinic_id, doctor_id),
    ).fetchone()
    if not row:
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
    _require_doctor(conn, clinic_id, doctor_id, lock=True)
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


EXCEPTION_COLUMNS = "id,doctor_id,kind,starts_at,ends_at,label"


def list_exceptions(conn, clinic_id, starts=None, ends=None):
    """Exceptions overlapping the optional half-open range [starts, ends)."""
    return conn.execute(
        f"SELECT {EXCEPTION_COLUMNS} FROM schedule_exceptions WHERE clinic_id=%s"  # noqa: S608
        " AND (%s::timestamptz IS NULL OR ends_at > %s)"
        " AND (%s::timestamptz IS NULL OR starts_at < %s)"
        " ORDER BY starts_at,id",
        (clinic_id, starts, starts, ends, ends),
    ).fetchall()


def create_exception(conn, staff, doctor_id, kind, starts_at, ends_at, label):
    """Insert an exception and report, but never cancel, overlapping active appointments.

    Takes the same doctor row lock(s) reserve() takes, so once this commits no reservation
    validated without it can land inside the window; anything that committed first is
    reported as conflicting.
    """
    clinic_id = staff["clinic_id"]
    if doctor_id is None:
        conn.execute(
            "SELECT id FROM doctors WHERE clinic_id=%s ORDER BY id FOR UPDATE", (clinic_id,)
        ).fetchall()
    else:
        _require_doctor(conn, clinic_id, doctor_id, lock=True)
    exception_id = uuid4()
    row = conn.execute(
        "INSERT INTO schedule_exceptions"
        " (id,clinic_id,doctor_id,kind,starts_at,ends_at,label,created_by)"
        f" VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {EXCEPTION_COLUMNS}",  # noqa: S608
        (exception_id, clinic_id, doctor_id, kind, starts_at, ends_at, label, staff["id"]),
    ).fetchone()
    conflicts = conn.execute(
        "SELECT id FROM appointments WHERE clinic_id=%s"
        " AND (%s::uuid IS NULL OR doctor_id=%s)"
        " AND status IN ('pending_sync', 'confirmed', 'needs_review')"
        " AND starts_at < %s AND blocks_until > %s ORDER BY starts_at,id",
        (clinic_id, doctor_id, doctor_id, ends_at, starts_at),
    ).fetchall()
    audit(conn, clinic_id, staff["id"], "exception.created", exception_id)
    return {**row, "conflicting_appointment_ids": [r["id"] for r in conflicts]}


def delete_exception(conn, staff, exception_id):
    if not conn.execute(
        "DELETE FROM schedule_exceptions WHERE clinic_id=%s AND id=%s RETURNING id",
        (staff["clinic_id"], exception_id),
    ).fetchone():
        raise HTTPException(404, NOT_FOUND)
    audit(conn, staff["clinic_id"], staff["id"], "exception.deleted", exception_id)
