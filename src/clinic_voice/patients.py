"""Patient records and consent (T7).

A patient row holds an encrypted name, an encrypted phone number plus its keyed hash, a
preferred language and retention_until. The phone is never taken from a tool argument: the
gateway copies the provider-asserted caller number of the call session (decrypted from
call_sessions and re-encrypted under the patient's associated data) together with its HMAC.

Attach policy (ADR 0006): a reservation with a caller name and a caller number attaches to an
existing patient of the same clinic only when exactly one patient with that phone hash has
that name (case and whitespace folded). Otherwise, including every reservation without a
name, it creates a new patient row. Shared family phones therefore never merge people by
number alone, and a duplicate row is the worst case of an ambiguous match.

Authority over an existing patient (ADR 0006): matching phone + name is not verification,
so an attach may only add. Consent grants are applied; a decline revokes an active grant only
when the patient row was created in this transaction or the call session has verified an
appointment of that patient. Otherwise the decline is recorded nowhere (it is neither applied
nor turned into a grant). The preferred language likewise only fills a NULL unless the patient
is new or verified in this session.

Nothing here logs or returns names or numbers.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from clinic_voice.crypto import CryptoError, Keyring

log = logging.getLogger("clinic_voice.patients")

PURPOSES = ("booking_confirmation", "reminder_call", "reminder_message")


@dataclass(frozen=True)
class PatientDetails:
    name: str | None = None
    language: str | None = None
    # purpose -> True (granted) or False (declined: revokes an active grant). A purpose the
    # caller was not asked about is absent.
    consents: dict[str, bool] = field(default_factory=dict)


def field_aad(clinic_id: UUID, patient_id: UUID, name: str) -> bytes:
    return b"patient:" + clinic_id.bytes + patient_id.bytes + b":" + name.encode()


def session_aad(clinic_id: UUID, session_id: UUID) -> bytes:
    # Associated data of call_sessions.caller_number_ciphertext; bolna.gateway imports it.
    return b"call_session:" + clinic_id.bytes + session_id.bytes


def _normal(name: str) -> str:
    return " ".join(name.split()).casefold()


def decrypt_field(keyring: Keyring, clinic_id, patient_id, name, key_id, blob) -> str | None:
    if blob is None or key_id is None:
        return None
    try:
        return keyring.decrypt(key_id, blob, field_aad(clinic_id, patient_id, name)).decode()
    except CryptoError:
        log.error("patient field could not be decrypted field=%s", name)
        return None


def caller_number(conn, keyring: Keyring, clinic_id, session_id) -> tuple[str, bytes] | None:
    """The session's stored caller number and its hash, or None if none was recorded."""
    row = conn.execute(
        """SELECT caller_number_ciphertext, caller_number_key_id, caller_number_hash
        FROM call_sessions WHERE clinic_id=%s AND id=%s""",
        (clinic_id, session_id),
    ).fetchone()
    if row is None or row["caller_number_hash"] is None:
        return None
    try:
        number = keyring.decrypt(
            row["caller_number_key_id"],
            row["caller_number_ciphertext"],
            session_aad(clinic_id, session_id),
        ).decode()
    except CryptoError:
        log.error("call session caller number could not be decrypted")
        return None
    return number, bytes(row["caller_number_hash"])


def _matching(conn, keyring, clinic_id, digest, name) -> list[UUID]:
    rows = conn.execute(
        """SELECT id, name_ciphertext, name_key_id FROM patients
        WHERE clinic_id=%s AND phone_hash=%s AND name_ciphertext IS NOT NULL
        ORDER BY created_at, id""",
        (clinic_id, digest),
    ).fetchall()
    wanted = _normal(name)
    return [
        row["id"]
        for row in rows
        if (
            stored := decrypt_field(
                keyring, clinic_id, row["id"], "name", row["name_key_id"], row["name_ciphertext"]
            )
        )
        is not None
        and _normal(stored) == wanted
    ]


