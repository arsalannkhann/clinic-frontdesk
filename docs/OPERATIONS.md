# Development and operations

## Local lifecycle

Follow README setup. `docker compose stop` preserves data; `docker compose down` also preserves the named volume. `docker compose down -v` deletes local data and must only be used intentionally. Migrations run as a separate CLI step before API start, from the repository root. The application never auto-creates tables. The schema lock serializes migration processes, and checksums reject edits to applied files.

The image runs as UID 10001. Build with `docker build -t clinic-voice:dev .`; pass DATABASE_URL through your environment/secret manager. Issue tool credentials with the CLI after migrating; the plaintext token is printed once and must go straight to the gateway's secret store. Inside a Compose network the DB hostname is `db:5432`, not localhost:55432. The Dockerfile does not run migrations on startup. No deployment automation is configured.

Readiness checks schema availability and reports integrations `not_configured`. It does not certify calendar sync or a working phone line. Keep liveness separate so a provider outage does not trigger a restart loop. Outbox entries are consumed only while a worker runs (see below).

## Running the worker

`clinic-voice worker --calendar fake` runs the outbox worker in a loop: it claims up to 10 due jobs, processes them, and polls again after `--interval` seconds (default 2, 0.1-300) when idle. `--once` processes one batch and exits; `--worker-id` names the lease owner (default `host:pid`, at most 128 characters; use a distinct id per process). SIGINT/SIGTERM stop the loop after the current batch. A database error in a batch is logged by type and the loop waits one interval. `--calendar fake` uses an in-process FakeCalendar and prints a warning: nothing reaches a real calendar, and events vanish when the process exits, so use it only for local development. Without `--calendar` the worker refuses to start; the Google adapter arrives in T5. Run as many workers as needed; `SKIP LOCKED` keeps claims disjoint and fenced completion makes a reclaimed job safe. A worker killed mid-job leaves its job `processing` until the 2-minute lease expires, then another worker reclaims it and recovers by reading the deterministic event id.

## Dead-letter review

Jobs dead-letter after `max_attempts` (8) failures or on an outcome retrying cannot fix: `external_conflict` (the doctor's busy calendar overlaps the hold), `calendar_unmapped` (doctor has no booking calendar), `event_mismatch` (an event with the deterministic id exists with other times or is cancelled), `provider_permanent`, `unknown_kind` (a kind with no handler yet). A dead calendar job moves a still-pending appointment to `needs_review`; the caller is told staff will follow up. Owners and admins list them with `GET /v1/admin/outbox?status=dead`. Resolve the cause first (map the calendar, clear the conflicting external event, or contact the patient), then `POST /v1/admin/outbox/{id}/requeue`; the worker re-checks everything before writing. Do not requeue `external_conflict` without resolving the conflict, it will dead-letter again. Monitor `pending` age and `processing` jobs whose lease keeps expiring.

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

Calendar uncertain (implemented in the worker): keep pending/review state, reconcile using stable event ID; never blindly reinsert or announce success. Messaging failure: preserve appointment, retry delivery independently and alert after bounded attempts. Webhook gap: poll executions and reconcile. Reminder race: version-check before dialing and reconcile provider cancellation. Database failure: fail closed with a retryable safe response.

Proposed metrics: booking outcome counts, tool latency, pending reservation age, outbox age/retries/dead letters, calendar conflicts, webhook gaps, transfer failures, language task success and provider spend. Use opaque correlation IDs; no patient identifiers as metric labels.

Rollback app versions only if compatible with schema. No automatic down migrations are provided. Restore from backups only under a reviewed recovery plan; keep the reservation ledger and external calendar reconciled.
