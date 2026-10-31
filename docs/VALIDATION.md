# Validation

The first table records the initialization run. Later sections record each build task (T1 to T12, docs/plans/2026-10-29-full-build.md). Every result below is local: synthetic data, fakes or `httpx.MockTransport` fixtures, a disposable PostgreSQL 17 database. No live provider (Google, Bolna, messaging) was called and no real call or message was made.

## Initialization

Validated on 29 October 2026 with Python 3.14 and local PostgreSQL 17. These results cover the local foundation only; no live provider credentials or patient data were used.

| Check | Evidence |
| --- | --- |
| Ruff lint and format | `ruff check .` and `ruff format --check .` passed |
| Full application tests | 23 passed with TEST_DATABASE_URL set to a disposable PostgreSQL database |
| Reservation concurrency | Distinct actions for one slot yield one pending reservation and one conflict; concurrent same-key retries return the same booking |
| Database invariants | Direct overlapping insert across services rejected; composite FK prevents cross-tenant references |
| Atomic durability | Forced outbox constraint failure leaves zero committed reservations |
| API behavior | Slot lookup, reserve, retry, pending response and readiness exercised through HTTP TestClient |
| Security boundaries | Missing/wrong auth, caller-supplied tenant, naive timestamps and validation echo behavior tested |
| Migrations | Replay is harmless; changed applied SQL rejected; demo migration and synthetic seed succeeded |
| OpenAPI | Export matches committed schema |
| Docker | Final image built; running container returned ready against seeded DB and used UID 10001; smoke container removed |

The expanded concurrency run exposed a GiST exclusion-check deadlock. Reservation writes now take a short doctor-row lock after the action-key lock, and both full tests and focused concurrency tests passed after the fix. The range exclusion remains the final database safeguard.

Local `.env` contains a generated development credential, is mode 0600, and is ignored by Git. The local Compose database contains synthetic demo records only. An application readiness result reports `integrations: not_configured`; it does not imply a working phone line or external calendar.

Without TEST_DATABASE_URL, database tests skip; that is not sufficient validation for scheduling changes. GitHub Actions is configured to run the full PostgreSQL suite on Python 3.12 and 3.14. Inspect the actual workflow result for the pushed commit rather than treating the workflow definition as evidence of a pass.

## Tenant credential records (branch `claude/tenant-credentials`)

Validated on 29 October 2026, Python 3.14.6, local PostgreSQL 17: `ruff check .` and `ruff format --check .` passed; `pytest -q` with `TEST_DATABASE_URL` on `clinic_voice_test` gave 40 passed. New PostgreSQL tests cover digest-only storage, tenant A token cannot read tenant B doctor/service (and vice versa), wrong secret, unknown id, revoked, expired, rotation grace overlap, immediate rotation, repeat rotation never extending expiry, revoked credentials not rotatable, throttled last-used tracking and the CLI issue/list/revoke path. Malformed tokens are rejected without a database. A local smoke run against the dev database migrated 002, issued a credential, received 200 from check-slots, revoked it and received 401. The OpenAPI export did not change.

## Staff authentication (branch `claude/tenant-credentials`)

Validated on 29 October 2026, Python 3.14.6, local PostgreSQL 17: Ruff lint/format passed; `pytest -q` with `TEST_DATABASE_URL` gave 59 passed (without it, 17 pure tests pass and 42 PostgreSQL tests skip). Staff tests cover hashing and rehash, token kinds not crossing, indistinguishable login failures, lockout and lock expiry, logout and session expiry, the owner/admin/viewer matrix, cross-clinic list/update isolation, rejected `clinic_id` in bodies, password change revoking other sessions, immediate demotion/disable, last-owner protection, concurrent mutual demotion leaving one owner, minimized audit rows, and CLI bootstrap/recovery. A smoke run at production scrypt cost against the local dev database created an owner via CLI, logged in, read `/me`, created a viewer, listed staff, logged out and got 401 afterwards. OpenAPI regenerated with the staff endpoints.

