-- Staff accounts belong to exactly one clinic. Email is globally unique so login needs no
-- caller-supplied tenant. Passwords are scrypt hashes; sessions store only a SHA-256 digest.
CREATE TABLE staff_users (
    id uuid PRIMARY KEY,
    clinic_id uuid NOT NULL REFERENCES clinics(id),
    email text NOT NULL CHECK (email = lower(email) AND length(email) BETWEEN 3 AND 254),
    role text NOT NULL CHECK (role IN ('owner', 'admin', 'viewer')),
    password_hash text NOT NULL,
    failed_logins integer NOT NULL DEFAULT 0 CHECK (failed_logins >= 0),
    locked_until timestamptz,
    disabled_at timestamptz,
    password_changed_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (email),
    UNIQUE (clinic_id, id)
);
CREATE TABLE staff_sessions (
    id uuid PRIMARY KEY,
    clinic_id uuid NOT NULL,
    user_id uuid NOT NULL,
    secret_hash bytea NOT NULL CHECK (length(secret_hash) = 32),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    FOREIGN KEY (clinic_id, user_id) REFERENCES staff_users(clinic_id, id)
);
CREATE INDEX staff_sessions_user ON staff_sessions (user_id) WHERE revoked_at IS NULL;
-- Minimal audit trail: no emails, passwords, tokens or network addresses.
CREATE TABLE audit_events (
    id uuid PRIMARY KEY,
    clinic_id uuid NOT NULL REFERENCES clinics(id),
    actor_id uuid,
    action text NOT NULL,
    target_id uuid,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_events_clinic ON audit_events (clinic_id, created_at);
