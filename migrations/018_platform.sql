-- Clinic platform foundation (P1, docs/plans/2026-10-31-platform.md, ADR 0010): public
-- clinic profile, public catalogue fields, booking channel, callback requests, public rate
-- limits, ledger calendars, and Supabase hardening (RLS on, client roles revoked).

-- Public profile. The public API (/v1/public/clinics/{slug}) serves a clinic only while
-- public_slug is set and public_enabled is on; everything else stays private. Texts are
-- marketing copy shown on the clinic website, never patient data. public_voice_number is
-- the number patients may call (E.164; unset until the owner configures one).
-- seed_tag marks rows written by `clinic-voice seed-platform` (synthetic data only); the
-- seed refuses to run on a database holding clinics without it.
ALTER TABLE clinics
    ADD COLUMN public_slug text CHECK (public_slug IS NULL OR public_slug ~ '^[a-z0-9-]{3,40}$'),
    ADD COLUMN public_enabled boolean NOT NULL DEFAULT false,
    ADD COLUMN public_tagline text NOT NULL DEFAULT '' CHECK (char_length(public_tagline) <= 160),
    ADD COLUMN public_address text NOT NULL DEFAULT '' CHECK (char_length(public_address) <= 300),
    ADD COLUMN public_map_url text CHECK (
        public_map_url IS NULL OR (public_map_url ~ '^https://' AND char_length(public_map_url) <= 500)
    ),
    ADD COLUMN public_phone_display text NOT NULL DEFAULT ''
        CHECK (char_length(public_phone_display) <= 32),
    ADD COLUMN public_email text NOT NULL DEFAULT '' CHECK (char_length(public_email) <= 254),
    ADD COLUMN public_hours_note text NOT NULL DEFAULT ''
        CHECK (char_length(public_hours_note) <= 300),
    ADD COLUMN public_about text NOT NULL DEFAULT '' CHECK (char_length(public_about) <= 2000),
    ADD COLUMN public_voice_number text
        CHECK (public_voice_number IS NULL OR public_voice_number ~ '^\+[1-9][0-9]{6,14}$'),
    ADD COLUMN public_chat_enabled boolean NOT NULL DEFAULT false,
    ADD COLUMN seed_tag text CHECK (seed_tag IS NULL OR seed_tag ~ '^[a-z0-9-]{1,40}$'),
    ADD CONSTRAINT clinics_public_slug_key UNIQUE (public_slug),
    ADD CONSTRAINT clinics_public_enabled_check CHECK (NOT public_enabled OR public_slug IS NOT NULL);

-- Public catalogue. Services are per doctor (migration 001); rows that are the same offering
-- at several doctors share a catalog_id, which is the service id the public API shows
-- (COALESCE(catalog_id, id): an unshared row is its own offering). Rows sharing a catalog_id
-- should share name, duration, description and colour; the public list shows one of them.
-- public=false hides a service from the public API (staff and voice still see it). Colours
-- are lowercase #rrggbb for calendar blocks.
ALTER TABLE services
    ADD COLUMN catalog_id uuid,
    ADD COLUMN description text NOT NULL DEFAULT '' CHECK (char_length(description) <= 500),
    ADD COLUMN public boolean NOT NULL DEFAULT true,
    ADD COLUMN colour text CHECK (colour IS NULL OR colour ~ '^#[0-9a-f]{6}$');
CREATE INDEX services_catalog ON services (clinic_id, catalog_id) WHERE catalog_id IS NOT NULL;

-- A ledger calendar id names its own doctor ("ledger:<doctor uuid>"): the platform database
-- is that doctor's calendar of record (providers/ledger.py), so one doctor's appointments
-- can never be projected into another doctor's ledger calendar.
ALTER TABLE doctors
    ADD COLUMN public_bio text NOT NULL DEFAULT '' CHECK (char_length(public_bio) <= 1000),
    ADD COLUMN photo_url text CHECK (
        photo_url IS NULL OR (photo_url ~ '^https://' AND char_length(photo_url) <= 500)
    ),
    ADD COLUMN colour text CHECK (colour IS NULL OR colour ~ '^#[0-9a-f]{6}$'),
    ADD CONSTRAINT doctors_ledger_calendar_check CHECK (
        booking_calendar_id IS NULL OR booking_calendar_id NOT LIKE 'ledger:%'
        OR booking_calendar_id = 'ledger:' || id::text
    );

-- The channel that created the appointment row. Existing rows were made by the voice
-- gateway, the tool API or staff before channels existed; they read as voice. Optional
-- patient notes from a booking form are AES-256-GCM ciphertext (application keyring,
-- associated data binds them to clinic and appointment): they may carry health details.
ALTER TABLE appointments
    ADD COLUMN channel text NOT NULL DEFAULT 'voice'
        CHECK (channel IN ('voice', 'web', 'chat', 'staff')),
    ADD COLUMN notes_ciphertext bytea
        CHECK (notes_ciphertext IS NULL OR length(notes_ciphertext) BETWEEN 29 AND 4096),
    ADD COLUMN notes_key_id text CHECK (notes_key_id IS NULL OR char_length(notes_key_id) BETWEEN 1 AND 32),
    ADD CONSTRAINT appointments_notes_key_check CHECK ((notes_ciphertext IS NULL) = (notes_key_id IS NULL));
