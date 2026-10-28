# ADR 0002 Tenant credential records for tool authentication

Status: accepted, 29 October 2026.

The initialization authenticated tools with one `TOOL_TOKEN` environment variable mapped to one `CLINIC_ID`. That cannot serve more than one clinic, cannot rotate without downtime, and cannot revoke a leaked token without redeploying.

Store tool credentials in `tenant_credentials`, one row per credential, owned by exactly one clinic. Tokens have the form `cvt_<credential id hex>.<43-char urlsafe secret>`. The id is a lookup handle; the 256-bit secret is stored only as a SHA-256 digest and compared in constant time. Because the secret is random rather than user-chosen, a fast digest is sufficient; no password-stretching function is used. The plaintext token is printed once by the CLI.

Rotation issues a successor (`rotated_from`) and caps the old credential's expiry at now plus a grace window, so callers can switch without an outage; rotation can only shorten an existing expiry. Revocation is immediate. `last_used_at` is updated at most every five minutes to show when a rotated credential is no longer in use without adding a write to every request. Malformed, unknown, wrong-secret, expired and revoked tokens produce one indistinguishable 401; malformed tokens are rejected before a database connection.

Credential administration is CLI-only for now; the database operator is the trust root. Staff users, roles, an HTTP administration API and an audit trail of credential changes come next and should reuse the `scope` column (currently only `tools`) rather than mixing staff sessions into tool credentials. A server-side pepper (HMAC key from a secret manager) is a possible future hardening; it would need a forward migration and re-issue of credentials.
