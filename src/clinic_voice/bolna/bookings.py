"""Verified booking lookup, cancel and reschedule over the Bolna gateway (T7).

Verification (ADR 0006): the caller speaks a booking reference; it verifies only if the
appointment with that reference (in the credential's clinic) belongs to a patient whose
phone hash equals the call session's caller-number hash. Either factor alone is
insufficient. A wrong reference, a reference of another phone's booking, a malformed
reference and a session without a caller number all get the same 404 with the same text,
so the tool reveals nothing about which references exist. Bookings are never listed or
searched by phone; a shared family phone verifies only the reference the caller speaks.

Each distinct failed attempt of a session counts once (a provider retry of the same attempt
is not double counted). At MAX_VERIFICATION_FAILURES the session needs a staff handoff:
verify, cancel and reschedule then refuse with a speakable handoff message (403).

A success is recorded for that session and that appointment only
(call_session_verifications); another execution, even of the same caller, must verify again.

Cancel and reschedule need that verification plus confirmed=true, which the agent sends only
after reading the change back to the caller. Without it they return changed=false and touch
nothing. Writes use server-derived idempotency keys (gateway.derive_key) over the execution
id and the resolved appointment id (and new UTC start), recorded in appointment_changes and,
for a reschedule, on the new appointment; a replay returns the recorded outcome.

Spoken text follows service.spoken_status: "cancelled"/"moved" is said only after it was
persisted, and a new appointment is described by its own (pending) status.
"""

import hashlib
import hmac
from datetime import UTC, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from psycopg.errors import ExclusionViolation, UniqueViolation

from clinic_voice import appointments, patients, reminders, service
from clinic_voice.bolna.gateway import derive_key, require_line
from clinic_voice.db import connect
from clinic_voice.domain import ACTIVE_STATUSES, normalize_reference, spoken_reference
from clinic_voice.locks import lock_doctors

MAX_VERIFICATION_FAILURES = 3
VERIFY_FAILED = (
    "I could not verify a booking with that reference from this phone line."
    " Please check the reference and try again."
)
HANDOFF = (
    "I cannot verify or change bookings on this call. Clinic staff will help you with this booking."
)
NOT_VERIFIED = "I need to verify that booking reference on this call before I can change it."
NOT_ACTIVE = "That booking is no longer active, so nothing was changed."
STARTED = "That appointment time has already passed, so I cannot change it. Clinic staff can help."
SAME_TIME = "That is already the time of this appointment, so nothing was changed."
UNAVAILABLE = "That new time is not available. Your original appointment is unchanged."
OPT_OUT_NOT_VERIFIED = (
    "To stop reminders I first need to verify your booking reference on this call."
    " Clinic staff can also do this for you. Nothing was changed."
)
OPT_OUT_NO_PATIENT = "I cannot change contact preferences for that booking. Clinic staff can help."
OPT_OUT_SCOPES = {"all": "all", "calls": "call", "messages": "message"}
OPT_OUT_WORDS = {
    "all": "calls or messages",
    "call": "calls",
    "message": "messages",
}


def _zone(conn, clinic_id) -> ZoneInfo:
    row = conn.execute("SELECT timezone FROM clinics WHERE id=%s", (clinic_id,)).fetchone()
    return ZoneInfo(row["timezone"])


def _when(moment, zone) -> str:
    local = moment.astimezone(zone)
    return (
        local.strftime("%A %d %B").replace(" 0", " ")
        + " at "
        + local.strftime("%I:%M %p").lstrip("0")
    )


def _session(conn, clinic_id, session_id, lock=False):
    suffix = " FOR NO KEY UPDATE" if lock else ""
    return conn.execute(
        "SELECT verification_failures, caller_number_hash FROM call_sessions"
        " WHERE clinic_id=%s AND id=%s" + suffix,
        (clinic_id, session_id),
    ).fetchone()