CREATE INDEX appointments_clinic_start ON appointments (clinic_id, starts_at);

-- Consent captured on the website or in chat. Migration 012 declared the check inline, so
-- look its generated name up in the catalog.
DO $$
DECLARE
    item record;
BEGIN
    FOR item IN
        SELECT c.conname FROM pg_constraint c
        WHERE c.conrelid = 'consents'::regclass AND c.contype = 'c'
        AND pg_get_constraintdef(c.oid) LIKE '%source%'
    LOOP
        EXECUTE format('ALTER TABLE consents DROP CONSTRAINT %I', item.conname);
    END LOOP;
END
$$;
ALTER TABLE consents
    ADD CONSTRAINT consents_source_check CHECK (source IN ('voice_call', 'staff', 'web', 'chat'));

-- Callback requests from the website, the chat or the voice agent's handoff. Name, phone
-- and reason are AES-256-GCM ciphertext (associated data binds each to clinic, request and
-- field) plus a keyed HMAC of the phone (PHONE_HASH_KEY). patient_id is set only when staff
-- link a request to a patient; a public request never attaches itself to an existing
-- patient. Staff work the queue (P2): open -> calling -> done | dismissed.
CREATE TABLE callback_requests (
    id uuid PRIMARY KEY,
    clinic_id uuid NOT NULL REFERENCES clinics(id),
    patient_id uuid,
    name_ciphertext bytea NOT NULL CHECK (length(name_ciphertext) BETWEEN 29 AND 1024),
    name_key_id text NOT NULL CHECK (char_length(name_key_id) BETWEEN 1 AND 32),
    phone_ciphertext bytea NOT NULL CHECK (length(phone_ciphertext) BETWEEN 29 AND 256),
    phone_key_id text NOT NULL CHECK (char_length(phone_key_id) BETWEEN 1 AND 32),
    phone_hash bytea NOT NULL CHECK (length(phone_hash) = 32),
    reason_ciphertext bytea CHECK (reason_ciphertext IS NULL OR length(reason_ciphertext) BETWEEN 29 AND 4096),
    reason_key_id text CHECK (reason_key_id IS NULL OR char_length(reason_key_id) BETWEEN 1 AND 32),
    source text NOT NULL CHECK (source IN ('web', 'chat', 'voice')),
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'calling', 'done', 'dismissed')),
    outbound_execution_id text
        CHECK (outbound_execution_id IS NULL OR outbound_execution_id ~ '^[A-Za-z0-9-]{1,64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    resolved_by uuid,
    UNIQUE (clinic_id, id),
    FOREIGN KEY (clinic_id, patient_id) REFERENCES patients(clinic_id, id),
    FOREIGN KEY (clinic_id, resolved_by) REFERENCES staff_users(clinic_id, id),
    CHECK ((reason_ciphertext IS NULL) = (reason_key_id IS NULL)),
    CHECK ((status IN ('done', 'dismissed')) = (resolved_at IS NOT NULL)),
    CHECK (resolved_by IS NULL OR resolved_at IS NOT NULL)
);
CREATE INDEX callback_requests_queue ON callback_requests (clinic_id, status, created_at);
CREATE INDEX callback_requests_created ON callback_requests (created_at);

-- Fixed-window counters for the public API. key_hash is an HMAC (PHONE_HASH_KEY) of the
-- limit name and the client address or phone, so neither is stored. Cross-tenant; rows of
-- past windows are pruned by worker housekeeping.
CREATE TABLE public_rate_limits (
    key_hash bytea NOT NULL CHECK (length(key_hash) = 32),
    window_start timestamptz NOT NULL,
    count integer NOT NULL CHECK (count >= 1),
    PRIMARY KEY (key_hash, window_start)
);
CREATE INDEX public_rate_limits_window ON public_rate_limits (window_start);

-- Supabase hardening (ADR 0010). Supabase exposes schema public through PostgREST to the
-- anon and authenticated roles; nothing of this system may be reachable that way. Row level
-- security is enabled on every table (no policies: the table owner, which is the only role
-- the backend connects as, is not subject to RLS without FORCE, and FORCE is not used).
-- This runs on every PostgreSQL, so plain PostgreSQL behaves the same. Migrations that
-- create tables must enable RLS on them too (tests/test_platform.py checks every table).
DO $$
DECLARE
    item record;
BEGIN
    FOR item IN
        SELECT c.oid::regclass AS rel FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
    LOOP
        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', item.rel);
    END LOOP;
END
$$;

-- Revoke every privilege of the client roles on existing and future objects in public. A
-- no-op where the roles do not exist (plain PostgreSQL). Default privileges are revoked for
-- objects created by the role running migrations (on Supabase, postgres).
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
