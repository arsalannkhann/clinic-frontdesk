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
import ipaddress
import logging
from contextlib import nullcontext
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


def offerings(conn, clinic_id, service_id, doctor_id=None, public_only=True) -> list[dict]:
    """(doctor_id, service row id, names) of each doctor offering a public service.
    public_only=False (staff bookings): any active service of an active doctor, named by
    its catalogue id or its own row id."""
    offered = OFFERED if public_only else "s.active AND d.active"
    return conn.execute(
        f"""SELECT DISTINCT ON (s.doctor_id) s.doctor_id, s.id AS service_row_id,
        d.name AS doctor_name, s.name AS service_name
        FROM services s JOIN doctors d ON d.clinic_id=s.clinic_id AND d.id=s.doctor_id
        WHERE s.clinic_id=%s AND (COALESCE(s.catalog_id, s.id)=%s OR (NOT %s AND s.id=%s))
        AND {offered} AND (%s::uuid IS NULL OR s.doctor_id=%s::uuid)
        ORDER BY s.doctor_id, s.id""",  # noqa: S608
        (clinic_id, service_id, public_only, service_id, doctor_id, doctor_id),
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


IPV6_BUCKET_PREFIX = 64


def client_value(address) -> str:
    """The rate-limit bucket of a client address. IPv4: the address. IPv6: its /64 prefix,
    because one subscriber or host is routinely given a whole /64 and could otherwise rotate
    through 2**64 buckets (an IPv4-mapped IPv6 address counts as its IPv4 address).
    Requests whose address cannot be determined share one bucket (fails closed)."""
    if address is None:
        return "unknown"
    if isinstance(address, str):
        try:
            address = ipaddress.ip_address(address.strip())
        except ValueError:
            return "unknown"
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network((address, IPV6_BUCKET_PREFIX), strict=False))
    return str(address)


def limit(conn, hash_key, name, count, window, value) -> None:
    if not ratelimit.hit(conn, hash_key, ratelimit.Limit(name, count, window), value):
        log.warning("public rate limit exceeded limit=%s", name)
        raise fail(RATE_LIMITED)


def hash_key_of(settings) -> bytes:
    """The rate-limit HMAC key (PHONE_HASH_KEY), or 503 unavailable."""
    hash_key = settings.phone_hash_key
    if not hash_key or not hash_key.get_secret_value():
        log.error("public API needs PHONE_HASH_KEY")
        raise fail(UNAVAILABLE)
    try:
        return decode_hash_key(hash_key.get_secret_value())
    except CryptoError:
        log.error("public API phone hash key is invalid")
        raise fail(UNAVAILABLE) from None


def read_limit(conn, settings, name, count, address) -> None:
    """Per-IP hourly limit of a public read (catalogue, slots); commits its counter."""
    limit(conn, hash_key_of(settings), name, count, HOUR, client_value(address))


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


def booking_key(prefix: str, idempotency_key: str | None) -> str:
    """The stored idempotency key: the channel prefix plus a SHA-256 of the client's key,
    or a fresh random one (no replay) without it."""
    if idempotency_key:
        return prefix + hashlib.sha256(idempotency_key.encode()).hexdigest()
    return prefix + uuid4().hex


