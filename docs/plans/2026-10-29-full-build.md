# Full build plan (29 October 2026)

Goal: implement the remaining product described by the owner's design brief (see `docs/SOURCE_REVIEW.md` for accepted corrections) as tested repository code. Every external provider sits behind a port with a deterministic fake. Live adapters are built and contract-tested against synthetic HTTP fixtures, never real networks. Nothing in this plan authorizes real calls, number purchases, patient messages, OAuth against real accounts or deployment.

Branch: `claude/tenant-credentials`. Tasks run sequentially; each ends in one commit.

## Shared conventions (every task)

- Read `AGENTS.md`, then the source you touch. Match existing idiom: plain service functions, psycopg `dict_row` connections via `clinic_voice.db.connect(url)`, `HTTPException` raised from services, strict Pydantic boundary models in `schemas.py` (`StrictModel`, `extra="forbid"`), FastAPI routes in `api.py` (split into `routes_*.py` modules only where a task says so).
- Tenant comes only from an authenticated credential or staff session (`tenant`, `staff_session`, `require_role` in `api.py`). Never from a body, path or provider payload. Every query filters by `clinic_id`; use composite `(clinic_id, id)` foreign keys for new tenant tables.
- Schema changes: add the next numbered file in `migrations/`; never edit an applied migration. Update `REQUIRED_MIGRATION` in `api.py` to the newest file. Extend the `TRUNCATE` list in `tests/conftest.py` for new tables.
- Tests: PostgreSQL behavior tests use the `database` fixture (`tests/conftest.py`), run with `TEST_DATABASE_URL=postgresql://clinic:clinic_local_only@localhost:55432/clinic_voice_test`. Use the `cheap_hashing` pattern from `tests/test_staff.py` when creating staff. Synthetic data only (`example.test` emails, `+9100000xxxxx`-style fake numbers). Cover tenant leaks, races, retry ambiguity and state transitions where relevant.
- No secrets, tokens, phone numbers, names or provider response bodies in logs, errors, audit rows, calendar titles or fixtures. Staff mutations write `audit_events` (ids and action names only).
- Done means: `ruff check .`, `ruff format --check .`, full `pytest -q` with `TEST_DATABASE_URL` all pass; `clinic-voice export-openapi > docs/openapi.json` regenerated if routes changed; the relevant docs (`API.md`, `ARCHITECTURE.md`, `SECURITY.md`, `OPERATIONS.md`, `ROADMAP.md`, ADR if a decision was made) updated to describe implemented vs planned behavior honestly.
- Commit with explicit paths (`git add <paths>`), never `git add -A`/`.`; the working tree has an unrelated deleted `CONTRIBUTING.md` that must not be staged. Do not push.
- Never call real external services. HTTP adapters are tested with `httpx.MockTransport`.

## T1 Clinic configuration admin API

Migration 004: `clinics` gains `lead_time_minutes` (default 60, 0–10080), `horizon_days` (default 90, 1–365); `doctors.active` and `services.active` booleans (default true); `services.buffer_after_minutes` (default 0, 0–120).

Routes (new module `src/clinic_voice/routes_admin.py`, included by `api.py`), clinic from the staff session; GET allowed for all roles, mutations require `owner` or `admin`:

- `GET/PATCH /v1/admin/clinic`: name, timezone (validated as an IANA zone with `zoneinfo`; 422 otherwise), lead_time_minutes, horizon_days.
- `GET/POST /v1/admin/doctors`, `PATCH /v1/admin/doctors/{id}`: name, active.
- `GET/POST /v1/admin/services`, `PATCH /v1/admin/services/{id}`: doctor_id (create only, must belong to the clinic → 404), name, duration_minutes 5–240, buffer_after_minutes, active.
- `GET/PUT /v1/admin/doctors/{id}/hours`: replace the doctor's weekly hours atomically with `[{weekday 0–6, opens, closes}]`; reject opens>=closes and overlapping windows on the same weekday (422).

