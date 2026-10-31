-- Chat assistant, staff-initiated outbound callbacks and synthetic contact guard (P2,
-- docs/plans/2026-10-31-platform.md, ADR 0011).

-- Synthetic contacts. `clinic-voice seed-platform` writes fictional patients and callback
-- requests whose +91 90000 0xxxx numbers may belong to real people. They are marked here,
-- and every outbound path (reminder planning and sending, staff "Call now") refuses a
-- synthetic target with code synthetic_contact, whatever the clinic's outbound settings.
ALTER TABLE patients ADD COLUMN synthetic boolean NOT NULL DEFAULT false;
ALTER TABLE callback_requests ADD COLUMN synthetic boolean NOT NULL DEFAULT false;

-- Outbound callbacks. The caller id (Bolna from_phone_number, E.164) staff callbacks are
-- placed from, configured by the owner (`clinic-voice bolna set-outbound-number`); a call is
-- refused without it. The callback row records who asked for the call and when, and the
-- clinic-local day it counts against in the daily contact budget (shared with reminders).
ALTER TABLE clinics
    ADD COLUMN bolna_outbound_from_number text CHECK (
        bolna_outbound_from_number IS NULL OR bolna_outbound_from_number ~ '^\+[1-9][0-9]{6,14}$'
    );
ALTER TABLE callback_requests
    ADD COLUMN call_requested_at timestamptz,
    ADD COLUMN call_requested_by uuid,
    ADD COLUMN call_contact_day date,
    ADD COLUMN call_outcome text CHECK (call_outcome IS NULL OR call_outcome ~ '^[a-z_]{1,32}$'),
    ADD CONSTRAINT callback_requests_called_by_fkey
        FOREIGN KEY (clinic_id, call_requested_by) REFERENCES staff_users(clinic_id, id),
    ADD CONSTRAINT callback_requests_call_check
        CHECK ((call_requested_at IS NULL) = (call_contact_day IS NULL));
CREATE INDEX callback_requests_contact_day ON callback_requests (clinic_id, call_contact_day)
    WHERE call_contact_day IS NOT NULL;

-- Chat assistant (ADR 0011). A session is addressed by a 128-bit random bearer secret that
-- only the patient's browser holds; the table stores its SHA-256 (token_hash), never the
-- secret. ip_hash is an HMAC (PHONE_HASH_KEY) of the client address at creation, for abuse
-- review only. patient_id is set when a proposal from this chat was booked. synthetic marks
-- sessions written by the seed.
CREATE TABLE chat_sessions (
    id uuid PRIMARY KEY,
    clinic_id uuid NOT NULL REFERENCES clinics(id),
    token_hash bytea NOT NULL UNIQUE CHECK (length(token_hash) = 32),
    created_at timestamptz NOT NULL DEFAULT now(),
    last_active_at timestamptz NOT NULL DEFAULT now(),
    ip_hash bytea CHECK (ip_hash IS NULL OR length(ip_hash) = 32),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'closed')),
    turn_count integer NOT NULL DEFAULT 0 CHECK (turn_count >= 0),
    patient_id uuid,
    synthetic boolean NOT NULL DEFAULT false,
    UNIQUE (clinic_id, id),
    FOREIGN KEY (clinic_id, patient_id) REFERENCES patients(clinic_id, id)
);
CREATE INDEX chat_sessions_clinic ON chat_sessions (clinic_id, last_active_at DESC);
CREATE INDEX chat_sessions_created ON chat_sessions (created_at);

-- The transcript, in order (seq per session). content is AES-256-GCM ciphertext of a JSON
-- document (application keyring; associated data binds it to clinic, session and seq): the
-- visible text, and for the model's own bookkeeping its tool calls and tool results. Chats
-- carry health details, so nothing is stored in clear except the role and tool name.
-- 'system' rows are notes the server adds (a booking confirmed from a proposal).
CREATE TABLE chat_messages (
    clinic_id uuid NOT NULL,
    session_id uuid NOT NULL,
    seq integer NOT NULL CHECK (seq >= 1),
    role text NOT NULL CHECK (role IN ('user', 'assistant', 'tool', 'system')),
    content_ciphertext bytea NOT NULL CHECK (length(content_ciphertext) BETWEEN 29 AND 65536),
    content_key_id text NOT NULL CHECK (char_length(content_key_id) BETWEEN 1 AND 32),
    tool_name text CHECK (tool_name IS NULL OR tool_name ~ '^[a-z_]{1,40}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, seq),
    FOREIGN KEY (clinic_id, session_id) REFERENCES chat_sessions(clinic_id, id) ON DELETE CASCADE
);

-- A slot the assistant offered for booking (propose_booking). It never holds the slot and
-- is not an appointment: only the patient's confirm books it, through the same booking
-- path as the website, at most once (consumed_at, appointment_id) and before expires_at.
CREATE TABLE chat_proposals (
    id uuid PRIMARY KEY,
    clinic_id uuid NOT NULL,
    session_id uuid NOT NULL,
    doctor_id uuid NOT NULL,
    service_id uuid NOT NULL,
    starts_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    appointment_id uuid,
    UNIQUE (clinic_id, id),
    FOREIGN KEY (clinic_id, session_id) REFERENCES chat_sessions(clinic_id, id) ON DELETE CASCADE,
    FOREIGN KEY (clinic_id, doctor_id) REFERENCES doctors(clinic_id, id),
    FOREIGN KEY (clinic_id, appointment_id) REFERENCES appointments(clinic_id, id),
    CHECK (expires_at > created_at),
    CHECK (appointment_id IS NULL OR consumed_at IS NOT NULL)
);
CREATE INDEX chat_proposals_session ON chat_proposals (clinic_id, session_id, created_at);

-- Supabase hardening for the new tables (ADR 0010): RLS on, client roles revoked. Same
-- guard as migration 018, so it is a no-op where the roles do not exist.
ALTER TABLE chat_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_proposals ENABLE ROW LEVEL SECURITY;
DO $$
DECLARE
    client text;
BEGIN
    FOREACH client IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = client) THEN
            EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', client);
            EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %I', client);
            EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM %I', client);
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM %I', client);
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM %I', client);
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM %I', client);
        END IF;
    END LOOP;
END
$$;
