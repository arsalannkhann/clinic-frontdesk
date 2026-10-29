-- Bolna execution webhooks and reconciliation (T8).

-- The clinic's Bolna agent id (canonical lowercase UUID), set by an operator. When set, a
-- webhook whose payload names a different agent is refused. It never selects the tenant:
-- the webhook credential in the URL does.
ALTER TABLE clinics
    ADD COLUMN bolna_agent_id text CHECK (
        bolna_agent_id IS NULL
        OR bolna_agent_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    );

-- Webhook credentials share tenant_credentials (SHA-256 digest of a 256-bit secret, rotation
-- with grace, revocation) under scope 'webhook'. Migration 002 declared the scope check
-- inline, so look its generated name up in the catalog.
DO $$
DECLARE
    item record;
BEGIN
    FOR item IN
        SELECT c.conname FROM pg_constraint c
        WHERE c.conrelid = 'tenant_credentials'::regclass AND c.contype = 'c'
        AND pg_get_constraintdef(c.oid) LIKE '%scope%'
    LOOP
        EXECUTE format('ALTER TABLE tenant_credentials DROP CONSTRAINT %I', item.conname);
    END LOOP;
END
$$;
ALTER TABLE tenant_credentials
    ADD CONSTRAINT tenant_credentials_scope_check CHECK (scope IN ('tools', 'webhook'));

-- Provider-reported call state, from webhooks or reconciliation. Codes only (letters,
-- underscore and hyphen; no digits, so no phone numbers): no transcript, recording,
-- extracted data, numbers or free text. provider_event_at is the latest provider
-- updated_at applied; an equal-rank event older than it is stale. provider_final is set once a final
-- status (completed, balance-low, busy, no-answer, canceled, failed, stopped, error) is
-- applied; the status then never changes. Reconciliation polls unfinished sessions with
-- backoff (next_reconcile_at, reconcile_count).
ALTER TABLE call_sessions
    ADD COLUMN provider_status text
        CHECK (provider_status IS NULL OR provider_status ~ '^[a-z][a-z-]{0,31}$'),
    ADD COLUMN provider_final boolean NOT NULL DEFAULT false,
    ADD COLUMN provider_updated_at timestamptz,
    ADD COLUMN provider_event_at timestamptz,
    ADD COLUMN termination_reason text
        CHECK (termination_reason IS NULL OR termination_reason ~ '^[A-Za-z][A-Za-z_-]{0,63}$'),
    ADD COLUMN hangup_reason text
        CHECK (hangup_reason IS NULL OR hangup_reason ~ '^[A-Za-z][A-Za-z_-]{0,63}$'),
    ADD COLUMN hangup_by text CHECK (hangup_by IS NULL OR hangup_by ~ '^[A-Za-z][A-Za-z_-]{0,63}$'),
    ADD COLUMN hangup_provider_code integer,
    ADD COLUMN call_type text CHECK (call_type IS NULL OR call_type IN ('inbound', 'outbound')),
    ADD COLUMN conversation_seconds double precision
        CHECK (conversation_seconds IS NULL OR conversation_seconds >= 0),
    ADD COLUMN total_cost double precision CHECK (total_cost IS NULL OR total_cost >= 0),
    ADD COLUMN next_reconcile_at timestamptz,
    ADD COLUMN reconcile_count integer NOT NULL DEFAULT 0 CHECK (reconcile_count >= 0),
    ADD CONSTRAINT call_sessions_provider_final_check
        CHECK (NOT provider_final OR provider_status IS NOT NULL);
CREATE INDEX call_sessions_unfinished ON call_sessions (started_at) WHERE NOT provider_final;

-- Every accepted delivery, minimized to an allowlist of fields, in order of receipt
-- (received_seq). The tenant comes from the authenticated webhook credential, never from
-- the payload. Bolna events carry no event id, so event_key is
-- "<execution id>:<status>:<sha256 of the canonical minimized payload>"; a redelivery of
-- the same event is a no-op.
CREATE TABLE webhook_inbox (
    id uuid PRIMARY KEY,
    clinic_id uuid NOT NULL REFERENCES clinics(id),
    provider text NOT NULL DEFAULT 'bolna' CHECK (provider = 'bolna'),
    received_seq bigint GENERATED ALWAYS AS IDENTITY,
    event_key text NOT NULL CHECK (char_length(event_key) BETWEEN 1 AND 200),
    execution_id text NOT NULL CHECK (char_length(execution_id) BETWEEN 1 AND 64),
    provider_status text NOT NULL CHECK (provider_status ~ '^[a-z][a-z-]{0,31}$'),
    payload jsonb NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz,
    status text NOT NULL DEFAULT 'received'
        CHECK (status IN ('received', 'processed', 'failed', 'ignored')),
    -- Short code: applied, stale, duplicate, unknown_execution, unknown_status, agent_mismatch.
    outcome text CHECK (outcome IS NULL OR outcome ~ '^[a-z_]{1,32}$'),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    UNIQUE (clinic_id, event_key),
    UNIQUE (clinic_id, id),
    CHECK ((status = 'received') = (processed_at IS NULL))
);
CREATE INDEX webhook_inbox_execution ON webhook_inbox (clinic_id, execution_id, received_seq);
CREATE INDEX webhook_inbox_received ON webhook_inbox (received_at);

-- bolna.reconcile: poll GET /executions/{id} for a session without a final status. It
-- replaces the reserved, never-used 'reconcile.execution' kind, which is dropped.
ALTER TABLE outbox
    DROP CONSTRAINT outbox_kind_check,
    ADD CONSTRAINT outbox_kind_check CHECK (kind IN (
        'calendar.create', 'calendar.cancel', 'calendar.refresh_busy', 'calendar.revoke_grant',
        'notification.send', 'reminder.schedule', 'reminder.stop', 'reminder.call',
        'webhook.process', 'bolna.reconcile', 'provision.step'
    ));
