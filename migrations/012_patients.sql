-- Patients, consent, booking references, verified changes (T7).
--
-- Patient names and phone numbers are stored only as AES-256-GCM ciphertext (application
-- keyring; associated data binds each blob to clinic, patient and field) plus a keyed
-- HMAC-SHA256 of the phone (PHONE_HASH_KEY) for verification lookups. retention_until is
-- extended to (latest appointment start + PATIENT_RETENTION_DAYS) by every booking; the
-- operator-run `clinic-voice retention purge` deletes expired rows. Nothing schedules it.
CREATE TABLE patients (
    id uuid PRIMARY KEY,
    clinic_id uuid NOT NULL REFERENCES clinics(id),
    name_ciphertext bytea
        CHECK (name_ciphertext IS NULL OR length(name_ciphertext) BETWEEN 29 AND 1024),
    name_key_id text CHECK (name_key_id IS NULL OR char_length(name_key_id) BETWEEN 1 AND 32),
    phone_ciphertext bytea
        CHECK (phone_ciphertext IS NULL OR length(phone_ciphertext) BETWEEN 29 AND 256),
    phone_key_id text CHECK (phone_key_id IS NULL OR char_length(phone_key_id) BETWEEN 1 AND 32),
    phone_hash bytea CHECK (phone_hash IS NULL OR length(phone_hash) = 32),
    preferred_language text CHECK (preferred_language IS NULL OR preferred_language IN ('en', 'hi')),
    created_at timestamptz NOT NULL DEFAULT now(),
    retention_until timestamptz NOT NULL,
    UNIQUE (clinic_id, id),
    CHECK ((name_ciphertext IS NULL) = (name_key_id IS NULL)),
    CHECK ((phone_ciphertext IS NULL) = (phone_key_id IS NULL)),
    CHECK ((phone_ciphertext IS NULL) = (phone_hash IS NULL))
);
CREATE INDEX patients_phone_hash ON patients (clinic_id, phone_hash) WHERE phone_hash IS NOT NULL;
CREATE INDEX patients_retention ON patients (retention_until);

-- One row per grant. A revocation stamps revoked_at; at most one active grant per purpose.
CREATE TABLE consents (
    id uuid PRIMARY KEY,
    clinic_id uuid NOT NULL REFERENCES clinics(id),
    patient_id uuid NOT NULL,
    purpose text NOT NULL
        CHECK (purpose IN ('booking_confirmation', 'reminder_call', 'reminder_message')),
    granted_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    source text NOT NULL CHECK (source IN ('voice_call', 'staff')),
    UNIQUE (clinic_id, id),
    FOREIGN KEY (clinic_id, patient_id) REFERENCES patients(clinic_id, id),
    CHECK (revoked_at IS NULL OR revoked_at >= granted_at)
);
CREATE UNIQUE INDEX consents_one_active ON consents (clinic_id, patient_id, purpose)
    WHERE revoked_at IS NULL;
CREATE INDEX consents_patient ON consents (clinic_id, patient_id);

-- Speakable booking reference: 8 Crockford base32 characters (40 random bits). The
-- application generates it with a CSPRNG (secrets) and retries on collision; this default
-- (gen_random_uuid, also a CSPRNG) covers rows inserted by other means and the backfill.
-- Bytes 0-5 and 10-11 of a version 4 UUID are fully random.
CREATE FUNCTION new_booking_reference() RETURNS text
    LANGUAGE sql VOLATILE AS $$
    SELECT string_agg(
        substr('0123456789ABCDEFGHJKMNPQRSTVWXYZ', (get_byte(raw, pos) & 31) + 1, 1), ''
        ORDER BY ord)
    FROM (SELECT uuid_send(gen_random_uuid()) AS raw) AS r,
        unnest(ARRAY[0, 1, 2, 3, 4, 5, 10, 11]) WITH ORDINALITY AS p(pos, ord)
$$;

ALTER TABLE appointments
    ADD COLUMN patient_id uuid,
    ADD COLUMN booking_reference text,
    ADD COLUMN rescheduled_from uuid,
    ADD CONSTRAINT appointments_patient_fkey
        FOREIGN KEY (clinic_id, patient_id) REFERENCES patients(clinic_id, id),
    ADD CONSTRAINT appointments_rescheduled_from_fkey
        FOREIGN KEY (clinic_id, rescheduled_from) REFERENCES appointments(clinic_id, id),
    ADD CONSTRAINT appointments_rescheduled_from_check
        CHECK (rescheduled_from IS NULL OR rescheduled_from <> id);

