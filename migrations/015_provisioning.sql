-- Clinic provisioning state machine (T10).
--
-- One provisioning row per clinic. State advances one step per provision.step job:
-- draft -> calendar_connected -> agent_created -> number_assigned -> verified, then the owner
-- activates (active). A permanent provider or configuration error moves to failed; an
-- ambiguous outcome or exhausted retries to manual_review. resume_state is where a reset
-- (owner) returns: the state whose step must run again. version increases on every state
-- change, number selection, reset and activation, and fences jobs (a job carries the
-- version it was enqueued at).
--
-- No secret is stored. tool_credential_id names the tenant credential the agent is known to
-- hold (set when Bolna acknowledged the agent write); attempt_credential_id the one issued
-- for an agent write whose outcome is still unknown (a timeout), which the agent may hold.
-- Their plaintext lived only in worker memory. The owner selects phone_number_id (a Bolna
-- number id; the number itself is never stored). agent_name is the deterministic provider
-- reference ("clinic-<clinic hex>-v1") used to find an agent after an ambiguous create;
-- agent_create_pending is the write-ahead intent set before POST /v2/agent and cleared once
-- the agent id is recorded. The tool-free outbound (reminder) agent is not provisioned
-- here; it stays a manual owner step (clinic-voice bolna set-agent --outbound).
--
-- A clinic set up by hand (bolna set-agent) gets its row from
-- `clinic-voice provisioning activate-manual`: state active with the hand-set agent id and
-- no phone_number_id (the number was routed outside this system). The Bolna gateway refuses
-- booking tools unless the state is active (or verified with clinics.test_calls_enabled),
-- and every tool unless it is verified or active; no row counts as not active.
ALTER TABLE tenant_credentials
    ADD CONSTRAINT tenant_credentials_clinic_id_key UNIQUE (clinic_id, id);

CREATE TABLE provisioning (
    clinic_id uuid PRIMARY KEY REFERENCES clinics(id),
    state text NOT NULL DEFAULT 'draft' CHECK (state IN (
        'draft', 'calendar_connected', 'agent_created', 'number_assigned', 'verified',
        'active', 'failed', 'manual_review'
    )),
    resume_state text NOT NULL DEFAULT 'draft' CHECK (resume_state IN (
        'draft', 'calendar_connected', 'agent_created', 'number_assigned', 'verified'
    )),
    agent_id text CHECK (
        agent_id IS NULL
        OR agent_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    ),
    agent_name text CHECK (agent_name IS NULL OR agent_name ~ '^clinic-[0-9a-f]{32}-v[0-9]{1,4}$'),
    agent_create_pending boolean NOT NULL DEFAULT false,
    tool_credential_id uuid,
    attempt_credential_id uuid,
    phone_number_id text CHECK (phone_number_id IS NULL OR phone_number_id ~ '^[A-Za-z0-9-]{1,64}$'),
    calendar_attempts integer NOT NULL DEFAULT 0 CHECK (calendar_attempts >= 0),
    agent_attempts integer NOT NULL DEFAULT 0 CHECK (agent_attempts >= 0),
    number_attempts integer NOT NULL DEFAULT 0 CHECK (number_attempts >= 0),
    verify_attempts integer NOT NULL DEFAULT 0 CHECK (verify_attempts >= 0),
    -- Short machine code only; never provider text.
    last_error text CHECK (last_error IS NULL OR last_error ~ '^[a-z_]{1,64}$'),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (clinic_id, tool_credential_id) REFERENCES tenant_credentials(clinic_id, id),
    FOREIGN KEY (clinic_id, attempt_credential_id) REFERENCES tenant_credentials(clinic_id, id),
    CHECK (state NOT IN ('agent_created', 'number_assigned', 'verified', 'active')
        OR agent_id IS NOT NULL),
    CHECK (state NOT IN ('number_assigned', 'verified') OR phone_number_id IS NOT NULL)
);
-- The Bolna account is shared by every clinic: one number serves one clinic.
CREATE UNIQUE INDEX provisioning_phone_number ON provisioning (phone_number_id)
    WHERE phone_number_id IS NOT NULL;
CREATE UNIQUE INDEX provisioning_agent ON provisioning (agent_id) WHERE agent_id IS NOT NULL;

-- Append-only history of provisioning transitions and refusals (from_state = to_state).
-- outcome is a short code; actor_id is the staff user, NULL for the worker. No provider
-- bodies, numbers or credentials.
CREATE TABLE provisioning_events (
    id uuid PRIMARY KEY,
    seq bigint GENERATED ALWAYS AS IDENTITY,
    clinic_id uuid NOT NULL REFERENCES clinics(id),
    from_state text NOT NULL,
    to_state text NOT NULL,
    step text NOT NULL CHECK (step IN (
        'calendar', 'agent', 'number', 'verify', 'select_number', 'activate', 'reset'
    )),
    outcome text NOT NULL CHECK (outcome ~ '^[a-z_]{1,64}$'),
    actor_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (clinic_id, actor_id) REFERENCES staff_users(clinic_id, id)
);
CREATE INDEX provisioning_events_clinic ON provisioning_events (clinic_id, seq);

CREATE FUNCTION provisioning_events_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'provisioning_events is append-only' USING ERRCODE = 'insufficient_privilege';
END
$$;
CREATE TRIGGER provisioning_events_append_only
    BEFORE UPDATE OR DELETE ON provisioning_events
    FOR EACH ROW EXECUTE FUNCTION provisioning_events_append_only();

-- At most one unfinished provision.step job per clinic, so concurrent advances cannot
-- enqueue two steps.
CREATE UNIQUE INDEX outbox_provision_step ON outbox (clinic_id)
    WHERE kind = 'provision.step' AND status IN ('pending', 'processing');

-- Clinic-approved scripts the agent must use for emergencies and human handoff. Editing a
-- text clears the approval unless the same change approves it; only an owner approves.
-- Activation requires both non-empty and approved; the Bolna context tool serves them once
-- approved (placeholders otherwise).
--
-- test_calls_enabled (owner only, audited, default false): while provisioning is verified
-- (not yet active), the gateway also allows booking-changing tools, so the owner can place
-- test calls before activation.
ALTER TABLE clinics
    ADD COLUMN test_calls_enabled boolean NOT NULL DEFAULT false,
    ADD COLUMN emergency_text text NOT NULL DEFAULT ''
        CHECK (char_length(emergency_text) <= 1000),
    ADD COLUMN handoff_text text NOT NULL DEFAULT '' CHECK (char_length(handoff_text) <= 1000),
    ADD COLUMN scripts_approved boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT clinics_scripts_approved_check
        CHECK (NOT scripts_approved OR (emergency_text <> '' AND handoff_text <> ''));
