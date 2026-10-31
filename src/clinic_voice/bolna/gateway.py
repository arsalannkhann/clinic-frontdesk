"""Trusted Bolna tool gateway: call sessions and speakable wrappers around the scheduling
services.

Trust model (see docs/SECURITY.md and ADR 0005):
- The tenant comes only from the authenticated tool credential.
- execution_id, from_number and agent_id are Bolna call-context values rendered into the
  tool request. They scope the conversation (one call session per clinic and execution id)
  but are provider-asserted: the caller number is never identity proof, is stored only as
  ciphertext plus a keyed hash, and is never echoed, logged or audited.
- The idempotency key of a write is derived on the server from the execution id, the tool
  and the canonical arguments, so a Bolna retry replays the same result and the LLM never
  supplies keys.
- Responses are short speakable text without patient data.
"""

import hashlib
import json
import logging
import re
from datetime import UTC, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from clinic_voice import patients, reminders, service
from clinic_voice.crypto import CryptoError, Keyring, decode_hash_key, phone_hash
from clinic_voice.domain import spoken_reference
from clinic_voice.patients import session_aad
from clinic_voice.schemas import CONSENT_UNCLEAR, UNSUBSTITUTED_CODE, ReservationRequest

log = logging.getLogger("clinic_voice.bolna")

PROVIDER = "bolna"
KEY_PREFIX = "bolna:"
SUPPORTED_LANGUAGES = ("en", "hi")
# Most pending_sync/confirmed appointments one call session may create; more is a handoff.
MAX_SESSION_ACTIVE = 3
E164 = re.compile(r"\+[1-9]\d{6,14}")
NOT_CONFIGURED = "Call session storage not configured"
REMINDER_CALL = (
    "I cannot book, verify or change appointments on this call."
    " Please call the clinic directly and the team will help you."
)
PLACEHOLDER_FAQ = (
    "No clinic-approved answers are configured. Offer to take the question to clinic staff."
)
PLACEHOLDER_EMERGENCY = (
    "No clinic-approved emergency script is configured. Say you cannot help with emergencies"
    " and ask the caller to contact local emergency services."
)
PLACEHOLDER_HANDOFF = (
    "No clinic-approved handoff script is configured. Offer to pass a message to clinic staff."
)
NOT_ACCEPTING = (
    "This line isn't accepting bookings yet. Please call the clinic directly and the team will"
    " help you."
)
CONTEXT_UNSUBSTITUTED = (
    "I can't use the booking system on this call. Offer to pass the caller to clinic staff."
)
# Provisioning states (migration 015) in which the line answers at all, and in which it may
# change bookings. verified also allows changes while the owner has test calls enabled.
LINE_OPEN = ("verified", "active")


def require_line(conn, clinic_id, changes: bool, lock: bool = False) -> None:
    """Refuse the tool (403, speakable NOT_ACCEPTING) unless the clinic's line is open:
    active for every tool; verified for context and read-only tools, and for
    booking-changing tools too while clinics.test_calls_enabled is on. A clinic without a
    provisioning row is not active. Runs before anything is written (no call session is
    recorded for a refused request).

    lock=True re-checks inside a booking write transaction and holds the clinic row FOR
    SHARE until it commits (locks.py, level 1b). Every change that closes the line updates
    the clinics row (test_calls_enabled off; a future deactivation must too), so it waits
    for in-flight writes and none commits after the line closed."""
    suffix = " FOR SHARE OF c" if lock else ""
    row = conn.execute(
        "SELECT p.state, c.test_calls_enabled FROM clinics c"
        " LEFT JOIN provisioning p ON p.clinic_id = c.id WHERE c.id=%s" + suffix,
        (clinic_id,),
    ).fetchone()
    state = row["state"] if row else None
    if state == "active" or (state == "verified" and (not changes or row["test_calls_enabled"])):
        return
    conn.rollback()
    raise HTTPException(403, NOT_ACCEPTING)


def refuse_unsubstituted(clinic_id, tool: str, fields: list[str]) -> JSONResponse:
    """422 for a tool request whose call-context fields still hold their templates
    (schemas.unsubstituted): Bolna did not fill execution_id/from_number/agent_id, or the
    agent's tool parameters do not reference them. Nothing has been written (validation runs
    before the endpoint, so before the line gate and the call session). Answers and logs
    field names only, never the values, the credential or the body."""
    log.warning(
        "call context unsubstituted clinic=%s tool=%s fields=%s",
        clinic_id,
        tool,
        ",".join(fields),
    )
    return JSONResponse(
        status_code=422,
        content={"detail": CONTEXT_UNSUBSTITUTED, "code": UNSUBSTITUTED_CODE, "fields": fields},
        headers={"Cache-Control": "no-store"},
    )