## Build tasks T1 to T12 (branch `claude/tenant-credentials`)

Each task commit was checked with the same commands before it was committed: `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`, `TEST_DATABASE_URL=postgresql://clinic:clinic_local_only@localhost:55432/clinic_voice_test .venv/bin/pytest -q`, and from T11 on `cd dashboard && npm ci && npm run typecheck && npm run build && npm test`. Earlier per-task counts were reported in the commit reviews, not recorded here; the totals below are for HEAD after T11 and after T12.

| Task | Commits | What the PostgreSQL tests prove |
| --- | --- | --- |
| T1 clinic configuration | a6333cf, cd5652e | Owner/admin/viewer matrix, IANA zone validation, cross-clinic references refused, service edits serialized with reservations |
| T2 exceptions, lead time, buffers | 90074c1, f57d185 | Holidays/leave/breaks remove slots, DST-safe slot grid, buffers snapshotted at booking |
| T3 calendar port and busy cache | 0d70f6d, dc6e470, 0147231 | Fake adapter fault injection, per-doctor refresh serialization, real source calendar stored |
| T4 durable worker | 7ea54e3, 041571c, b002c6b | Leases, fencing, retry and dead-letter, crash after provider success recovers without a duplicate event, version-safe cancel |
| T5 Google OAuth and adapter | ea69ef3, 6600a3a, 85698d9, c760cb1 | PKCE state single use, encrypted keyring, MockTransport contract fixtures, account-bound events, grant revocation |
| T6 Bolna gateway | 94e785c, 03fe112 | Session-bound tools, server-derived idempotency keys, template injection refused, session cap |
| T7 patients and verified changes | b609ae4, 0d00986 | Encrypted patients, uniform verification failures, cancel/reschedule atomicity, lock order, purge race |
| T8 webhooks and reconciliation | e01728c, 4acdfc4 | Path credential, minimization, dedupe and monotonic status, reconcile tenant binding, X-Forwarded-For hops |
| T9 confirmations and reminders | 7bada8b, 30cbc57, 7e85731 | Consent, opt-out scope, quiet hours, budget, unknown outcomes to review, outbound agent separation |
| T10 provisioning | c3bbe2b, 53c0877, 72aeb60 | Version fencing, idempotent agent create, line gating, agent conflicts, kept create intent |
| T11 staff dashboard | 1d0af14, 3205589 | Opt-in CORS; vitest for the client, labels, screens and an HTML-sink source scan. HEAD 3205589: 652 pytest passed, 47 vitest passed |
| T12 final review | this commit | See below |

## T12 final review and documentation

Validated on 29 October 2026, Python 3.14, local PostgreSQL 17, Node from `dashboard/package-lock.json`: `ruff check .` and `ruff format --check .` passed; the full pytest suite with `TEST_DATABASE_URL` on `clinic_voice_test` gave 665 passed; `npm ci && npm run typecheck && npm run build && npm test` in `dashboard/` passed with 47 vitest tests; `npm run gen:api` left `src/api/schema.ts` unchanged, and `clinic-voice export-openapi` matches `docs/openapi.json`.

New PostgreSQL tests cover: a `bolna`-scope token refused by every `/v1/tools/*` route with the generic 401 while the gateway accepts it; the CLI issuing `--scope bolna`; the tool API line gate (403 before verification, reads in verified, reserve only when active or test calls on); turning test calls off waiting for an in-flight reserve, a reserve queued behind the toggle being refused, and racing reserves and toggles staying consistent; reservations recording their credential and appointment-status refusing other credentials' and gateway bookings; `reminder.stop` left pending by a worker without a Bolna port; dead contact jobs refused for requeue (409); the booking reference left out of message text unless the owner turns it on; a plain provisioning reset keeping the attempt credential; and a concurrent agent id unique violation ending in `agent_conflict`.

## Provisioning sets the agent webhook_url (migration 017)