def create_booking(
    url,
    settings,
    clinic,
    offer,
    *,
    starts_at,
    name,
    phone,
    notes,
    channel,
    consent: bool,
    key,
    keyring,
    hash_key,
    gate=None,
    actor_id=None,
    audit_action="public.booking_created",
    after_insert=None,
    ports=None,
) -> dict:
    """The booking core shared by the website, the chat confirm and staff bookings: a new
    patient from the typed name and phone (patients.create_from_form, never a match), the
    contact consents when `consent` (source = channel), encrypted notes, an audit row, all
    inside service.reserve (offered slot re-validated under the doctor row lock, appointment
    and calendar.create job in one commit), then the inline confirm after the commit.
    `offer` is an offerings() row. Returns the public booking shape plus appointment_id."""
    clinic_id = clinic["id"]
    digest = phone_hash(hash_key, phone)
    contact = ratelimit.key_hash(hash_key, "booking-contact", f"{digest.hex()}:{name}")
    reservation = WebReservation(
        doctor_id=offer["doctor_id"],
        service_id=offer["service_row_id"],
        starts_at=starts_at,
        contact=contact.hex(),
    )
    details = patients.PatientDetails(
        name=name, consents={purpose: True for purpose in CONTACT_PURPOSES} if consent else {}
    )

    def attach(conn, starts_at):
        retention_until = starts_at + timedelta(days=settings.patient_retention_days)
        return patients.create_from_form(
            conn, keyring, clinic_id, details, (phone, digest), retention_until, channel
        )

    def inserted(conn, appointment_id, patient_id):
        if notes:
            key_id, blob = keyring.encrypt(notes.encode(), notes_aad(clinic_id, appointment_id))
            conn.execute(
                "UPDATE appointments SET notes_ciphertext=%s, notes_key_id=%s"
                " WHERE clinic_id=%s AND id=%s",
                (blob, key_id, clinic_id, appointment_id),
            )
        if after_insert is not None:
            after_insert(conn, appointment_id, patient_id)
        audit(conn, clinic_id, actor_id, audit_action, appointment_id)

    try:
        result = service.reserve(
            url,
            clinic_id,
            reservation,
            key,
            attach_patient=attach,
            after_insert=inserted,
            gate=gate,
            channel=channel,
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
        "%s booking appointment=%s clinic=%s status=%s",
        "public" if channel == "web" else channel,
        result["appointment_id"],
        clinic_id,
        status,
    )
    return {
        "appointment_id": result["appointment_id"],
        "reference": result["booking_reference"],
        "status": status,
        "starts_at": result["starts_at"].astimezone(ZoneInfo(clinic["timezone"])),
        "doctor_name": offer["doctor_name"],
        "service_name": offer["service_name"],
    }


def public_booking(result: dict) -> dict:
    """The public booking response shape (drops the internal appointment id)."""
    return {key: value for key, value in result.items() if key != "appointment_id"}


def book(url, settings, clinic, request, address, idempotency_key=None, ports=None) -> dict:
    """A web booking (channel web). Order: consent (422), crypto (503), IP and phone rate
    limits (429, counted before any booking work), offering (404), then create_booking.
    idempotency_key (optional, client-chosen) makes a retried submission replay the first
    booking."""
    if request.consent_contact is not True:
        raise PublicError(422, "consent_required", "Consent to be contacted is required to book")
    keyring, hash_key = crypto(settings)
    clinic_id = clinic["id"]
    with connect(url) as conn:
        booking_limits(conn, settings, hash_key, clinic_id, request.phone, address)
        found = offerings(conn, clinic_id, request.service_id, request.doctor_id)
    if not found:
        raise fail(NOT_FOUND)

    def gate(conn):
        # Level 1b analogue (locks.py): the clinic row FOR SHARE, re-checking that the
        # clinic still accepts public bookings inside the write transaction.
        row = conn.execute(
            "SELECT public_enabled FROM clinics WHERE id=%s FOR SHARE", (clinic_id,)
        ).fetchone()
        if row is None or not row["public_enabled"]:
            raise fail(NOT_FOUND)

    result = create_booking(
        url,
        settings,
        clinic,
        found[0],
        starts_at=request.starts_at,
        name=request.name,
        phone=request.phone,
        notes=request.notes,
        channel="web",
        consent=True,
        key=booking_key(KEY_PREFIX, idempotency_key),
        keyring=keyring,
        hash_key=hash_key,
        gate=gate,
        ports=ports,
    )
    return public_booking(result)


def booking_limits(conn, settings, hash_key, clinic_id, phone, address) -> None:
    """The per-IP and per-phone booking limits (web and chat confirm share the buckets)."""
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
        f"{clinic_id}:{phone}",
    )


def lookup(url, settings, clinic, request, address, conn=None) -> dict:
    """One booking by reference + phone. Every mismatch (malformed or unknown reference,
    another clinic's reference, a phone that is not the booking patient's, a booking whose
    patient record was purged) is the same 404 not_found, so the response never reveals
    whether a reference exists. `conn` (the chat turn's connection) is used instead of a
    new one; the caller commits it."""
    _, hash_key = crypto(settings)
    with nullcontext(conn) if conn is not None else connect(url) as conn:
        limit(
            conn,
            hash_key,
            "lookup-ip",
            settings.public_lookup_ip_per_hour,
            HOUR,
            client_value(address),
        )
        limit(
            conn,
            hash_key,
            "lookup-phone",
            settings.public_lookup_phone_per_day,
            DAY,
            f"{clinic['id']}:{request.phone}",
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
