-- Bolna call sessions (T6). One row per (clinic, Bolna execution id), created by the trusted
-- gateway on the first tool or context call of an execution. The caller number is
-- provider-asserted (Bolna's from_number context variable): it is stored only as AES-256-GCM
-- ciphertext (application keyring; associated data binds it to (clinic_id, id)) plus a keyed
-- HMAC-SHA256 (PHONE_HASH_KEY) for later lookups. It is never identity proof.
CREATE TABLE call_sessions (
    id uuid PRIMARY KEY,
    clinic_id uuid NOT NULL REFERENCES clinics(id),
    provider text NOT NULL DEFAULT 'bolna' CHECK (provider = 'bolna'),
    execution_id text NOT NULL CHECK (char_length(execution_id) BETWEEN 1 AND 64),
    agent_id text CHECK (agent_id IS NULL OR char_length(agent_id) BETWEEN 1 AND 64),
    caller_number_ciphertext bytea
        CHECK (caller_number_ciphertext IS NULL OR length(caller_number_ciphertext) BETWEEN 29 AND 256),
    caller_number_key_id text
        CHECK (caller_number_key_id IS NULL OR char_length(caller_number_key_id) BETWEEN 1 AND 32),
    caller_number_hash bytea CHECK (caller_number_hash IS NULL OR length(caller_number_hash) = 32),
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'ended')),
    verification_failures integer NOT NULL DEFAULT 0 CHECK (verification_failures >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, execution_id),
    UNIQUE (clinic_id, id),
    CHECK ((caller_number_ciphertext IS NULL) = (caller_number_key_id IS NULL)),
    CHECK ((caller_number_ciphertext IS NULL) = (caller_number_hash IS NULL)),
    CHECK ((status = 'ended') = (ended_at IS NOT NULL))
);
CREATE INDEX call_sessions_caller_hash ON call_sessions (clinic_id, caller_number_hash)
    WHERE caller_number_hash IS NOT NULL;

-- The call session that created an appointment through the Bolna gateway (NULL for the
-- generic /v1/tools API). appointment-status over the gateway only reports appointments of
-- the same session.
ALTER TABLE appointments
    ADD COLUMN call_session_id uuid,
    ADD CONSTRAINT appointments_call_session_fkey
        FOREIGN KEY (clinic_id, call_session_id) REFERENCES call_sessions(clinic_id, id);
CREATE INDEX appointments_call_session ON appointments (clinic_id, call_session_id)
    WHERE call_session_id IS NOT NULL;
