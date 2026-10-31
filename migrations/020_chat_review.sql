-- P2 review fixes (ADR 0011): chat proposal service integrity, one chat turn at a time, and
-- the history of reopened callback calls.

-- chat_proposals.service_id had no foreign key. Services are addressed per clinic like every
-- other tenant table, so add the (clinic_id, id) key they lacked (id is already the primary
-- key, so this cannot fail on existing rows) and reference it.
ALTER TABLE services ADD CONSTRAINT services_clinic_id_id_key UNIQUE (clinic_id, id);
ALTER TABLE chat_proposals
    ADD CONSTRAINT chat_proposals_service_fkey
        FOREIGN KEY (clinic_id, service_id) REFERENCES services(clinic_id, id);

-- One turn per chat session at a time (chat.send). A turn marks its session busy until a
-- short lease ends and clears the mark when it finishes (compare-and-set on the value it
-- wrote); a concurrent message gets 409 turn_in_progress. A lease, not a transaction-scoped
-- advisory lock, because a turn spans model calls of up to CHAT_LLM_TIMEOUT_SECONDS each and
-- holding a pooled transaction open that long would pin a server connection of the
-- transaction pooler; a crashed turn frees the session when its lease expires.
ALTER TABLE chat_sessions ADD COLUMN busy_until timestamptz;

-- Callback calls that staff reopened (POST /v1/staff/callbacks/{id}/reopen) so a missed call
-- can be retried deliberately. Reopening moves the request's last call here: it keeps
-- counting against the daily contact budget of its clinic-local day (reminders.budget_used)
-- and keeps its outcome and execution id for review. Ids, days and codes only; no contact
-- details. Deleted with the request (retention).
CREATE TABLE callback_call_attempts (
    id uuid PRIMARY KEY,
    clinic_id uuid NOT NULL REFERENCES clinics(id),
    callback_request_id uuid NOT NULL,
    contact_day date NOT NULL,
    requested_at timestamptz NOT NULL,
    requested_by uuid,
    outcome text CHECK (outcome IS NULL OR outcome ~ '^[a-z_]{1,32}$'),
    execution_id text CHECK (execution_id IS NULL OR execution_id ~ '^[A-Za-z0-9-]{1,64}$'),
    reopened_at timestamptz NOT NULL DEFAULT now(),
    reopened_by uuid NOT NULL,
    FOREIGN KEY (clinic_id, callback_request_id)
        REFERENCES callback_requests(clinic_id, id) ON DELETE CASCADE,
    FOREIGN KEY (clinic_id, requested_by) REFERENCES staff_users(clinic_id, id),
    FOREIGN KEY (clinic_id, reopened_by) REFERENCES staff_users(clinic_id, id)
);
CREATE INDEX callback_call_attempts_day ON callback_call_attempts (clinic_id, contact_day);
CREATE INDEX callback_call_attempts_request
    ON callback_call_attempts (clinic_id, callback_request_id);

-- Supabase hardening for the new table (ADR 0010): RLS on, client roles revoked. Same guard
-- as migrations 018 and 019, so it is a no-op where the roles do not exist.
ALTER TABLE callback_call_attempts ENABLE ROW LEVEL SECURITY;
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
