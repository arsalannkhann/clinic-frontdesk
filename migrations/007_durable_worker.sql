-- Durable worker (T4). Appointments gain a version (a reschedule bumps it and the calendar
-- event id with it), the verified calendar event id and an update stamp. Outbox jobs gain a
-- lease (owner + expiry) that fences completion, a short error code, a payload and a retry cap.

ALTER TABLE appointments
    ADD COLUMN version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    ADD COLUMN calendar_event_id text
        CHECK (calendar_event_id IS NULL OR char_length(calendar_event_id) BETWEEN 1 AND 1024),
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();

ALTER TABLE outbox
    ADD COLUMN appointment_version integer NOT NULL DEFAULT 1 CHECK (appointment_version >= 1),
    ADD COLUMN lease_owner text CHECK (lease_owner IS NULL OR char_length(lease_owner) BETWEEN 1 AND 128),
    ADD COLUMN lease_expires_at timestamptz,
    -- Short machine codes only ("timeout", "external_conflict"); never provider bodies or PII.
    ADD COLUMN last_error text CHECK (last_error IS NULL OR char_length(last_error) <= 64),
    ADD COLUMN payload jsonb NOT NULL DEFAULT '{}',
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN max_attempts integer NOT NULL DEFAULT 8 CHECK (max_attempts >= 1);

-- Migration 001 declared these constraints inline, so their names were generated. Look them
-- up in the catalog rather than assuming the generated names.
DO $$
DECLARE
    item record;
BEGIN
    FOR item IN
        SELECT c.conname FROM pg_constraint c
        WHERE c.conrelid = 'outbox'::regclass AND (
            (c.contype = 'u' AND (
                SELECT array_agg(a.attname::text ORDER BY a.attname)
                FROM unnest(c.conkey) AS k(attnum)
                JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
            ) = ARRAY['appointment_id', 'kind'])
            OR (c.contype = 'c' AND pg_get_constraintdef(c.oid) LIKE '%kind = ANY%')
        )
    LOOP
        EXECUTE format('ALTER TABLE outbox DROP CONSTRAINT %I', item.conname);
    END LOOP;
END
$$;

ALTER TABLE outbox
    ALTER COLUMN appointment_id DROP NOT NULL,
    ADD CONSTRAINT outbox_appointment_version_key UNIQUE (appointment_id, kind, appointment_version),
    ADD CONSTRAINT outbox_kind_check CHECK (kind IN (
        'calendar.create', 'calendar.cancel', 'calendar.refresh_busy', 'notification.send',
        'reminder.schedule', 'reminder.stop', 'reminder.call', 'webhook.process',
        'reconcile.execution', 'provision.step'
    )),
    -- Appointment calendar projections need their appointment. calendar.refresh_busy is a
    -- per-doctor job with no appointment, so it is exempt.
    ADD CONSTRAINT outbox_calendar_appointment_check
        CHECK (kind NOT IN ('calendar.create', 'calendar.cancel') OR appointment_id IS NOT NULL),
    ADD CONSTRAINT outbox_lease_check
        CHECK (status <> 'processing' OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL));

-- outbox_pending (available_at WHERE status='pending') already serves due pending jobs.
CREATE INDEX outbox_expired_leases ON outbox (lease_expires_at) WHERE status = 'processing';
CREATE INDEX outbox_clinic_status ON outbox (clinic_id, status, updated_at);
