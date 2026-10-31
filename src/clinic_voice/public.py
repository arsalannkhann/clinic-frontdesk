"""The public clinic API (P1): profile, catalogue, live slots, web bookings, booking lookup
and callback requests, for the clinic website (ADR 0010).

Trust model: nothing here is authenticated. The clinic comes from its public slug, and only
clinics with public_enabled are served. Responses carry the clinic's own public profile and
catalogue, its free slots, and, for a lookup that proved reference + phone, that one
booking's status; never other patients, staff, credentials, calendar ids or internal state.
Refusals use fixed codes (errors.PublicError) and never echo request values.

Bookings are a channel of the same ledger as the voice gateway: the slot is re-validated
inside service.reserve under the doctor row lock (it must be an offered start of that doctor
and service; the GiST exclusion constraint still decides a race), the appointment and its
calendar.create job commit together, and the status is 'confirmed' only once that job
verified the calendar event (for ledger doctors, normally inline: tick.confirm_inline).

Patients: a web booking always creates its own patient row (patients.create_from_form);
the typed phone proves nothing, so it never attaches to, reveals or changes an existing
patient. consent_contact=true is required to book and is recorded as the three contact
purposes (booking confirmation, reminder call, reminder message) for that new patient; no
contact happens unless the clinic's outbound_enabled is on.

Public service ids are catalogue ids (migration 018, COALESCE(catalog_id, id)), shared by
the per-doctor service rows of one offering; a booking names the catalogue id and a doctor,
and the server picks that doctor's row.

Logs and audit rows carry ids and codes only.
"""

import hashlib
import logging
from datetime import UTC, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from clinic_voice import patients, ratelimit, service
from clinic_voice.audit import audit
from clinic_voice.crypto import CryptoError, Keyring, decode_hash_key, phone_hash
from clinic_voice.db import connect
from clinic_voice.domain import normalize_reference
from clinic_voice.errors import PublicError
from clinic_voice.schemas import ReservationRequest

log = logging.getLogger("clinic_voice.public")

KEY_PREFIX = "web:"
CONTACT_PURPOSES = ("booking_confirmation", "reminder_call", "reminder_message")

NOT_FOUND = PublicError(404, "not_found", "Not found")
UNAVAILABLE = PublicError(503, "unavailable", "Booking is unavailable; please try again later")
RATE_LIMITED = PublicError(429, "rate_limited", "Too many requests; please try again later")
SLOT_UNAVAILABLE = PublicError(409, "slot_unavailable", "That time is no longer available")


def fail(error: PublicError) -> PublicError:
    # The module-level instances are templates; raise a fresh exception each time.
    return PublicError(error.status, error.code, error.message)


def notes_aad(clinic_id: UUID, appointment_id: UUID) -> bytes:
    return b"appointment_notes:" + clinic_id.bytes + appointment_id.bytes


def callback_aad(clinic_id: UUID, request_id: UUID, field: str) -> bytes:
    return b"callback_request:" + clinic_id.bytes + request_id.bytes + b":" + field.encode()


def crypto(settings) -> tuple[Keyring, bytes]:
    """Keyring and phone-hash key, or 503 unavailable: the public writes store names and
    phones only as ciphertext plus a keyed hash, and rate limits hash their keys."""
    keys, hash_key = settings.token_encryption_keys, settings.phone_hash_key
    if not keys or not keys.get_secret_value() or not hash_key or not hash_key.get_secret_value():
        log.error("public API needs TOKEN_ENCRYPTION_KEYS and PHONE_HASH_KEY")
        raise fail(UNAVAILABLE)
    try:
        return Keyring.from_setting(keys.get_secret_value()), decode_hash_key(
            hash_key.get_secret_value()
        )
    except CryptoError:
        log.error("public API keyring or phone hash key is invalid")
        raise fail(UNAVAILABLE) from None


CLINIC_COLUMNS = """id, name, timezone, horizon_days, public_tagline, public_address,
    public_map_url, public_phone_display, public_email, public_hours_note, public_about,
    public_voice_number, public_chat_enabled"""


def clinic_by_slug(conn, slug: str):
    """The public clinic, or 404 not_found (unknown slug, or public_enabled off)."""
    row = conn.execute(
        f"SELECT {CLINIC_COLUMNS} FROM clinics WHERE public_slug=%s AND public_enabled",  # noqa: S608
        (slug,),
    ).fetchone()
    if row is None:
        raise fail(NOT_FOUND)
    return row