def session_crypto(settings) -> tuple[Keyring, bytes]:
    """Keyring and phone-hash key for call sessions, or 503. Both are required: the gateway
    refuses to store a caller number it cannot encrypt and hash."""
    keys = settings.token_encryption_keys
    hash_key = settings.phone_hash_key
    if not keys or not keys.get_secret_value() or not hash_key or not hash_key.get_secret_value():
        log.error("call session storage needs TOKEN_ENCRYPTION_KEYS and PHONE_HASH_KEY")
        raise HTTPException(503, NOT_CONFIGURED)
    try:
        return Keyring.from_setting(keys.get_secret_value()), decode_hash_key(
            hash_key.get_secret_value()
        )
    except CryptoError:
        log.error("call session keyring or phone hash key is invalid")
        raise HTTPException(503, NOT_CONFIGURED) from None


def caller_number(value: str) -> str | None:
    """The provider-asserted number if it is E.164, else None (withheld or unrendered)."""
    value = value.strip()
    return value if E164.fullmatch(value) else None


def provider_uuid(value: str) -> str | None:
    try:
        return str(UUID(value.strip()))
    except ValueError:
        return None


def _sealed(keyring, hash_key, clinic_id, session_id, number):
    if number is None:
        return None, None, None
    key_id, blob = keyring.encrypt(number.encode(), session_aad(clinic_id, session_id))
    return blob, key_id, phone_hash(hash_key, number)


def upsert_session(conn, clinic_id, context, crypto) -> UUID:
    """The call session for (clinic, execution id), created on first use. A later call may
    fill a missing caller number or agent id but never replaces one already stored.
    Commits, so the session is recorded even when the tool itself then fails, and so that
    service.reserve (its own connection) can reference it."""
    keyring, hash_key = crypto
    execution_id = str(context.execution_id)
    agent_id = provider_uuid(context.agent_id)
    number = caller_number(context.from_number)
    session_id = uuid4()
    blob, key_id, digest = _sealed(keyring, hash_key, clinic_id, session_id, number)
    row = conn.execute(
        """INSERT INTO call_sessions (id,clinic_id,provider,execution_id,agent_id,
        caller_number_ciphertext,caller_number_key_id,caller_number_hash)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (clinic_id, execution_id) DO NOTHING RETURNING id""",
        (session_id, clinic_id, PROVIDER, execution_id, agent_id, blob, key_id, digest),
    ).fetchone()
    if row:
        conn.commit()
        return row["id"]
    row = conn.execute(
        """SELECT id, status, agent_id, caller_number_hash IS NULL AS no_number
        FROM call_sessions WHERE clinic_id=%s AND execution_id=%s FOR NO KEY UPDATE""",
        (clinic_id, execution_id),
    ).fetchone()
    if row["status"] != "active":
        raise HTTPException(409, "This call has ended")
    if row["no_number"] and number is not None:
        blob, key_id, digest = _sealed(keyring, hash_key, clinic_id, row["id"], number)
        conn.execute(
            """UPDATE call_sessions SET caller_number_ciphertext=%s, caller_number_key_id=%s,
            caller_number_hash=%s WHERE clinic_id=%s AND id=%s AND caller_number_hash IS NULL""",
            (blob, key_id, digest, clinic_id, row["id"]),
        )
    if row["agent_id"] is None and agent_id is not None:
        conn.execute(
            "UPDATE call_sessions SET agent_id=%s WHERE clinic_id=%s AND id=%s",
            (agent_id, clinic_id, row["id"]),
        )
    conn.commit()
    return row["id"]


def refuse_reminder_call(conn, clinic_id, context) -> None:
    """403 with a speakable message when this execution is one of the clinic's outbound
    reminder calls: its execution id is a placed reminder call's, or its agent id is the
    clinic's bolna_outbound_agent_id. Whoever answers a reminder call is not verified (the
    call went to a number on file, and the answerer may not be the patient), so the tools
    that book, verify, change or opt out are refused there even if the outbound agent was
    misconfigured with them. Both values are provider-asserted; they are only ever used to
    refuse. The clinic's own number is not stored, so it is not checked."""
    agent_id = provider_uuid(context.agent_id)
    row = conn.execute(
        """SELECT EXISTS (SELECT 1 FROM reminders WHERE clinic_id=%s AND channel='call'
            AND provider_execution_id=%s) AS placed,
        EXISTS (SELECT 1 FROM clinics WHERE id=%s AND bolna_outbound_agent_id IS NOT NULL
            AND bolna_outbound_agent_id=%s) AS outbound""",
        (clinic_id, str(context.execution_id), clinic_id, agent_id),
    ).fetchone()
    conn.commit()
    if row["placed"] or row["outbound"]:
        log.warning("booking tool refused on a reminder call")
        raise HTTPException(403, REMINDER_CALL)