def _refuse_handoff(session):
    if session["verification_failures"] >= MAX_VERIFICATION_FAILURES:
        raise HTTPException(403, HANDOFF)


def _attempt_digest(hash_key: bytes, value: str) -> bytes:
    return hmac.new(hash_key, b"verify-attempt:" + value.encode(), hashlib.sha256).digest()


def verify(conn, clinic_id, session_id, query, hash_key: bytes):
    """Verify booking reference + session caller number (lock order 1a: the session row)."""
    session = _session(conn, clinic_id, session_id, lock=True)
    _refuse_handoff(session)
    reference = normalize_reference(query.booking_reference)
    row = None
    if reference is not None and session["caller_number_hash"] is not None:
        row = conn.execute(
            """SELECT a.id, a.status, a.starts_at, a.booking_reference, d.name AS doctor,
            s.name AS service FROM appointments a
            JOIN patients p ON p.clinic_id=a.clinic_id AND p.id=a.patient_id
            JOIN doctors d ON d.clinic_id=a.clinic_id AND d.id=a.doctor_id
            JOIN services s ON s.clinic_id=a.clinic_id AND s.id=a.service_id
            WHERE a.clinic_id=%s AND a.booking_reference=%s AND p.phone_hash=%s""",
            (clinic_id, reference, bytes(session["caller_number_hash"])),
        ).fetchone()
    if row is None:
        attempt = reference or query.booking_reference.strip().casefold()
        counted = conn.execute(
            """INSERT INTO call_session_verification_failures
            (clinic_id,call_session_id,attempt_digest) VALUES (%s,%s,%s)
            ON CONFLICT DO NOTHING RETURNING call_session_id""",
            (clinic_id, session_id, _attempt_digest(hash_key, attempt)),
        ).fetchone()
        failures = session["verification_failures"]
        if counted:
            failures = conn.execute(
                "UPDATE call_sessions SET verification_failures = verification_failures + 1"
                " WHERE clinic_id=%s AND id=%s RETURNING verification_failures",
                (clinic_id, session_id),
            ).fetchone()["verification_failures"]
        conn.commit()  # The failure counts even though the response is an error.
        if failures >= MAX_VERIFICATION_FAILURES:
            raise HTTPException(403, HANDOFF)
        raise HTTPException(404, VERIFY_FAILED)
    conn.execute(
        """INSERT INTO call_session_verifications (clinic_id,call_session_id,appointment_id)
        VALUES (%s,%s,%s) ON CONFLICT DO NOTHING""",
        (clinic_id, session_id, row["id"]),
    )
    zone = _zone(conn, clinic_id)
    return {
        "booking_reference": row["booking_reference"],
        "status": row["status"],
        "starts_at": row["starts_at"],
        "doctor": row["doctor"],
        "service": row["service"],
        "spoken": (
            f"I found the booking with {row['doctor']} on {_when(row['starts_at'], zone)}. "
            + service.spoken_status(row["status"])
        ),
    }


def _verified(conn, clinic_id, session_id, raw_reference):
    """The appointment this session verified under that reference, else 403. The handoff
    state is checked first."""
    _refuse_handoff(_session(conn, clinic_id, session_id))
    reference = normalize_reference(raw_reference)
    row = None
    if reference is not None:
        row = conn.execute(
            """SELECT a.* FROM call_session_verifications v
            JOIN appointments a ON a.clinic_id=v.clinic_id AND a.id=v.appointment_id
            WHERE v.clinic_id=%s AND v.call_session_id=%s AND a.booking_reference=%s""",
            (clinic_id, session_id, reference),
        ).fetchone()
    if row is None:
        raise HTTPException(403, NOT_VERIFIED)
    return row


def _changeable(row):
    if row["status"] not in ACTIVE_STATUSES:
        raise HTTPException(409, NOT_ACTIVE)
    if row["starts_at"] <= service.utc_now():
        raise HTTPException(409, STARTED)


def _lock(conn, clinic_id, key):
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"{clinic_id}:{key}",))