-- Unique first (NULLs do not collide), then one set-based backfill; a collision aborts the
-- UPDATE inside its subtransaction and it is retried with fresh references.
ALTER TABLE appointments
    ADD CONSTRAINT appointments_booking_reference_key UNIQUE (clinic_id, booking_reference);

DO $$
BEGIN
    FOR attempt IN 1..20 LOOP
        BEGIN
            UPDATE appointments SET booking_reference = new_booking_reference()
            WHERE booking_reference IS NULL;
            RETURN;
        EXCEPTION WHEN unique_violation THEN
            NULL;  -- Retry.
        END;
    END LOOP;
    RAISE EXCEPTION 'could not backfill unique booking references';
END
$$;

ALTER TABLE appointments
    ALTER COLUMN booking_reference SET DEFAULT new_booking_reference(),
    ALTER COLUMN booking_reference SET NOT NULL,
    ADD CONSTRAINT appointments_booking_reference_format
        CHECK (booking_reference ~ '^[0-9A-HJKMNP-TV-Z]{8}$');
-- An appointment is rescheduled at most once (its successor may be rescheduled in turn).
CREATE UNIQUE INDEX appointments_rescheduled_once ON appointments (clinic_id, rescheduled_from)
    WHERE rescheduled_from IS NOT NULL;
CREATE INDEX appointments_patient ON appointments (clinic_id, patient_id)
    WHERE patient_id IS NOT NULL;

-- A successful booking verification: this call session proved booking reference + caller
-- number for this appointment. Verifications never carry over to another session.
CREATE TABLE call_session_verifications (
    clinic_id uuid NOT NULL,
    call_session_id uuid NOT NULL,
    appointment_id uuid NOT NULL,
    verified_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (clinic_id, call_session_id, appointment_id),
    FOREIGN KEY (clinic_id, call_session_id) REFERENCES call_sessions(clinic_id, id)
        ON DELETE CASCADE,
    FOREIGN KEY (clinic_id, appointment_id) REFERENCES appointments(clinic_id, id)
);
CREATE INDEX call_session_verifications_appointment
    ON call_session_verifications (clinic_id, appointment_id);

-- Distinct failed verification attempts of a session, keyed by an HMAC (PHONE_HASH_KEY) of
-- the normalized reference, so a provider retry of the same attempt is not counted twice.
-- call_sessions.verification_failures counts these rows.
CREATE TABLE call_session_verification_failures (
    clinic_id uuid NOT NULL,
    call_session_id uuid NOT NULL,
    attempt_digest bytea NOT NULL CHECK (length(attempt_digest) = 32),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (clinic_id, call_session_id, attempt_digest),
    FOREIGN KEY (clinic_id, call_session_id) REFERENCES call_sessions(clinic_id, id)
        ON DELETE CASCADE
);

-- Cancellations and reschedules. One change ends an appointment, so each appointment has at
-- most one row. Voice-call changes carry the server-derived idempotency key; replays return
-- the recorded outcome. No patient data.
CREATE TABLE appointment_changes (
    id uuid PRIMARY KEY,
    clinic_id uuid NOT NULL REFERENCES clinics(id),
    appointment_id uuid NOT NULL,
    action text NOT NULL CHECK (action IN ('cancel', 'reschedule')),
    from_version integer NOT NULL CHECK (from_version >= 1),
    new_appointment_id uuid,
    source text NOT NULL CHECK (source IN ('voice_call', 'staff')),
    call_session_id uuid,
    staff_user_id uuid,
    idempotency_key text
        CHECK (idempotency_key IS NULL OR char_length(idempotency_key) BETWEEN 1 AND 160),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, appointment_id),
    UNIQUE (clinic_id, idempotency_key),
    FOREIGN KEY (clinic_id, appointment_id) REFERENCES appointments(clinic_id, id),
    FOREIGN KEY (clinic_id, new_appointment_id) REFERENCES appointments(clinic_id, id),
    FOREIGN KEY (clinic_id, call_session_id) REFERENCES call_sessions(clinic_id, id),
    FOREIGN KEY (clinic_id, staff_user_id) REFERENCES staff_users(clinic_id, id),
    CHECK ((action = 'reschedule') = (new_appointment_id IS NOT NULL)),
    CHECK (source <> 'staff' OR staff_user_id IS NOT NULL)
);
CREATE INDEX appointment_changes_session ON appointment_changes (clinic_id, call_session_id)
    WHERE call_session_id IS NOT NULL;
CREATE INDEX appointment_changes_new_appointment ON appointment_changes (clinic_id, new_appointment_id)
    WHERE new_appointment_id IS NOT NULL;