def derive_key(execution_id: UUID, tool: str, args: dict) -> str:
    """bolna:<execution_id>:<tool>:<sha256(canonical json args)[:24]>."""
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    return f"{KEY_PREFIX}{execution_id}:{tool}:{digest}"


def _normal(name: str) -> str:
    return " ".join(name.split()).casefold()


def resolve(conn, clinic_id, doctor: str, service_name: str) -> tuple[UUID, UUID]:
    """Map spoken catalog names (as listed by /v1/bolna/context) to this clinic's active
    doctor and service ids. Exact match after case and whitespace folding; never fuzzy."""
    doctors = [
        row["id"]
        for row in conn.execute(
            "SELECT id, name FROM doctors WHERE clinic_id=%s AND active", (clinic_id,)
        ).fetchall()
        if _normal(row["name"]) == _normal(doctor)
    ]
    if not doctors:
        raise HTTPException(404, "I could not find that doctor at this clinic.")
    if len(doctors) > 1:
        raise HTTPException(409, "More than one doctor has that name; clinic staff must help.")
    services = [
        row["id"]
        for row in conn.execute(
            "SELECT id, name FROM services WHERE clinic_id=%s AND doctor_id=%s AND active",
            (clinic_id, doctors[0]),
        ).fetchall()
        if _normal(row["name"]) == _normal(service_name)
    ]
    if not services:
        raise HTTPException(404, "That doctor does not offer that service.")
    if len(services) > 1:
        raise HTTPException(409, "More than one service has that name; clinic staff must help.")
    return doctors[0], services[0]


def clinic_zone(conn, clinic_id) -> ZoneInfo:
    """The clinic's IANA zone (gateway and bolna.bookings speak times in it)."""
    row = conn.execute("SELECT timezone FROM clinics WHERE id=%s", (clinic_id,)).fetchone()
    return ZoneInfo(row["timezone"])


def _clock(moment) -> str:
    return moment.strftime("%I:%M %p").lstrip("0")


def spoken_slots(starts, zone, day) -> str:
    when = day.strftime("%A %d %B").replace(" 0", " ")
    if not starts:
        return f"There are no open times on {when}."
    times = [_clock(s.astimezone(zone)) for s in starts]
    listed = times[0] if len(times) == 1 else ", ".join(times[:-1]) + " or " + times[-1]
    return f"Open times on {when}: {listed}."


def check_slots(conn, clinic_id, query):
    doctor_id, service_id = resolve(conn, clinic_id, query.doctor, query.service)
    available = service.slots(conn, clinic_id, doctor_id, service_id, query.day)[:3]
    zone = clinic_zone(conn, clinic_id)
    return {"slots": available, "spoken": spoken_slots(available, zone, query.day)}


# Said when a decline could not revoke an existing grant (unverified caller, ADR 0006).
PREFERENCE_KEPT = (
    "This booking will not get the contacts you declined, but your other contact"
    " preferences were not changed. Clinic staff can change them for all your bookings."
)
# A decline on a matched existing patient becomes an opt-out of the new booking only.
DECLINE_SCOPE = {
    "booking_confirmation": ("booking_confirmation", "message"),
    "reminder_call": ("reminder", "call"),
    "reminder_message": ("reminder", "message"),
}

CONSENT_WORDS = {
    "booking_confirmation": "confirming this booking",
    "reminder_call": "reminder calls",
    "reminder_message": "reminder messages",
}


def patient_details(request) -> patients.PatientDetails:
    """Patient details of a reservation, or 422 with speakable text if a consent answer was
    neither yes nor no (checked before anything is written)."""
    flags = {
        "booking_confirmation": request.consent_booking_confirmation,
        "reminder_call": request.consent_reminder_call,
        "reminder_message": request.consent_reminder_message,
    }
    unclear = [CONSENT_WORDS[p] for p, value in flags.items() if value == CONSENT_UNCLEAR]
    if unclear:
        raise HTTPException(
            422,
            "I did not catch a clear yes or no about "
            + " and ".join(unclear)
            + ". Please ask the caller to answer yes or no, then try again. Nothing was booked.",
        )
    return patients.PatientDetails(
        name=request.patient_name or None,
        language=request.preferred_language or None,
        consents={p: value for p, value in flags.items() if value is not None},
    )