def _change(conn, clinic_id, key):
    return conn.execute(
        "SELECT * FROM appointment_changes WHERE clinic_id=%s AND idempotency_key=%s",
        (clinic_id, key),
    ).fetchone()


def cancel(conn, clinic_id, session_id, request):
    row = _verified(conn, clinic_id, session_id, request.booking_reference)
    zone = _zone(conn, clinic_id)
    key = derive_key(request.execution_id, "cancel-booking", {"appointment_id": str(row["id"])})
    out = {"booking_reference": row["booking_reference"], "starts_at": row["starts_at"]}
    if not request.confirmed:
        _changeable(row)
        return {
            **out,
            "changed": False,
            "status": row["status"],
            "spoken": (
                f"Please confirm: cancel the appointment on {_when(row['starts_at'], zone)}?"
                " Nothing has been changed yet."
            ),
        }
    _lock(conn, clinic_id, key)  # Lock order 1.
    require_line(conn, clinic_id, True, lock=True)  # Lock order 1b: the line is still open.
    if _change(conn, clinic_id, key) is None:
        locked = appointments.lock_appointment(conn, clinic_id, row["id"])  # Lock order 5.
        _changeable(locked)
        appointments.cancel_locked(conn, locked)
        appointments.record_change(
            conn, locked, "cancel", "voice_call", call_session_id=session_id, key=key
        )
    return {
        **out,
        "changed": True,
        "status": "cancelled",
        "spoken": service.spoken_status("cancelled"),
    }


def _rescheduled(new_row, previous_reference, zone):
    return {
        "changed": True,
        "booking_reference": new_row["booking_reference"],
        "previous_booking_reference": previous_reference,
        "status": new_row["status"],
        "starts_at": new_row["starts_at"],
        "spoken": (
            f"Your appointment has been moved to {_when(new_row['starts_at'], zone)}. "
            + service.spoken_status(new_row["status"])
            + " Your new booking reference is "
            + spoken_reference(new_row["booking_reference"])
            + "."
        ),
    }


def reschedule(url, clinic_id, session_id, request, retention_days: int):
    """Move a verified booking to a new start for the same doctor and service, in one
    transaction (lock order 1 -> 3 -> 5): cancel the old appointment, then reserve the new
    slot through the normal slot check. Any failure rolls back both, keeping the original."""
    starts_at = request.starts_at
    with connect(url) as conn:
        row = _verified(conn, clinic_id, session_id, request.booking_reference)
        zone = _zone(conn, clinic_id)
        args = {
            "appointment_id": str(row["id"]),
            "starts_at": starts_at.astimezone(UTC).isoformat(),
        }
        key = derive_key(request.execution_id, "reschedule-booking", args)
        request_hash = service.fingerprint(args)
        if not request.confirmed:
            _changeable(row)
            return {
                "changed": False,
                "booking_reference": row["booking_reference"],
                "status": row["status"],
                "starts_at": row["starts_at"],
                "spoken": (
                    f"Please confirm: move the appointment on {_when(row['starts_at'], zone)}"
                    f" to {_when(starts_at, zone)}? Nothing has been changed yet."
                ),
            }
    try:
        with connect(url) as conn:
            _lock(conn, clinic_id, key)
            require_line(conn, clinic_id, True, lock=True)  # Lock order 1b.
            done = _replayed(conn, clinic_id, key, request_hash, row, zone)
            if done:
                return done
            lock_doctors(conn, clinic_id, [row["doctor_id"]])
            locked = appointments.lock_appointment(conn, clinic_id, row["id"])
            _changeable(locked)
            if locked["starts_at"] == starts_at:
                raise HTTPException(409, SAME_TIME)
            appointments.cancel_locked(conn, locked)
            try:
                new_row = service.insert_reservation(
                    conn,
                    clinic_id,
                    locked["doctor_id"],
                    locked["service_id"],
                    starts_at,
                    key,
                    request_hash,
                    call_session_id=session_id,
                    patient_id=locked["patient_id"],
                    rescheduled_from=locked["id"],
                )
            except HTTPException as error:
                if error.status_code == 409:
                    raise HTTPException(409, UNAVAILABLE) from None
                raise
            # Opt-outs of the moved booking follow it to its successor.
            reminders.carry_opt_outs(conn, clinic_id, locked["id"], new_row["id"])
            if locked["patient_id"] is not None:
                patients.extend_retention(
                    conn,
                    clinic_id,
                    locked["patient_id"],
                    new_row["starts_at"] + timedelta(days=retention_days),
                )
            appointments.record_change(
                conn,
                locked,
                "reschedule",
                "voice_call",
                new_appointment_id=new_row["id"],
                call_session_id=session_id,
                key=key,
            )
            # The caller proved this booking on this call; the successor stays verified here.
            conn.execute(
                """INSERT INTO call_session_verifications
                (clinic_id,call_session_id,appointment_id) VALUES (%s,%s,%s)""",
                (clinic_id, session_id, new_row["id"]),
            )
            result = _rescheduled(new_row, locked["booking_reference"], zone)
        return result
    except (ExclusionViolation, UniqueViolation):
        # A concurrent change or retry won a constraint; report its committed outcome.
        with connect(url) as conn:
            done = _replayed(conn, clinic_id, key, request_hash, row, zone)
            if done:
                return done
        raise HTTPException(409, UNAVAILABLE) from None


