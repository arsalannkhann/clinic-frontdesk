-- Calendar ownership (ADR 0004). booking_calendar_id is the dedicated calendar the system
-- writes appointments to (NULL = not mapped). busy_calendar_ids are read-only advisory inputs.
-- Identifiers are opaque provider ids, never patient data.

-- CHECK constraints cannot contain subqueries, so per-element validation lives in a function.
CREATE FUNCTION calendar_ids_valid(ids text[]) RETURNS boolean
    LANGUAGE sql IMMUTABLE AS $$
    SELECT cardinality(ids) <= 10
        AND NOT EXISTS (
            SELECT 1 FROM unnest(ids) AS c WHERE c IS NULL OR char_length(c) NOT BETWEEN 1 AND 255
        )
$$;

ALTER TABLE doctors
    ADD COLUMN booking_calendar_id text
        CHECK (booking_calendar_id IS NULL OR char_length(booking_calendar_id) BETWEEN 1 AND 255),
    ADD COLUMN busy_calendar_ids text[] NOT NULL DEFAULT '{}'
        CHECK (calendar_ids_valid(busy_calendar_ids));

-- Cached advisory busy time read from external calendars. Never a lock: reservations treat
-- it as one more busy input and the ledger (appointments) stays authoritative.
CREATE TABLE external_busy (
    id uuid PRIMARY KEY,
    clinic_id uuid NOT NULL REFERENCES clinics(id),
    doctor_id uuid NOT NULL,
    starts_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL,
    source_calendar text NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    CHECK (starts_at < ends_at),
    FOREIGN KEY (clinic_id, doctor_id) REFERENCES doctors(clinic_id, id)
);
CREATE INDEX external_busy_lookup ON external_busy (clinic_id, doctor_id, starts_at);