Validated on 29 October 2026, Python 3.14, local PostgreSQL 17: `ruff check .` and `ruff format --check .` passed; the full pytest suite with `TEST_DATABASE_URL` on `clinic_voice_test` gave 682 tests (17 new), 681 passed. The one failure, `test_webhooks.py::test_check_ports_and_cli_guard_bolna`, comes from the local `.env` (it sets `BOLNA_API_KEY`, which the test's `monkeypatch.delenv` cannot remove). The same failure occurs on the previous commit with that `.env`, and the test passes without it. No API schema changed, so `docs/openapi.json` and the dashboard types are untouched.

New tests (FakeBolna and httpx.MockTransport only; nothing reached Bolna) cover: the agent payload's `webhook_url` carrying a `provisioning:webhook` credential whose token is accepted by `POST /v1/webhooks/bolna/{token}` for that clinic and only in webhook scope; promotion on success and revocation of the earlier one after an agent rewrite; a failed or manual_review stop revoking unheld webhook credentials but keeping the held and possibly-held ones; a refused (4xx) or never-sent (compile error) write giving its webhook credential up and restoring the earlier attempt id; `agent_conflict` after a create revoking it; a confirmed reset revoking the attempt one and a plain reset keeping it; verify failing with `webhook_mismatch` (other credential, other host, unreadable, revoked earlier URL), reporting `webhook_unchecked` / `token_and_webhook_unchecked` when the read has no `webhook_url`, and failing with `credential_inactive` when the webhook credential is revoked; activation refusing an expired webhook credential; rows with a NULL `webhook_credential_id` (provisioned before 017) verifying as `webhook_unchecked` and activating; operator-issued webhook credentials never being touched; the compiler's webhook URL validation and redaction; adapter parsing of an optional `webhook_url`; no webhook secret in the database, logs or responses; and the compile-agent CLI printing the webhook URL with `<redacted>` and no `cvt_`/`cvb_`/`cvw_` token.

## Unsubstituted call-context refusal and test isolation from .env

Validated on 31 October 2026, Python 3.14, local PostgreSQL 17: `ruff check .` and `ruff format --check .` passed; the full pytest suite with `TEST_DATABASE_URL` on `clinic_voice_test` gave 730 tests (48 new). In the working tree 728 passed; the two failures, `test_bolna.py::test_compiler_matches_contract_fixture` and `test_bolna.py::test_cli_compile_agent_prints_redacted`, come from an uncommitted owner edit to `agents/prompts/receptionist.md` (the compiled prompt no longer matches the contract fixture); on a clean checkout of this commit all 730 passed. `test_webhooks.py::test_check_ports_and_cli_guard_bolna` now passes with a local `.env` present: `tests/conftest.py` clears `Settings.model_config["env_file"]` at import, so no test reads `.env` (production still reads it from the working directory). `clinic-voice export-openapi` matches `docs/openapi.json` (the refusal is an exception handler, not a schema change), so the dashboard types are untouched.

New tests cover: a templated `execution_id`, `from_number` or `agent_id` refused with 422 `context_unsubstituted` on all eight `/v1/bolna/*` routes (a test asserts the route list is complete) with field names only, `Cache-Control: no-store` and no call session; brace, double-brace, `%(name)s` and bare/case-folded variable-name shapes; several fields listed in order; 401 (not 422) without valid credentials; the refusal coming before the line gate (a closed line still answers 422, and the same request filled in reaches the 403); one warning log line with clinic, tool and field names and no values, credential or body; real E.164, empty, `anonymous`, `withheld` and malformed numbers and real UUIDs never flagged (withheld-number behaviour unchanged); other validation errors keeping the generic shape; the detector's narrowness; and Settings ignoring a `.env` in the working directory under the test harness while an explicit `.env` still loads.

## Webhook provisioning review fixes (conflict revocation, secret-hash verify, redaction, userinfo)

Validated on 31 October 2026, Python 3.14, local PostgreSQL 17: `ruff check .` and `ruff format --check .` passed; the full pytest suite with `TEST_DATABASE_URL` on a disposable `_test` database gave 747 tests (17 new). In the working tree 745 passed; the two failures, `test_bolna.py::test_compiler_matches_contract_fixture` and `test_bolna.py::test_cli_compile_agent_prints_redacted`, come from the uncommitted owner edit to `agents/prompts/receptionist.md`, and both pass on a clean checkout of this commit. `clinic-voice export-openapi` matches `docs/openapi.json` (no API schema change).

New or extended tests (FakeBolna and httpx.MockTransport only) cover: a concurrent `bolna set-agent` winning the compare-and-set clearing and revoking the attempt's tool and webhook credentials (the written agent's tokens no longer authenticate); verify reporting `webhook_mismatch` for the recorded webhook credential id with a different secret and `credential_mismatch` for the recorded tool credential id with a different secret (on every tool or only one); a masked webhook token with the right base verifying as `webhook_unchecked` and with the wrong base failing with `webhook_mismatch`; two create timeouts then a success leaving exactly one active managed credential of each label; another clinic's `provisioning:webhook` and `provisioning:agent` credentials surviving this clinic's stop, confirmed reset, promotion and review; no token secret or `cvb_`/`cvw_` prefix in any table, `provisioning_events` or the logs after a timed-out, refused (4xx) or dead-lettered agent write; `summarize_agent` keeping only SHA-256 digests of token secrets (out of `repr`); `redact()` removing credential-shaped strings anywhere; the compiler refusing a trailing slash (or anything but a webhook token) after the webhook path; and gateway, webhook and settings URLs (`BOLNA_GATEWAY_BASE_URL`, `BOLNA_API_BASE`, `GOOGLE_REDIRECT_URI`) refusing a username or password.

