# Development and operations

## Local lifecycle

Follow README setup. `docker compose stop` preserves data; `docker compose down` also preserves the named volume. `docker compose down -v` deletes local data and must only be used intentionally. Migrations run as a separate CLI step before API start, from the repository root. The application never auto-creates tables. The schema lock serializes migration processes, and checksums reject edits to applied files.

The image runs as UID 10001. Build with `docker build -t clinic-voice:dev .`; pass DATABASE_URL through your environment/secret manager. Issue tool credentials with the CLI after migrating; the plaintext token is printed once and must go straight to the gateway's secret store. Inside a Compose network the DB hostname is `db:5432`, not localhost:55432. The Dockerfile does not run migrations on startup. No deployment automation is configured.

Readiness checks schema availability and reports integrations `not_configured`. It does not certify calendar sync or a working phone line. Keep liveness separate so a provider outage does not trigger a restart loop. Pending outbox entries are expected in this foundation and are not consumed automatically.

## Credential rotation

1. `clinic-voice credential rotate <old-id> --grace-minutes 60` prints the replacement token; the old credential now expires within the grace window (rotation never extends an existing expiry).
2. Deploy the new token to the caller (future Bolna gateway).
3. Confirm `last_used_at` on the old credential stops advancing (`credential list --clinic <uuid>`; resolution is about five minutes), then `clinic-voice credential revoke <old-id>`.

For a suspected leak, revoke first; revocation is effective on the next request. Migration 002 drops nothing; `TOOL_TOKEN`/`CLINIC_ID` environment variables are now ignored and should be deleted from deployed environments.

## Staff bootstrap and recovery

Create a clinic's first owner with `clinic-voice staff create-owner --clinic <uuid> --email <address>`; the password is read from a hidden prompt or a single stdin line, never from arguments. The owner then creates other staff via `POST /v1/staff/users`. To recover a locked-out or forgotten account run `clinic-voice staff set-password --email <address>`: it clears the lockout, revokes every session for that user and writes a `staff.password_reset` audit event. Migration 003 only adds tables.

## Proposed pilot runbook

1. Record the commit/image digest and migration version; back up DB and verify restore procedures.
2. Run CI including real PostgreSQL races and tenant isolation; provision nonproduction credentials.
3. Configure a dedicated test calendar, verified test numbers, provider headers and inbound routing; validate provider schema with real sanitized responses.
4. Enable one synthetic clinic and make authorized test calls. Verify DB appointment, actual calendar event, notification receipt, outbox state, and spoken status agree.
5. Exercise duplicate tools, lost webhook, worker restart, provider timeout after success, and human calendar edits. Validate cancellation stops reminders and failed reschedule preserves original booking.
6. Enable limited pilot traffic only after owner review of the recorded evidence and unresolved risks. Set per-clinic spend/rate caps and a staffed fallback.

## Failure handling to implement

Calendar uncertain: keep pending/review state, reconcile using stable event ID; never blindly reinsert or announce success. Messaging failure: preserve appointment, retry delivery independently and alert after bounded attempts. Webhook gap: poll executions and reconcile. Reminder race: version-check before dialing and reconcile provider cancellation. Database failure: fail closed with a retryable safe response.

Proposed metrics: booking outcome counts, tool latency, pending reservation age, outbox age/retries/dead letters, calendar conflicts, webhook gaps, transfer failures, language task success and provider spend. Use opaque correlation IDs; no patient identifiers as metric labels.

Rollback app versions only if compatible with schema. No automatic down migrations are provided. Restore from backups only under a reviewed recovery plan; keep the reservation ledger and external calendar reconciled.