def _create(conn, keyring, clinic_id, details, phone, retention_until) -> UUID:
    patient_id = uuid4()
    name_key, name_blob = (None, None)
    if details.name:
        name_key, name_blob = keyring.encrypt(
            details.name.encode(), field_aad(clinic_id, patient_id, "name")
        )
    phone_key, phone_blob, digest = (None, None, None)
    if phone is not None:
        number, digest = phone
        phone_key, phone_blob = keyring.encrypt(
            number.encode(), field_aad(clinic_id, patient_id, "phone")
        )
    conn.execute(
        """INSERT INTO patients (id,clinic_id,name_ciphertext,name_key_id,phone_ciphertext,
        phone_key_id,phone_hash,preferred_language,retention_until)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            patient_id,
            clinic_id,
            name_blob,
            name_key,
            phone_blob,
            phone_key,
            digest,
            details.language,
            retention_until,
        ),
    )
    return patient_id


def extend_retention(conn, clinic_id, patient_id, retention_until: datetime) -> None:
    conn.execute(
        "UPDATE patients SET retention_until = GREATEST(retention_until, %s)"
        " WHERE clinic_id=%s AND id=%s",
        (retention_until, clinic_id, patient_id),
    )


def _lock_existing(conn, clinic_id, patient_id) -> bool:
    """Lock a matched patient row; False if it is gone (e.g. deleted by a concurrent
    retention purge, which locks the same row before deleting it)."""
    return (
        conn.execute(
            "SELECT id FROM patients WHERE clinic_id=%s AND id=%s FOR UPDATE",
            (clinic_id, patient_id),
        ).fetchone()
        is not None
    )


def verified_in_session(conn, clinic_id, session_id, patient_id) -> bool:
    """True if this call session verified (reference + caller number) an appointment of
    this patient."""
    return (
        conn.execute(
            """SELECT 1 FROM call_session_verifications v
            JOIN appointments a ON a.clinic_id=v.clinic_id AND a.id=v.appointment_id
            WHERE v.clinic_id=%s AND v.call_session_id=%s AND a.patient_id=%s LIMIT 1""",
            (clinic_id, session_id, patient_id),
        ).fetchone()
        is not None
    )


def record_consents(
    conn, clinic_id, patient_id, consents: dict[str, bool], source, allow_revoke: bool
) -> None:
    """Grants are always recorded. A decline revokes an active grant only with
    allow_revoke; without it the decline is ignored (nothing is stored)."""
    for purpose, granted in consents.items():
        if purpose not in PURPOSES:
            raise ValueError("unknown consent purpose")
        if granted:
            conn.execute(
                """INSERT INTO consents (id,clinic_id,patient_id,purpose,source)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (clinic_id, patient_id, purpose) WHERE revoked_at IS NULL
                DO NOTHING""",
                (uuid4(), clinic_id, patient_id, purpose, source),
            )
        elif allow_revoke:
            conn.execute(
                "UPDATE consents SET revoked_at=now() WHERE clinic_id=%s AND patient_id=%s"
                " AND purpose=%s AND revoked_at IS NULL",
                (clinic_id, patient_id, purpose),
            )


def attach(
    conn,
    keyring: Keyring,
    clinic_id,
    session_id,
    details: PatientDetails,
    retention_until: datetime,
) -> UUID | None:
    """Create or attach the patient for a gateway reservation inside its transaction and
    record consent. Returns None (no patient row) when neither a caller number nor a name is
    known. Lock order 4 (locks.py): a per-(clinic, phone hash) advisory lock, after the
    doctor rows, so concurrent bookings from one phone attach consistently."""
    phone = caller_number(conn, keyring, clinic_id, session_id)
    if phone is None and not details.name:
        return None
    patient_id = None
    if phone is not None and details.name:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"patient:{clinic_id}:{phone[1].hex()}",),
        )
        matches = _matching(conn, keyring, clinic_id, phone[1], details.name)
        if len(matches) == 1 and _lock_existing(conn, clinic_id, matches[0]):
            patient_id = matches[0]
    if patient_id is None:
        patient_id = _create(conn, keyring, clinic_id, details, phone, retention_until)
        authorized = True  # Created in this transaction.
    else:
        extend_retention(conn, clinic_id, patient_id, retention_until)
        authorized = verified_in_session(conn, clinic_id, session_id, patient_id)
        if details.language:
            # Unverified callers may only fill a missing language, never change one.
            conn.execute(
                "UPDATE patients SET preferred_language ="
                " CASE WHEN %s THEN %s ELSE COALESCE(preferred_language, %s) END"
                " WHERE clinic_id=%s AND id=%s",
                (authorized, details.language, details.language, clinic_id, patient_id),
            )
    record_consents(
        conn, clinic_id, patient_id, details.consents, "voice_call", allow_revoke=authorized
    )
    return patient_id