def _replayed(conn, clinic_id, key, request_hash, row, zone):
    new_row = conn.execute(
        "SELECT * FROM appointments WHERE clinic_id=%s AND idempotency_key=%s", (clinic_id, key)
    ).fetchone()
    if new_row is None:
        return None
    if new_row["request_hash"] != request_hash:
        raise HTTPException(409, "Idempotency key reused with different request")
    return _rescheduled(new_row, row["booking_reference"], zone)


def opt_out(conn, clinic_id, session_id, request):
    """Stop reminders and confirmations of the one booking this session verified (reference
    + caller number) on the requested channels. Requires that verification: caller ID alone
    is spoofable. Scoped to that appointment, never patient-wide: a caller can be attached
    to another patient's record by caller ID and name (patients.attach), so a patient-wide
    opt-out from a verified booking could still silence another patient's other bookings
    (ADR 0008). Patient-wide opt-outs are staff only. confirmed=false reads the change back
    and changes nothing. Unverified (or handoff) sessions get a speakable 403. Idempotent;
    suppresses that appointment's unsent contacts at once."""
    try:
        row = _verified(conn, clinic_id, session_id, request.booking_reference)
    except HTTPException as error:
        if error.detail == NOT_VERIFIED:
            raise HTTPException(403, OPT_OUT_NOT_VERIFIED) from None
        raise
    if row["patient_id"] is None:
        raise HTTPException(409, OPT_OUT_NO_PATIENT)
    channel = OPT_OUT_SCOPES[request.scope]
    zone = _zone(conn, clinic_id)
    words = OPT_OUT_WORDS[channel]
    out = {"booking_reference": row["booking_reference"], "scope": request.scope}
    if not request.confirmed:
        return {
            **out,
            "opted_out": False,
            "spoken": (
                f"Please confirm: stop appointment {words} for the appointment on"
                f" {_when(row['starts_at'], zone)}? Nothing has been changed yet."
            ),
        }
    require_line(conn, clinic_id, True, lock=True)  # Lock order 1b: the line is still open.
    reminders.opt_out(
        conn, clinic_id, row["patient_id"], "all", channel, "tool", appointment_id=row["id"]
    )
    return {
        **out,
        "opted_out": True,
        "spoken": (
            f"Done. The clinic will not send you {words} about the appointment on"
            f" {_when(row['starts_at'], zone)}. Clinic staff can change this if you ask them."
        ),
    }