def profile(clinic) -> dict:
    return {
        "name": clinic["name"],
        "tagline": clinic["public_tagline"],
        "timezone": clinic["timezone"],
        "address": clinic["public_address"],
        "map_url": clinic["public_map_url"],
        "phone_display": clinic["public_phone_display"],
        "email": clinic["public_email"],
        "hours_note": clinic["public_hours_note"],
        "about": clinic["public_about"],
        "voice_number": clinic["public_voice_number"],
        "chat_enabled": clinic["public_chat_enabled"],
    }


# A service row is offered publicly while it, its doctor and the public flag are on.
OFFERED = "s.active AND s.public AND d.active"


def list_services(conn, clinic_id) -> list[dict]:
    rows = conn.execute(
        f"""SELECT DISTINCT ON (COALESCE(s.catalog_id, s.id)) COALESCE(s.catalog_id, s.id) AS id,
        s.name, s.description, s.duration_minutes, s.colour
        FROM services s JOIN doctors d ON d.clinic_id=s.clinic_id AND d.id=s.doctor_id
        WHERE s.clinic_id=%s AND {OFFERED}
        ORDER BY COALESCE(s.catalog_id, s.id), s.id""",  # noqa: S608
        (clinic_id,),
    ).fetchall()
    return sorted(rows, key=lambda r: (r["name"].casefold(), str(r["id"])))


def list_doctors(conn, clinic_id) -> list[dict]:
    rows = conn.execute(
        """SELECT d.id, d.name, d.public_bio, d.photo_url, d.colour,
        COALESCE(array_agg(DISTINCT COALESCE(s.catalog_id, s.id))
            FILTER (WHERE s.id IS NOT NULL), '{}') AS service_ids
        FROM doctors d
        LEFT JOIN services s ON s.clinic_id=d.clinic_id AND s.doctor_id=d.id AND s.active AND s.public
        WHERE d.clinic_id=%s AND d.active
        GROUP BY d.id ORDER BY d.name, d.id""",  # noqa: S608
        (clinic_id,),
    ).fetchall()
    return [{**row, "service_ids": sorted(row["service_ids"], key=str)} for row in rows]


def offerings(conn, clinic_id, service_id, doctor_id=None) -> list[dict]:
    """(doctor_id, service row id, names) of each doctor offering a public service."""
    return conn.execute(
        f"""SELECT DISTINCT ON (s.doctor_id) s.doctor_id, s.id AS service_row_id,
        d.name AS doctor_name, s.name AS service_name
        FROM services s JOIN doctors d ON d.clinic_id=s.clinic_id AND d.id=s.doctor_id
        WHERE s.clinic_id=%s AND COALESCE(s.catalog_id, s.id)=%s AND {OFFERED}
        AND (%s::uuid IS NULL OR s.doctor_id=%s::uuid)
        ORDER BY s.doctor_id, s.id""",  # noqa: S608
        (clinic_id, service_id, doctor_id, doctor_id),
    ).fetchall()


def list_slots(conn, clinic, service_id, doctor_id, day, days) -> dict:
    """Free starts of a public service (one doctor, or every doctor offering it) on `days`
    consecutive clinic-local dates from `day`. Dates past the booking horizon are skipped;
    a first date before today or past the horizon is 422 invalid_date."""
    found = offerings(conn, clinic["id"], service_id, doctor_id)
    if not found:
        raise fail(NOT_FOUND)
    zone = ZoneInfo(clinic["timezone"])
    today = service.utc_now().astimezone(zone).date()
    horizon = clinic["horizon_days"]
    if not 0 <= (day - today).days <= horizon:
        raise PublicError(422, "invalid_date", f"Date must be today through {horizon} days ahead")
    result = []
    for offset in range(days):
        date = day + timedelta(days=offset)
        if (date - today).days > horizon:
            break
        for offer in found:
            context = service.get_context(
                conn, clinic["id"], offer["doctor_id"], offer["service_row_id"]
            )
            length = timedelta(minutes=context["duration_minutes"])
            for start in service.slots(
                conn,
                clinic["id"],
                offer["doctor_id"],
                offer["service_row_id"],
                date,
                context=context,
            ):
                result.append(
                    {"doctor_id": offer["doctor_id"], "starts_at": start, "ends_at": start + length}
                )
    result.sort(key=lambda s: (s["starts_at"].astimezone(UTC), str(s["doctor_id"])))
    return {"timezone": clinic["timezone"], "slots": result}


