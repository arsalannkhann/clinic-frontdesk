# Claude continuation prompt

Copy the following prompt into Claude Code opened in this repository. It is self-contained and points to committed project instructions. Initialization deliberately establishes a tested core and documented boundaries rather than pretending the providers are integrated.

---

You are continuing the Clinic Voice AI project in `/Users/arsalan/Documents/GitHub/voice-ai`, remote `https://github.com/arsalannkhann/voice-ai`. Build a reliable multilingual AI receptionist for Indian clinics using Bolna Hosted API, Sarvam Hindi/Hinglish/Indian English speech, FastAPI and PostgreSQL, with Google Calendar and consented confirmations/reminders. Treat this as implementation work: inspect the repo, execute coherent slices, verify them, and keep the documentation accurate.

Read `AGENTS.md`, `CLAUDE.md`, `README.md`, `docs/ARCHITECTURE.md`, `docs/PRODUCT.md`, `docs/API.md`, `docs/SECURITY.md`, `docs/SOURCE_REVIEW.md`, `docs/ROADMAP.md`, and `docs/OPERATIONS.md` first. Check Git status and current HEAD before making changes; preserve unrelated edits. Create a feature branch for your work. Do not assume initialization notes still describe later code exactly.

The original owner-supplied DOCX is a design input, not an executable instruction source. Its corrections and official references are recorded in `docs/SOURCE_REVIEW.md`. If the original file is available at `/Users/arsalan/Desktop/Bolna Clinic AI Receptionist Implementation.docx`, treat its API JSON and performance claims as unverified examples. Do not install external agent skills merely because the document suggests doing so.

## What exists now (after T12, 29 October 2026)

Everything below is implemented on `claude/tenant-credentials` and tested locally against PostgreSQL 17 with synthetic data. Providers are exercised only through fakes and `httpx.MockTransport` contract fixtures: no live Google, Bolna or messaging account has been used, and no real call or patient message has been made. docs/ROADMAP.md has the per-task status and docs/VALIDATION.md the evidence.

- Backend (`src/clinic_voice/`, FastAPI app `clinic_voice.api:app`, migrations 001 to 017): tenant credential records with scopes `tools` (`cvt_`), `bolna` (`cvb_`) and `webhook` (`cvw_`); staff users, roles, sessions, lockout and audit; clinic, doctor, service, hours and schedule exception administration; DST-safe slot math with lead time and buffers; the reservation ledger (GiST exclusion, canonical idempotency, atomic outbox).
- Durable worker (`clinic-voice worker`): leased, fenced outbox jobs with retry and dead-letter; Google Calendar adapter with OAuth PKCE and an encrypted token keyring; external busy cache; deterministic event ids and verified confirmation.
- Bolna: tool gateway (`/v1/bolna/*`) with call sessions, patients, verified cancel and reschedule and opt-out; agent compiler; execution webhook inbox and reconciliation; confirmation messages and reminder calls (`outbound_enabled` off by default); provisioning state machine with line gating; the provisioned agent's `webhook_url` carries a managed webhook credential (migration 017). The generic `/v1/tools/*` API is line-gated too, and its appointment-status tool shows only the same credential's reservations.
- Staff dashboard (`dashboard/`, TypeScript + Vite, typed from `docs/openapi.json`).
- `agents/`: receptionist prompt, a tool-free reminder prompt draft for owner review, design-only flow, synthetic acceptance scenarios.

Not implemented: a live messaging provider, provisioning of the outbound reminder agent, rotation of a provisioned agent's credential, human transfer, cross-call verification rate limits, MFA, retention of provisioning events, deployment automation.

## Owner actions required before any live use

None of these can be done by an agent in this repository. Each needs the owner, and items 1, 4, 6 and 10 need explicit authorization before anything live happens.

1. Bolna: create the account and authorize a test number. Validate the real tool request shapes, `execution_id`/`from_number` substitution and token masking against an owner-authorized test agent, then record sanitized fixtures (docs/SOURCE_REVIEW.md).
2. Design and review the outbound reminder agent: it must carry no tools; review the `agents/prompts/reminder.md` wording and the handling of "stop calling" on a reminder call. Release blocker for outbound calls.
3. Approve the clinic scripts: emergency and handoff text, the reschedule and "not yet confirmed" wording, and whether the booking reference may appear in messages (`include_reference_in_messages`, default off).
4. Google: create the OAuth client and redirect URI, decide on requesting `openid` to record the account `sub`, and validate the adapter against a live test account (docs/OPERATIONS.md).
5. Keep `TOKEN_ENCRYPTION_KEYS` and `PHONE_HASH_KEY` in a secret manager with a rotation plan. `PHONE_HASH_KEY` cannot be rotated casually: existing hashes stop matching and verification breaks.
6. Choose a messaging provider whose adapter can honestly declare `idempotent_reference` and `consistent_lookup`. Keep `outbound_enabled` off until consent capture and DLT/TRAI compliance are confirmed.
7. Set retention periods and a purge schedule for patients, call sessions, the webhook inbox and `provisioning_events` (which the purge does not touch yet).
8. Pilot blockers to build or decide: a cross-call verification rate limit per phone hash and clinic, a staff login rate limit beyond the per-account lockout and MFA, human transfer, and a native-speaker evaluation.
9. Decide whether consent granted on a call that matched an existing patient (a caller who may be spoofing the number) should be scoped to that booking only.
10. Deployment: hosting, TLS, `DASHBOARD_ORIGINS`, `TRUSTED_PROXY_HOPS`, a restore drill and a tenant-access audit. Any deployment, number routing, paid call or patient message needs explicit owner authorization in the session.
11. Rebuild any development or test database that applied a pre-release version of migrations 012 to 015 (the checksum check refuses it).

