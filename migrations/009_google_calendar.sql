-- Google Calendar connection (T5). A clinic connects a Google account through OAuth; the
-- refresh token is stored only as AES-256-GCM ciphertext (application keyring, key_id names
-- the key) bound by associated data to (clinic_id, id). Revocation clears the ciphertext.
CREATE TABLE calendar_accounts (
    id uuid PRIMARY KEY,
    clinic_id uuid NOT NULL REFERENCES clinics(id),
    provider text NOT NULL CHECK (provider = 'google'),
    refresh_token_ciphertext bytea CHECK (refresh_token_ciphertext IS NULL OR length(refresh_token_ciphertext) BETWEEN 29 AND 8192),
    key_id text CHECK (key_id IS NULL OR char_length(key_id) BETWEEN 1 AND 32),
    scopes text[] NOT NULL DEFAULT '{}',
    status text NOT NULL CHECK (status IN ('active', 'revoked', 'error')),
    connected_by uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    UNIQUE (clinic_id, id),
    FOREIGN KEY (clinic_id, connected_by) REFERENCES staff_users(clinic_id, id),
    CHECK ((status = 'revoked') = (revoked_at IS NOT NULL)),
    CHECK (status = 'revoked' OR (refresh_token_ciphertext IS NOT NULL AND key_id IS NOT NULL))
);
CREATE INDEX calendar_accounts_clinic ON calendar_accounts (clinic_id, status);

-- Pending OAuth authorizations. Only the SHA-256 digest of the random state is stored; the
-- PKCE verifier is encrypted (associated data binds it to the digest, clinic and user).
-- A state is single-use (used_at) and expires after ten minutes.
CREATE TABLE oauth_states (
    state_digest bytea PRIMARY KEY CHECK (length(state_digest) = 32),
    clinic_id uuid NOT NULL,
    user_id uuid NOT NULL,
    code_verifier_ciphertext bytea NOT NULL,
    key_id text NOT NULL CHECK (char_length(key_id) BETWEEN 1 AND 32),
    expires_at timestamptz NOT NULL,
    used_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (clinic_id, user_id) REFERENCES staff_users(clinic_id, id)
);
CREATE INDEX oauth_states_expiry ON oauth_states (clinic_id, expires_at);

-- The account whose credentials write and read this doctor's calendars (NULL: the clinic's
-- only active account, if exactly one exists).
ALTER TABLE doctors
    ADD COLUMN calendar_account_id uuid,
    ADD CONSTRAINT doctors_calendar_account_fkey
        FOREIGN KEY (clinic_id, calendar_account_id) REFERENCES calendar_accounts(clinic_id, id);
