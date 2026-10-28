-- Tool credentials replace the single bootstrap TOOL_TOKEN. Only a SHA-256 digest of the
-- high-entropy secret is stored; the plaintext token is shown once at issue time.
CREATE TABLE tenant_credentials (
    id uuid PRIMARY KEY,
    clinic_id uuid NOT NULL REFERENCES clinics(id),
    scope text NOT NULL CHECK (scope IN ('tools')),
    label text NOT NULL CHECK (length(label) BETWEEN 1 AND 80),
    secret_hash bytea NOT NULL CHECK (length(secret_hash) = 32),
    rotated_from uuid REFERENCES tenant_credentials(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz,
    revoked_at timestamptz,
    last_used_at timestamptz
);
CREATE INDEX tenant_credentials_clinic ON tenant_credentials (clinic_id);