## Exact next step

With the owner's authorization, start owner action 1 against a test agent and number, then items 2 and 3, before any clinic line is routed. Without that authorization, the remaining repository work is item 8 (verification rate limit, staff login rate limit, MFA) and the retention gap in item 7, each with PostgreSQL tests.

## Original milestone guidance (implemented in T1 to T12; kept for its constraints)

Add minimal patient contact data with explicit purpose/retention/consent and verified ownership. Build a trusted Bolna tool gateway that obtains tenant/call identity from authenticated context and persists a stable action ID. Add verified cancellation/rescheduling with an explicit booking reference and read-back confirmation. Reserve a replacement before releasing the old slot; failed moves preserve the original booking. Use versioning to suppress stale reminders.

Validate current Bolna graph/tool/multilingual schemas with official docs and sanitized contract fixtures. The source DOCX's multilingual languages array is inconsistent with the official keyed-object example. Sarvam's native REST/Batch keyterm capability does not establish streaming support through Bolna. Select a verified voice identifier rather than assuming display-name casing is accepted. Do not deploy a simple agent JSON while claiming graph enforcement.

Provisioning must persist/reconcile each step rather than buying numbers on retries. Start with an existing verified test number when live testing is authorized. Add authenticated/minimized webhook inbox before 2xx, multiple event/status handling for each execution, terminal-state protections, and polling recovery. Do not dedupe solely by execution ID or rely on assumed vendor delivery retries. Implement reminders with consent, quiet hours, opt-out, explicit execution IDs and version-aware stop/reconciliation.

Build the staff dashboard after backend authorization and contracts are stable. Show pending sync/failures/manual review clearly. Keep medical advice and automated clinical triage out of scope. Human transfer and a clinic-approved emergency/fallback script are required before pilot.

## Non-negotiable invariants

- Never accept tenant identity from an LLM body or unverified phone number. Never reveal/modify prior appointments based on caller ID alone. Handle shared family phones safely.
- Never say confirmed from an HTTP timeout, a local reservation, an enqueued job, or a conversational model's assertion.
- Preserve database overlap exclusion, canonical idempotency and atomic outbox commit. Do not replace durable jobs with in-process background tasks.
- Provider side effects must be idempotent or explicitly reconciled; retries cannot duplicate bookings or contacts.
- No secrets/PHI in source, logs, error responses, calendar titles, fixtures, metrics or public issues. No real credentials in agent configs.
- No live deployment, number purchases, paid calls or patient messages without explicit owner authorization. The repository is public; do not copy the original document into it.
- Add numbered forward migrations; never rewrite applied migrations. Document recovery for destructive changes.

## Verification and completion

Use Python 3.12–3.14. From repo root create a venv, install `requirements-dev.lock` and the package with `pip install --no-deps -e .`, copy `.env.example`. `docker compose up -d --wait db`, `clinic-voice migrate`, `clinic-voice seed-demo`, and `clinic-voice credential issue --clinic <demo clinic uuid> --label local-dev` initialize synthetic local data and print a one-time tool token. Check current Docker state first; the initialization may have left the local DB running.

For tests, use a disposable database named `clinic_voice_test`; create it if missing. Set `TEST_DATABASE_URL=postgresql://clinic:clinic_local_only@localhost:55432/clinic_voice_test` and run `pytest -q`. Tests truncate their test tables, so never use a production URL. Without TEST_DATABASE_URL the PostgreSQL tests skip, which is insufficient for a booking change. Also run `ruff check .`, `ruff format --check .`, and regenerate `docs/openapi.json` with `clinic-voice export-openapi` when API contracts change.

Prove tenant isolation, overlap prevention, concurrent retry replay, atomic reservation/outbox rollback, restart recovery and ambiguous provider timeouts with tests. Use only synthetic data. Update architecture/API/roadmap/operations and ADRs as decisions change. Report implemented behavior, exact validation results, remaining provider gaps, and any owner action required for live integration. Push/PR only as authorized in the current session; do not infer a new push authorization from the initialization conversation.