## Context-detection review fixes (ordering comment, env isolation, wider detector, missing-context diagnostics)

Validated on 31 October 2026, Python 3.14, local PostgreSQL 17: `ruff check .` and `ruff format --check .` passed; the full pytest suite with `TEST_DATABASE_URL` on a disposable `_test` database gave 794 tests (47 new). In the working tree 792 passed; the two failures, `test_bolna.py::test_compiler_matches_contract_fixture` and `test_bolna.py::test_cli_compile_agent_prints_redacted`, come from the uncommitted owner edit to `agents/prompts/receptionist.md`. `clinic-voice export-openapi` matches `docs/openapi.json` (no API schema change).

New or extended tests cover: the widened placeholder detector (`%(name)d`, `%(...)r`, `<from_number>` and the other four names in angle brackets, any case) with no false positives on E.164 numbers, upper- or lower-case UUIDs, `anonymous`, `withheld`, `None`, `null`, `unknown`, `50%`, `<unknown>` or an unclosed `%(name)`; an absent, `null`, blank or whitespace `execution_id` refused with 422 `context_missing` (fields `["execution_id"]`, no-store, no call session) on all eight `/v1/bolna/*` routes, logged once without values, with `context_unsubstituted` taking precedence and a non-UUID `execution_id` (`None`, `null`) keeping the generic `uuid_parsing` 422; a malformed `from_number` (`None`, `null`, `unknown`, `anonymous`, a spaced number) accepted, ignored and logged at warning as `malformed=from_number`, empty fields logged at info as `empty=...`, a complete context logging nothing, and none of these lines carrying values; a webhook (`cvw_`), staff session (`cvs_`) or revoked gateway (`cvb_`) token with a template or empty-execution body getting 401, not 422; non-Bolna 422s (`/v1/staff/login`, `/v1/tools/check-slots`) keeping the generic `{loc, type}` shape without `code` or `fields`; the `.env` isolation test asserting `config.ENV_FILE` and, in a fresh interpreter from an empty directory, that `Settings` is configured with it; and the access-log subprocess test running from an empty directory. The conftest clears `Settings`' env file before any other `clinic_voice` import.
