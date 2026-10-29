-- T12: credential scopes for the Bolna gateway, tool-API status scoping, and the owner's
-- choice of whether booking references appear in patient messages.

-- A 'bolna' credential (cvb_ prefix) authenticates /v1/bolna/* only. Provisioning issues it
-- for the agent it creates, so the agent's credential cannot reach the generic /v1/tools/*
-- API, which accepts 'tools' credentials only. /v1/bolna/* still accepts 'tools' credentials
-- for clinics configured by hand (docs/OPERATIONS.md). Existing rows keep their scope: a
-- provisioning credential issued before this migration stays 'tools' (its token carries the
-- cvt_ prefix); development databases that applied earlier T10 builds should be rebuilt.
ALTER TABLE tenant_credentials DROP CONSTRAINT tenant_credentials_scope_check;
ALTER TABLE tenant_credentials
    ADD CONSTRAINT tenant_credentials_scope_check CHECK (scope IN ('tools', 'webhook', 'bolna'));

-- The /v1/tools credential that reserved the appointment (NULL for every other path: the
-- Bolna gateway, reschedules, staff). /v1/tools/appointment-status reads only appointments
-- whose tool_credential_id is the caller's own credential id (ADR 0005), so a tools
-- credential cannot read arbitrary appointments of its clinic by UUID.
ALTER TABLE appointments
    ADD COLUMN tool_credential_id uuid,
    ADD CONSTRAINT appointments_tool_credential_fkey
        FOREIGN KEY (clinic_id, tool_credential_id) REFERENCES tenant_credentials(clinic_id, id);

-- Owner decision (ADR 0008): the booking reference is a verification factor on inbound calls,
-- so confirmation and reminder messages leave it out unless an owner turns this on. Owner
-- only, audited ('clinic.reference_in_messages_enabled' / '..._disabled').
ALTER TABLE clinics
    ADD COLUMN include_reference_in_messages boolean NOT NULL DEFAULT false;
