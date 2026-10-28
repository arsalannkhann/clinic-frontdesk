import hashlib
import json
from datetime import timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from psycopg.errors import ExclusionViolation, UniqueViolation

from clinic_voice.db import connect
from clinic_voice.domain import candidate_slots, utc_now


def get_context(conn, clinic_id, doctor_id, service_id):
    context = conn.execute(
        """SELECT s.duration_minutes, c.timezone, c.horizon_days FROM services s
        JOIN clinics c ON c.id=s.clinic_id
        JOIN doctors d ON d.clinic_id=s.clinic_id AND d.id=s.doctor_id
        WHERE s.clinic_id=%s AND s.doctor_id=%s AND s.id=%s AND s.active AND d.active""",
        (clinic_id, doctor_id, service_id),
    ).fetchone()
    if not context:
        raise HTTPException(404, "Doctor or service not found")
    return context


def slots(conn, clinic_id, doctor_id, service_id, day):
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
    busy = conn.execute(
        """SELECT starts_at, ends_at FROM appointments WHERE clinic_id=%s AND doctor_id=%s
        AND status IN ('pending_sync', 'confirmed', 'needs_review')
        AND (starts_at AT TIME ZONE %s)::date <= %s
        AND (ends_at AT TIME ZONE %s)::date >= %s""",
        (clinic_id, doctor_id, context["timezone"], day, context["timezone"], day),
    ).fetchall()
    return candidate_slots(
        day,
        [(r["opens"], r["closes"]) for r in hours],
        context["duration_minutes"],
        zone,
        [(r["starts_at"], r["ends_at"]) for r in busy],
        now,
    )


def response(row):
    return {
        "appointment_id": row["id"],
        "status": row["status"],
        "starts_at": row["starts_at"],
        "ends_at": row["ends_at"],
        "spoken_confirmation": "Your request is reserved locally and awaiting calendar confirmation. It is not yet confirmed.",
    }


def replay(conn, clinic_id, key, fingerprint):
    row = conn.execute(
        "SELECT * FROM appointments WHERE clinic_id=%s AND idempotency_key=%s", (clinic_id, key)
    ).fetchone()
    if row:
        if row["request_hash"] != fingerprint:
            raise HTTPException(409, "Idempotency key reused with different request")
        return response(row)


def reserve(url, clinic_id, request, key):
    canonical = request.model_dump(mode="json")
    # Normalize offset representations so the same instant retries consistently.
    from datetime import UTC

    canonical["starts_at"] = request.starts_at.astimezone(UTC).isoformat()
    fingerprint = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
    try:
        with connect(url) as conn:
            # Serialize the same logical action even if the winner commits between reads.
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"{clinic_id}:{key}",)
            )
            previous = replay(conn, clinic_id, key, fingerprint)
            if previous:
                return previous
            # Short DB-only lock orders competing writes for the same doctor.
            # GiST exclusion alone can deadlock simultaneous overlapping inserts.
            conn.execute(
                "SELECT id FROM doctors WHERE clinic_id=%s AND id=%s FOR UPDATE",
                (clinic_id, request.doctor_id),
            )
            context = get_context(conn, clinic_id, request.doctor_id, request.service_id)
            day = request.starts_at.astimezone(ZoneInfo(context["timezone"])).date()
            if request.starts_at not in slots(
                conn, clinic_id, request.doctor_id, request.service_id, day
            ):
                raise HTTPException(409, "Slot unavailable or outside working hours")
            appointment_id = uuid4()
            row = conn.execute(
                """INSERT INTO appointments (id,clinic_id,doctor_id,service_id,starts_at,ends_at,status,idempotency_key,request_hash)
                VALUES (%s,%s,%s,%s,%s,%s,'pending_sync',%s,%s) RETURNING *""",
                (
                    appointment_id,
                    clinic_id,
                    request.doctor_id,
                    request.service_id,
                    request.starts_at,
                    request.starts_at + timedelta(minutes=context["duration_minutes"]),
                    key,
                    fingerprint,
                ),
            ).fetchone()
            conn.execute(
                "INSERT INTO outbox (id,clinic_id,appointment_id,kind) VALUES (%s,%s,%s,'calendar.create')",
                (uuid4(), clinic_id, appointment_id),
            )
            result = response(row)
        return result
    except (ExclusionViolation, UniqueViolation):
        # A competing retry may have won either constraint. Read its committed result.
        with connect(url) as conn:
            previous = replay(conn, clinic_id, key, fingerprint)
            if previous:
                return previous
        raise HTTPException(409, "Slot unavailable") from None