Slot computation uses `horizon_days` instead of the fixed 90 and excludes inactive doctors/services (404 as today). Lead time and buffers are applied in T2. Tests: role matrix, cross-tenant 404s for every id-taking route, hours validation, timezone validation, inactive service unbookable, audit rows.

## T2 Schedule exceptions, lead time and buffers

Migration 005: `schedule_exceptions` (id, clinic_id, doctor_id NULL = whole clinic, kind `holiday|leave|break`, starts_at, ends_at, label ≤ 80 chars, created_by, created_at; starts_at < ends_at). `appointments.blocks_until timestamptz` backfilled to `ends_at`, NOT NULL, CHECK `blocks_until >= ends_at`; replace exclusion constraint `no_overlapping_appointments` with one over `tstzrange(starts_at, blocks_until, '[)')` (same status filter).

- `candidate_slots` takes buffer minutes, lead-time cutoff and busy intervals (appointment blocks + exceptions). A candidate occupies `[start, start+duration+buffer)`; `start+duration` must fit inside the working window; start must be ≥ now+lead time; date within horizon.
- Reservation stores `blocks_until = ends_at + buffer` and re-checks through `slots()` as today.
- Admin routes: `GET/POST /v1/admin/exceptions`, `DELETE /v1/admin/exceptions/{id}`. Creating an exception takes the doctor row lock (`FOR UPDATE`; for clinic-wide, all clinic doctors ordered by id) so it serializes with reservations, and returns `conflicting_appointment_ids` for active appointments it overlaps (no automatic cancellation).
- Tests: buffer between bookings, adjacency after buffer, lead time, horizon, clinic holiday, doctor leave, concurrent exception vs reservation (one ordering wins consistently), DB exclusion honours blocks_until.

## T3 Calendar port, fake adapter and external busy

Package `src/clinic_voice/providers/` with `calendar.py`:

- `CalendarPort` Protocol: `get_event(calendar_id, event_id) -> CalendarEvent | None`, `insert_event(calendar_id, CalendarEvent) -> CalendarEvent`, `cancel_event(calendar_id, event_id) -> None` (idempotent), `free_busy(calendar_ids, start, end) -> list[(start, end)]`.
- Errors: `ProviderTimeout` (outcome unknown), `ProviderRetryable`, `ProviderPermanent`, `EventAlreadyExists`.
- `FakeCalendar`: in-memory, deterministic, with fault injection (`timeout_after_write`, `timeout_before_write`, `fail_next(n, error)`), counts inserts per event id.
- `event_id_for(appointment_id, version)`: `cv` + appointment UUID hex + `v` + version (Google event ids allow base32hex `a-v0-9`). Event summary is opaque (`Clinic appointment <booking reference or short id>`); no patient data.

Migration 006: `doctors.booking_calendar_id text`, `doctors.busy_calendar_ids text[] DEFAULT '{}'`; `external_busy` (clinic_id, doctor_id, starts_at, ends_at, source_calendar text, fetched_at). Admin PATCH doctor accepts the calendar fields. `slots()` treats cached `external_busy` as busy (advisory). A `refresh_external_busy(conn, port, clinic_id, doctor_id, start, end)` service replaces the doctor's cached window. ADR 0004 records calendar ownership: the DB is the reservation ledger, a dedicated booking calendar per doctor is preferred, external busy calendars are advisory and re-checked before insert, conflicts go to `needs_review`, FreeBusy is never a lock.

## T4 Durable worker and verified confirmation

Migration 007: `appointments.version int DEFAULT 1`, `appointments.calendar_event_id text`; `outbox` gains `appointment_version int NOT NULL DEFAULT 1`, `lease_owner text`, `lease_expires_at`, `last_error text` (short code only), `payload jsonb DEFAULT '{}'`, `updated_at`; replace `UNIQUE (appointment_id, kind)` with `UNIQUE (appointment_id, kind, appointment_version)`; extend `kind` CHECK for later kinds (`calendar.create`, `calendar.cancel`, `notification.send`, `reminder.schedule`, `reminder.stop`, `reminder.call`, `webhook.process`, `provision.step`) — make `appointment_id` nullable for non-appointment jobs with a CHECK that calendar kinds have one.