def reserve(url, clinic_id, session_id, request, doctor_id, service_id, crypto, retention_days):
    """Reserve through service.reserve with the derived key. The caller resolves the names
    and releases its own connection first, so one reservation holds one connection.

    The key covers the slot only (never the name or consent flags, so no patient data is
    hashed into keys): a repeated call for the same slot in the same execution replays the
    first reservation, including its patient and booking reference."""
    args = {
        "doctor_id": str(doctor_id),
        "service_id": str(service_id),
        "starts_at": request.starts_at.astimezone(UTC).isoformat(),
    }
    key = derive_key(request.execution_id, "reserve-slot", args)
    keyring = crypto[0]
    details = patient_details(request)
    kept: list[str] = []
    declined: list[str] = []

    def attach(conn, starts_at):
        kept.clear()  # Only the attempt that commits counts.
        declined.clear()
        retention_until = starts_at + timedelta(days=retention_days)
        return patients.attach(
            conn,
            keyring,
            clinic_id,
            session_id,
            details,
            retention_until,
            kept_grants=kept,
            declined=declined,
        )

    def scope_declines(conn, appointment_id, patient_id):
        # Matched existing patient: the decline applies to this booking only (never revokes
        # the patient's consent, which a spoofed caller ID plus a name could otherwise do).
        for purpose in declined:
            opt_purpose, channel = DECLINE_SCOPE[purpose]
            reminders.opt_out(
                conn,
                clinic_id,
                patient_id,
                opt_purpose,
                channel,
                "tool",
                appointment_id=appointment_id,
            )

    result = service.reserve(
        url,
        clinic_id,
        ReservationRequest(doctor_id=doctor_id, service_id=service_id, starts_at=request.starts_at),
        key,
        call_session_id=session_id,
        max_session_active=MAX_SESSION_ACTIVE,
        attach_patient=attach,
        after_insert=scope_declines,
        gate=lambda conn: require_line(conn, clinic_id, True, lock=True),
    )
    # A replay returns the appointment's current status (it may since have been confirmed,
    # cancelled or sent for review): speak that, never a fixed "reserved".
    reference = result["booking_reference"]
    return {
        "appointment_id": result["appointment_id"],
        "booking_reference": reference,
        "status": result["status"],
        "starts_at": result["starts_at"],
        "spoken": service.spoken_status(result["status"])
        + f" Your booking reference is {spoken_reference(reference)}."
        + (f" {PREFERENCE_KEPT}" if kept else ""),
    }


def appointment_status(conn, clinic_id, query):
    """Only appointments created in, or verified by, this same call session (clinic +
    execution id)."""
    row = conn.execute(
        """SELECT a.id, a.status FROM appointments a
        JOIN call_sessions s ON s.clinic_id=a.clinic_id AND s.execution_id=%s
        WHERE a.clinic_id=%s AND a.id=%s AND (a.call_session_id=s.id OR EXISTS (
            SELECT 1 FROM call_session_verifications v WHERE v.clinic_id=a.clinic_id
            AND v.call_session_id=s.id AND v.appointment_id=a.id))""",
        (str(query.execution_id), clinic_id, query.appointment_id),
    ).fetchone()
    if not row:
        raise HTTPException(404, "I cannot find that request in this call.")
    return {
        "appointment_id": row["id"],
        "status": row["status"],
        "spoken": service.spoken_status(row["status"]),
    }


def clinic_context(conn, clinic_id):
    clinic = conn.execute(
        "SELECT name, timezone, emergency_text, handoff_text, scripts_approved FROM clinics"
        " WHERE id=%s",
        (clinic_id,),
    ).fetchone()
    if not clinic:
        raise HTTPException(404, "Clinic not found")
    approved = clinic["scripts_approved"]
    rows = conn.execute(
        """SELECT d.id, d.name AS doctor, s.name AS service FROM doctors d
        LEFT JOIN services s ON s.clinic_id=d.clinic_id AND s.doctor_id=d.id AND s.active
        WHERE d.clinic_id=%s AND d.active ORDER BY d.name, d.id, s.name""",
        (clinic_id,),
    ).fetchall()
    doctors = {}
    for row in rows:
        entry = doctors.setdefault(row["id"], {"name": row["doctor"], "services": []})
        if row["service"] is not None:
            entry["services"].append(row["service"])
    zone = ZoneInfo(clinic["timezone"])
    return {
        "clinic_name": clinic["name"],
        "timezone": clinic["timezone"],
        "today": service.utc_now().astimezone(zone).date(),
        "doctors": list(doctors.values()),
        "supported_languages": list(SUPPORTED_LANGUAGES),
        "faq_text": PLACEHOLDER_FAQ,
        "emergency_text": clinic["emergency_text"] if approved else PLACEHOLDER_EMERGENCY,
        "handoff_text": clinic["handoff_text"] if approved else PLACEHOLDER_HANDOFF,
        "approved_text": approved,
    }
