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


DOCTOR_COLUMNS = "id,name,active,booking_calendar_id,busy_calendar_ids,calendar_account_id"


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
    columns = ("name", "active", "booking_calendar_id", "busy_calendar_ids", "calendar_account_id")
    account_id = changes.get("calendar_account_id")
    if account_id is not None:
        account = conn.execute(
            "SELECT status FROM calendar_accounts WHERE clinic_id=%s AND id=%s FOR SHARE",
            (staff["clinic_id"], account_id),
        ).fetchone()
        if not account:
            raise HTTPException(404, NOT_FOUND)
        if account["status"] != "active":
            raise HTTPException(409, "Calendar account is not active")
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


OUTBOX_COLUMNS = (
    "id,kind,status,attempts,max_attempts,last_error,appointment_id,"
    "available_at,created_at,updated_at"
)


def list_outbox(conn, clinic_id, status, limit):
    """The clinic's jobs in one status, most recently changed first. No payloads."""
    return conn.execute(
        f"SELECT {OUTBOX_COLUMNS} FROM outbox WHERE clinic_id=%s AND status=%s"  # noqa: S608
        " ORDER BY updated_at DESC, id LIMIT %s",
        (clinic_id, status, limit),
    ).fetchall()


def requeue_outbox(conn, staff, job_id):
    """Return a dead job to pending with a fresh budget of attempts.

    attempts is kept (fences rely on it only ever growing); max_attempts becomes attempts +
    DEFAULT_MAX_ATTEMPTS. A job for an appointment that has already started is refused (409).
    For calendar.create whose last_error is one of CALENDAR_DEAD_CODES, the appointment is
    returned from needs_review to pending_sync (same version only), since that review was
    caused by this job's failure; the worker re-reads the event id and re-checks external busy
    time before inserting, so a requeue never bypasses those checks.
    """
    from clinic_voice.handlers.calendar import CALENDAR_DEAD_CODES
    from clinic_voice.outbox import DEFAULT_MAX_ATTEMPTS

    clinic_id = staff["clinic_id"]
    job = conn.execute(
        "SELECT o.status, o.kind, o.last_error, o.appointment_id, o.appointment_version,"
        " a.starts_at <= now() AS past FROM outbox o"
        " LEFT JOIN appointments a ON a.clinic_id=o.clinic_id AND a.id=o.appointment_id"
        " WHERE o.clinic_id=%s AND o.id=%s FOR UPDATE OF o",
        (clinic_id, job_id),
    ).fetchone()
    if not job:
        raise HTTPException(404, NOT_FOUND)
    if job["status"] != "dead":
        raise HTTPException(409, "Only dead jobs can be requeued")
    if job["past"]:
        raise HTTPException(409, "The appointment has already started")
    row = conn.execute(
        "UPDATE outbox SET status='pending', max_attempts=attempts + %s, available_at=now(),"
        " last_error=NULL, lease_expires_at=NULL, updated_at=now()"
        f" WHERE clinic_id=%s AND id=%s RETURNING {OUTBOX_COLUMNS}",  # noqa: S608
        (DEFAULT_MAX_ATTEMPTS, clinic_id, job_id),
    ).fetchone()
    if job["kind"] == "calendar.create" and job["last_error"] in CALENDAR_DEAD_CODES:
        conn.execute(
            "UPDATE appointments SET status='pending_sync', updated_at=now()"
            " WHERE clinic_id=%s AND id=%s AND status='needs_review' AND version=%s",
            (clinic_id, job["appointment_id"], job["appointment_version"]),
        )
    audit(conn, clinic_id, staff["id"], "outbox.requeued", job_id)
    return row