def client_value(address) -> str:
    # Requests whose address cannot be determined share one bucket (fails closed).
    return str(address) if address is not None else "unknown"


def limit(conn, hash_key, name, count, window, value) -> None:
    if not ratelimit.hit(conn, hash_key, ratelimit.Limit(name, count, window), value):
        log.warning("public rate limit exceeded limit=%s", name)
        raise fail(RATE_LIMITED)


HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


class WebReservation(ReservationRequest):
    """The reservation fingerprinted for idempotency: the slot plus a keyed digest of the
    contact, so a replayed key with another phone or name is a 409, never a replay that
    hands out someone else's booking reference."""

    contact: str


def _map_reserve_error(error: HTTPException) -> PublicError:
    if error.status_code == 404:
        return fail(NOT_FOUND)
    if error.status_code in (409, 422):
        if "Idempotency" in str(error.detail):
            return PublicError(409, "idempotency_conflict", "Idempotency key reused")
        return fail(SLOT_UNAVAILABLE)
    return fail(UNAVAILABLE)


def booking_out(row, zone) -> dict:
    return {
        "reference": row["booking_reference"],
        "status": row["status"],
        "starts_at": row["starts_at"].astimezone(zone),
        "doctor_name": row["doctor_name"],
        "service_name": row["service_name"],
    }


def book(url, settings, clinic, request, address, idempotency_key=None, ports=None) -> dict:
    """A web booking (channel web). Order: consent (422), crypto (503), IP and phone rate
    limits (429, counted before any booking work), offering (404), then service.reserve and,
    after its commit, the inline confirm. idempotency_key (optional, client-chosen) makes a
    retried submission replay the first booking."""
    if request.consent_contact is not True:
        raise PublicError(422, "consent_required", "Consent to be contacted is required to book")
    keyring, hash_key = crypto(settings)
    clinic_id = clinic["id"]
    digest = phone_hash(hash_key, request.phone)
    with connect(url) as conn:
        limit(
            conn,
            hash_key,
            "booking-ip",
            settings.public_booking_ip_per_hour,
            HOUR,
            client_value(address),
        )
        limit(
            conn,
            hash_key,
            "booking-phone",
            settings.public_booking_phone_per_day,
            DAY,
            f"{clinic_id}:{request.phone}",
        )
        found = offerings(conn, clinic_id, request.service_id, request.doctor_id)
    if not found:
        raise fail(NOT_FOUND)
    offer = found[0]
    if idempotency_key:
        key = KEY_PREFIX + hashlib.sha256(idempotency_key.encode()).hexdigest()
    else:
        key = KEY_PREFIX + uuid4().hex
    contact = ratelimit.key_hash(hash_key, "booking-contact", f"{digest.hex()}:{request.name}")
    reservation = WebReservation(
        doctor_id=request.doctor_id,
        service_id=offer["service_row_id"],
        starts_at=request.starts_at,
        contact=contact.hex(),
    )
    details = patients.PatientDetails(
        name=request.name, consents={purpose: True for purpose in CONTACT_PURPOSES}
    )

    def attach(conn, starts_at):
        retention_until = starts_at + timedelta(days=settings.patient_retention_days)
        return patients.create_from_form(
            conn, keyring, clinic_id, details, (request.phone, digest), retention_until, "web"
        )

    def after_insert(conn, appointment_id, patient_id):
        if request.notes:
            key_id, blob = keyring.encrypt(
                request.notes.encode(), notes_aad(clinic_id, appointment_id)
            )
            conn.execute(
                "UPDATE appointments SET notes_ciphertext=%s, notes_key_id=%s"
                " WHERE clinic_id=%s AND id=%s",
                (blob, key_id, clinic_id, appointment_id),
            )
        audit(conn, clinic_id, None, "public.booking_created", appointment_id)

    def gate(conn):
        # Level 1b analogue (locks.py): the clinic row FOR SHARE, re-checking that the
        # clinic still accepts public bookings inside the write transaction.
        row = conn.execute(
            "SELECT public_enabled FROM clinics WHERE id=%s FOR SHARE", (clinic_id,)
        ).fetchone()
        if row is None or not row["public_enabled"]:
            raise fail(NOT_FOUND)

    try:
        result = service.reserve(
            url,
            clinic_id,
            reservation,
            key,
            attach_patient=attach,
            after_insert=after_insert,
            gate=gate,
            channel="web",
        )
    except HTTPException as error:
        raise _map_reserve_error(error) from None
    status = result["status"]
    if status == "pending_sync" and ports is not None:
        from clinic_voice.tick import confirm_inline

        status = confirm_inline(
            url, ports, clinic_id, result["appointment_id"], settings.inline_confirm_budget_seconds
        )
    log.info(
        "public booking appointment=%s clinic=%s status=%s",
        result["appointment_id"],
        clinic_id,
        status,
    )
    return {
        "reference": result["booking_reference"],
        "status": status,
        "starts_at": result["starts_at"].astimezone(ZoneInfo(clinic["timezone"])),
        "doctor_name": offer["doctor_name"],
        "service_name": offer["service_name"],
    }