`src/clinic_voice/worker.py`:

- `claim(conn, worker_id, limit, lease)`: `FOR UPDATE SKIP LOCKED` over due pending jobs and processing jobs whose lease expired; sets processing, increments attempts, sets lease; commits before any provider I/O.
- `complete(conn, job)` / `retry(conn, job, error_code)` / `dead(conn, job, error_code)`: fenced on `id` + `lease_owner` + attempts so a stale worker cannot overwrite a newer claim. Backoff `min(30s * 2^(attempts-1), 1h)` (injectable clock/jitter-free for tests); after `MAX_ATTEMPTS` (8) → `dead`, appointment → `needs_review`.
- Handler registry by kind; handlers receive ports. `calendar.create`: skip (done) if appointment not `pending_sync` or version changed; `get_event` first (recovery); if absent, re-check `free_busy` on the doctor's busy calendars and fail to `needs_review` on conflict; `insert_event`; on `ProviderTimeout`/`ProviderRetryable` → retry (next attempt starts with `get_event`); on `EventAlreadyExists` → `get_event` and verify times. Mark `confirmed` with `calendar_event_id` only after the event is read back or returned by insert with matching times, in a short transaction that re-checks status and version. `calendar.cancel` cancels idempotently.
- CLI `clinic-voice worker [--once] [--calendar fake]` loop with graceful stop. Live calendar selection arrives in T5.
- Tool route `POST /v1/tools/appointment-status` (tenant credential) returning status and cautious spoken text; `confirmed` wording only for persisted `confirmed`.
- Tests: two concurrent workers claim disjoint jobs; crash after provider success (fake writes, worker "dies" before completing, lease expires, second worker confirms with exactly one event); timeout-before-write retried once without duplication; conflict → needs_review; dead-letter after max attempts; stale worker cannot complete a reclaimed job; status endpoint never says confirmed for pending.

## T5 Google OAuth and live calendar adapter

Add runtime dependencies `httpx` and `cryptography` to `pyproject.toml` and regenerate both lock files with the `uv pip compile` commands documented in git history/CONTRIBUTING notes (`uv pip compile pyproject.toml --universal -o requirements.lock`; `uv pip compile pyproject.toml --extra dev --universal -o requirements-dev.lock`).

- `src/clinic_voice/crypto.py`: AES-GCM envelope with a keyring `TOKEN_ENCRYPTION_KEYS="kid:base64key,..."` (first is active; decrypt by stored key id); `PHONE_HASH_KEY` HMAC helper for later tasks. Settings fields optional; features fail closed (503 "not configured") when absent.
- Migration 008: `calendar_accounts` (id, clinic_id, provider `google`, encrypted refresh token + key id, granted scopes, status, connected_by, created_at, revoked_at), `oauth_states` (state digest, clinic_id, user_id, encrypted PKCE verifier, redirect, expires_at, used_at). Doctors reference a calendar account (`calendar_account_id`, composite FK).
- Routes: `POST /v1/admin/calendar/google/start` (owner/admin) → authorization URL with `state`, S256 PKCE, `access_type=offline`, minimum scopes (verify against official Google docs; record the links checked); `GET /v1/oauth/google/callback` validates single-use unexpired state, exchanges the code, stores the encrypted refresh token, never logs/returns tokens; `DELETE /v1/admin/calendar/google/{id}` revokes locally.
- `providers/google_calendar.py`: `GoogleCalendar(CalendarPort)` over httpx with access-token refresh, explicit timeouts, error mapping (timeout → `ProviderTimeout`, 409 → `EventAlreadyExists`, 429/5xx → `ProviderRetryable`, other 4xx → `ProviderPermanent`), no response bodies in exceptions.
- Worker `--calendar google` builds per-clinic adapters from stored accounts.
- Tests with `httpx.MockTransport` and synthetic fixtures only: state replay/expiry/cross-tenant, PKCE parameters, token encryption round-trip and key rotation, adapter mapping incl. timeout and already-exists recovery. OPERATIONS documents a local credential setup; live validation is a separate owner-authorized step.

