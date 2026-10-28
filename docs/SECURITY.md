# Security and privacy design

## Current controls

Secret configuration is required at startup and redacted in settings representation. Tool bearer tokens are per-clinic credential records (`tenant_credentials`): 256-bit random secrets, stored only as SHA-256 digests, shown once at issue, with expiry, overlap-window rotation, immediate revocation and coarse last-used tracking. Credential administration is CLI-only (operator access to the database); there is no HTTP endpoint for it yet. Staff users belong to one clinic with an `owner`/`admin`/`viewer` role; passwords are scrypt hashes (parameters stored per hash, rehashed on login when raised), sessions are random bearer tokens stored as SHA-256 digests with a 12-hour absolute lifetime, and accounts lock for 15 minutes after five consecutive failures. Disabling a user or changing a password revokes sessions. `audit_events` records staff logins, failures, logouts, password changes and user changes with ids only (no emails, passwords, tokens or IP addresses). Reads scope by tenant and schema foreign keys scope doctor/service writes. Validation responses omit submitted values. Application code does not log request bodies or patient data. The local database binds only to loopback. Source documents, credentials, recordings, and transcripts are ignored by Git.

## Release blockers

This public repository is a development scaffold. Before a pilot implement per-IP/global login rate limiting (account lockout alone lets an attacker who knows an email lock staff out), MFA or an external IdP, a decision on browser session transport (cookies with CSRF protection versus bearer), credential administration audit trail, TLS and request/rate/concurrency limits, trusted proxy configuration, audit trails, verified call identity and anti-replay controls, encrypted OAuth credentials, retention/deletion policy, backups/restore drills, monitoring, and access reviews. Plain database storage alone is not appropriate for health or credential data. Use a secrets manager and managed encryption; do not persist raw OAuth tokens in clinic rows.

Bolna custom headers can authenticate tool/webhook traffic once verified against the provider contract. An IP allowlist is defense in depth, never the sole credential. Do not trust arbitrary X-Forwarded-For. Confirm vendor address ranges at deployment; never copy the DOCX's malformed `13.202.133.534`. Do not send production payloads to public webhook debugging services.

Treat speech/transcripts/tool output as untrusted content. Permit only narrowly typed tools. Model claims cannot bypass identity, schedules, state transitions, or authorization. A caller's phone number is not proof of identity. Require verified ownership plus an explicit appointment reference for sensitive operations, with a human fallback for shared phones.

## Information minimization

The foundation accepts no patient name, phone, age, symptoms, transcript, or recording. Before adding them document purpose, field-level access, encryption, consent and retention. Calendar entries should use an opaque appointment reference and minimal operational data. Keep credentials/PII out of URLs, logs, trace attributes, exception reports, metrics labels, provider error bodies, and agent config fixtures.

## Clinical and contact policy

The receptionist handles administration only. No diagnosis, medication advice, or medical triage. Have the clinic approve emergency/human-transfer wording and test failed-transfer fallback. Obtain appropriate recording and reminder consent, honor opt-out and quiet hours, and confirm applicable local obligations with qualified reviewers before launch. No legal-compliance certification is asserted by this scaffold.