def lookup(url, settings, clinic, request, address) -> dict:
    """One booking by reference + phone. Every mismatch (malformed or unknown reference,
    another clinic's reference, a phone that is not the booking patient's, a booking whose
    patient record was purged) is the same 404 not_found, so the response never reveals
    whether a reference exists."""
    _, hash_key = crypto(settings)
    with connect(url) as conn:
        limit(
            conn,
            hash_key,
            "lookup-ip",
            settings.public_lookup_ip_per_hour,
            HOUR,
            client_value(address),
        )
        reference = normalize_reference(request.reference)
        row = None
        if reference is not None:
            row = conn.execute(
                """SELECT a.booking_reference, a.status, a.starts_at, d.name AS doctor_name,
                s.name AS service_name FROM appointments a
                JOIN patients p ON p.clinic_id=a.clinic_id AND p.id=a.patient_id
                JOIN doctors d ON d.clinic_id=a.clinic_id AND d.id=a.doctor_id
                JOIN services s ON s.clinic_id=a.clinic_id AND s.id=a.service_id
                WHERE a.clinic_id=%s AND a.booking_reference=%s AND p.phone_hash=%s""",
                (clinic["id"], reference, phone_hash(hash_key, request.phone)),
            ).fetchone()
    if row is None:
        raise fail(NOT_FOUND)
    return booking_out(row, ZoneInfo(clinic["timezone"]))


def store_callback(conn, keyring, hash_key, clinic_id, name, phone, reason, source):
    """Insert one callback request (name, phone and reason encrypted, the phone also as its
    keyed hash) and audit it, in the caller's transaction. Returns (id, status)."""
    request_id = uuid4()

    def seal(field, value):
        if not value:
            return None, None
        return keyring.encrypt(value.encode(), callback_aad(clinic_id, request_id, field))

    name_key, name_blob = seal("name", name)
    phone_key, phone_blob = seal("phone", phone)
    reason_key, reason_blob = seal("reason", reason)
    row = conn.execute(
        """INSERT INTO callback_requests (id,clinic_id,name_ciphertext,name_key_id,
        phone_ciphertext,phone_key_id,phone_hash,reason_ciphertext,reason_key_id,source)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id, status""",
        (
            request_id,
            clinic_id,
            name_blob,
            name_key,
            phone_blob,
            phone_key,
            phone_hash(hash_key, phone),
            reason_blob,
            reason_key,
            source,
        ),
    ).fetchone()
    audit(conn, clinic_id, None, "public.callback_requested", request_id)
    return row["id"], row["status"]


def request_callback(url, settings, clinic, request, address) -> dict:
    """Store a callback request (source web). It is never attached to an existing
    patient."""
    keyring, hash_key = crypto(settings)
    clinic_id = clinic["id"]
    with connect(url) as conn:
        limit(
            conn,
            hash_key,
            "callback-ip",
            settings.public_callback_ip_per_hour,
            HOUR,
            client_value(address),
        )
        limit(
            conn,
            hash_key,
            "callback-phone",
            settings.public_callback_phone_per_day,
            DAY,
            f"{clinic_id}:{request.phone}",
        )
        request_id, status = store_callback(
            conn, keyring, hash_key, clinic_id, request.name, request.phone, request.reason, "web"
        )
    log.info("public callback request=%s clinic=%s", request_id, clinic_id)
    return {"id": request_id, "status": status}