## T6 Bolna trusted gateway and agent configuration compiler

Verify current Bolna docs (agent v2 create, api_tools, call context variables, custom headers, multilingual config, webhooks/executions) with WebFetch; record checked URLs and anything unverified in `docs/SOURCE_REVIEW.md`. Build only what can be represented honestly; mark payload shapes "unverified against live API" where docs are ambiguous.

- Migration 009: `call_sessions` (id, clinic_id, provider `bolna`, execution_id unique per clinic, agent_id, caller_number_ciphertext + key id, caller_number_hash (HMAC), started_at, ended_at, status, verification_failures int).
- Gateway routes `/v1/bolna/tools/*` authenticated with the tenant tool credential (sent by Bolna as a custom header), plus the execution id and caller number supplied by Bolna call-context templating. Caller number is provider-asserted, stored encrypted, never treated as identity proof. Server derives the idempotency key `bolna:<execution_id>:<tool>:<sha256(canonical args)[:24]>` so retries replay and the LLM never invents keys. Tools: check-slots, reserve-slot, appointment-status (delegate to existing services).
- `POST /v1/bolna/context` pre-call variables: clinic name, timezone, today's date in clinic zone, active doctors/services, supported languages, approved FAQ/emergency text placeholders — never patient or booking data.
- `src/clinic_voice/bolna/agent_config.py`: compile an agent payload from clinic config, `agents/prompts/*.md` and gateway tool definitions (keyed multilingual config per SOURCE_REVIEW, Sarvam transcriber/synthesizer settings configurable). Credentials are injected only in memory at provisioning time; compiled fixtures in tests use placeholders. Contract tests compare against sanitized fixture files in `tests/fixtures/bolna/`.
- `providers/bolna.py`: `BolnaPort` Protocol (create/update agent, get execution, list/assign phone numbers, start outbound call, stop scheduled call) + `FakeBolna` + `BolnaClient` over httpx (MockTransport tests only). Purchase of numbers is not implemented.

## T7 Patients, consent and verified cancel/reschedule

- Migration 010: `patients` (id, clinic_id, name ciphertext, phone ciphertext, phone_hash HMAC, preferred_language, created_at, retention_until), `consents` (patient_id, purpose `booking_confirmation|reminder_call|reminder_message`, granted_at, revoked_at, source), `appointments.patient_id` (composite FK, nullable), `appointments.booking_reference` (8-char Crockford base32, unique per clinic), status values add `cancellation_pending`, `cancelled` already exists, `reschedule` modelled as new appointment + link `rescheduled_from`.
- Gateway reserve-slot accepts optional patient name and consent flags; phone comes from the call session, not the LLM. Response includes the booking reference.
- Verified lookup: `POST /v1/bolna/tools/verify-booking` requires booking reference + caller-number hash match on the session; 3 failures per session → `handoff_required`. Shared phones: reference disambiguates; never enumerate bookings by phone.
- Cancel/reschedule tools require a verified session for that booking plus an explicit `confirmed: true` read-back flag. Reschedule reserves the new slot (new appointment, same patient, new version chain) before cancelling the old one in one transaction; failure keeps the original. Both enqueue calendar jobs (`calendar.cancel` for the old event; `calendar.create` for the new) and bump versions so stale reminder jobs no-op.
- Staff routes: `GET /v1/admin/appointments` (date range/status filters, minimal fields, decrypted name only for owner/admin), `POST /v1/admin/appointments/{id}/cancel`, `POST /v1/admin/appointments/{id}/resolve` (needs_review → requeue or cancel).
- Tests: shared-phone enumeration impossible, wrong reference counting, cancel/reschedule races (one consistent state), failed reschedule preserves original, retention/consent stored, no PHI in audit/log/calendar summary.

## T8 Webhook inbox and execution reconciliation

- Migration 011: `webhook_inbox` (id, clinic_id resolved server-side from agent id, provider event key = provider event id or execution+status+payload hash, minimized payload jsonb, received_at, processed_at, status). Unique per (clinic, event key).
- `POST /v1/webhooks/bolna`: constant-time custom secret header check per clinic (stored as digest), body size limit, optional configurable IP allowlist as defense in depth (no X-Forwarded-For trust unless configured), persist minimized record and enqueue `webhook.process` before returning 2xx.
- Processor updates `call_sessions` with monotonic terminal states (no regression after completed/failed), records disposition fields allowed by an explicit allowlist, no transcripts/recordings stored.
- Reconciliation job polls `BolnaPort.get_execution` for sessions without a terminal webhook after a configurable delay.
- Tests: duplicate delivery harmless, multiple statuses for one execution all kept in order, regression ignored, bad secret 401 without persistence, oversize 413, reconciliation fills a missed final status.

## T9 Confirmations and reminders

- Migration 012: `contact_preferences`/opt-out per patient and purpose, `reminders` (appointment_id, appointment_version, channel `call|message`, scheduled_for, provider_execution_id, status). Clinic settings: quiet hours (start/end local time), reminder lead (default 24h), daily contact budget.
- `MessagingPort` (+ fake) for confirmation messages; Bolna outbound calls through `BolnaPort.start_outbound_call` for reminder calls.
- On verified `confirmed`, enqueue `notification.send` if consent exists. Reminder scheduler enqueues `reminder.call` respecting consent, opt-out, quiet hours (shift to next allowed time), budget. Handlers re-check appointment status/version before contacting; cancellation/reschedule enqueues `reminder.stop` which stops a provider-scheduled call by execution id. Opt-out tool `/v1/bolna/tools/opt-out` and webhook disposition both persist opt-out.
- Ambiguous provider timeout on a send: record attempt with stable provider idempotency/reference and reconcile before retrying; never re-contact blindly.
- Tests: no consent → no contact, opt-out suppresses pending reminders, quiet hours shift, stale version no-ops, cancellation stops scheduled call, timeout recovery sends once.

## T10 Clinic provisioning state machine

- Migration 013: `provisioning` (clinic_id, state `draft|calendar_connected|agent_created|number_assigned|verified|active|failed|manual_review`, agent_id, phone_number_id, step attempts, last_error code, updated_at) with an append-only `provisioning_events` log.
- Service advances one step at a time through `provision.step` jobs using `BolnaPort`: create/update agent from the T6 compiler (idempotent: look up by stored id/name before creating), assign an existing verified number chosen by the owner (no purchase path), verify by fetching back. Each step persists provider ids before moving on; uncertain results reconcile by lookup.
- Routes: `GET /v1/admin/provisioning`, `POST /v1/admin/provisioning/advance` (owner), `POST /v1/admin/provisioning/activate` (owner, requires verified + calendar connected + emergency/handoff text configured).
- Tests: retry after timeout during agent creation creates one agent; failure → failed/manual_review; cannot activate without prerequisites; tenant isolation.

## T11 Staff dashboard

`dashboard/`: TypeScript + Vite, no UI framework, typed API client from the backend contract. Screens: login, clinic settings, doctors/services/hours, exceptions, appointments with sync state and needs_review queue (resolve/cancel), staff users, calendar connection, provisioning status. Session token kept in memory (sessionStorage only if documented), CORS allowed only for configured dashboard origins (`DASHBOARD_ORIGINS`). `npm run build` and `npm run typecheck` must pass; a small vitest suite for the API client and state labels (never show pending as confirmed).

## T12 Final review and documentation

Whole-branch review against this plan and AGENTS.md invariants; fix findings; update README, ARCHITECTURE diagram (implemented vs planned), API, SECURITY, OPERATIONS, ROADMAP, VALIDATION, CLAUDE_HANDOFF with the owner actions still required for live integration (Bolna account/test number, Google OAuth client, encryption keys, messaging provider, deployment).
