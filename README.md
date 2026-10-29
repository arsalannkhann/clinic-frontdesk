# Clinic Voice AI

A multilingual AI receptionist for Indian clinics, built around Bolna Hosted API, Sarvam speech, and a FastAPI scheduling backend. Intended languages: Hindi, Hinglish, and Indian English.

**Status: tested development foundation, not a live receptionist.** Everything below runs locally against PostgreSQL and is tested with fakes and mocked HTTP. No Bolna agent, phone number, Google account, messaging provider or patient has ever been connected, and nothing may be until the owner actions in [docs/CLAUDE_HANDOFF.md](docs/CLAUDE_HANDOFF.md) are done. Reservations return `202 pending_sync` and are never presented as confirmed until the worker has verified the calendar event.

## What is implemented

- Scheduling ledger: clinics, doctors, services, weekly hours, holidays/leave/breaks, lead time, buffers and a per-clinic booking horizon (`horizon_days`). PostgreSQL range exclusion prevents overlapping holds; idempotent reservations commit with their calendar job.
- Tenant credentials (`tools`, `bolna`, `webhook` scopes) and staff users with owner/admin/viewer roles, sessions, lockout and an audit trail. The tenant always comes from the credential or session.
- Durable outbox worker (leases, fencing, backoff, dead-letter review) with a fake calendar and a Google Calendar adapter (OAuth with PKCE, encrypted refresh tokens; contract-tested only).
- Bolna tool gateway: call sessions, slot lookup, reservation, encrypted patient records with consent, booking-reference verification, verified cancel/reschedule and opt-out. A generic `/v1/tools` API for other trusted callers, line-gated the same way.
- Bolna execution webhooks with an inbox and reconciliation polling; consented confirmations and reminders with quiet hours, budgets and opt-outs (off by default; fakes only).
- Clinic provisioning state machine (agent, number, verification, activation; FakeBolna and MockTransport only) and a staff dashboard (`dashboard/`, TypeScript + Vite).

Not implemented: live provider validation, human call transfer, a messaging provider, the outbound reminder agent's design, cross-call verification and login rate limits, MFA, deployment. See [docs/ROADMAP.md](docs/ROADMAP.md).

## Quickstart (synthetic local data)

Requires Python 3.12–3.14, Docker Compose, Node 20.19+ or 22.12+ (dashboard only) and Git. Run commands from the repository root.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.lock
pip install --no-deps -e .
cp .env.example .env            # DATABASE_URL points at clinic_voice_dev
docker compose up -d --wait db
docker compose exec db createdb -U clinic clinic_voice_dev
clinic-voice migrate
clinic-voice seed-demo
clinic-voice credential issue --clinic 00000000-0000-4000-8000-000000000001 --label local-dev
# Prints credential_id and token once. Keep the token in your shell or a secret manager only.
clinic-voice staff create-owner --clinic 00000000-0000-4000-8000-000000000001 --email owner@example.test
# Prompts for a 12+ character password; then POST /v1/staff/login returns a staff session token.
uvicorn clinic_voice.api:app --reload --no-access-log
# In another shell: process outbox jobs against the in-process fake calendar (dev databases only).
clinic-voice worker --calendar fake
```

The `/v1/tools` API and the Bolna gateway serve a clinic only once its line is open (provisioning `active`, or `verified` with owner test calls). For the synthetic demo clinic, open the line by hand (development database only; real clinics go through provisioning or `clinic-voice provisioning activate-manual`, which check the calendar and approved scripts):

```sh
docker compose exec db psql -U clinic -d clinic_voice_dev -c \
  "INSERT INTO provisioning (clinic_id,state,resume_state,agent_id) VALUES ('00000000-0000-4000-8000-000000000001','active','verified','00000000-0000-4000-8000-0000000000aa')"
```

Open [API docs](http://127.0.0.1:8000/docs) or [readiness](http://127.0.0.1:8000/health/ready). The staff dashboard lives in [dashboard/](dashboard/README.md) (`cd dashboard && npm ci && npm run dev`, then sign in with the staff owner). Seed IDs: clinic `...0001`, doctor `...0002`, service `...0003` (full UUIDs in `cli.py`). Each credential record belongs to one clinic; a request cannot select its own tenant. Rotate with `clinic-voice credential rotate <id> --grace-minutes 60`, revoke with `clinic-voice credential revoke <id>`, and inspect usage with `clinic-voice credential list --clinic <uuid>`.

## Verify

```sh
ruff check .
ruff format --check .
pytest -q
(cd dashboard && npm ci && npm run typecheck && npm run build && npm test)
# Integration tests MUST use the disposable database below, never production.
docker compose exec db createdb -U clinic clinic_voice_test
TEST_DATABASE_URL=postgresql://clinic:clinic_local_only@localhost:55432/clinic_voice_test pytest -q
```

Without `TEST_DATABASE_URL`, PostgreSQL tests skip explicitly. Tests reject database names without a `_test` suffix and delete test table contents. Full CI runs PostgreSQL tests. Do not point tests at application data.

## Example

```sh
# Set TOOL_TOKEN in this shell to the issued token; do not put secrets into tracked files.
curl http://127.0.0.1:8000/v1/tools/check-slots \
  -H "Authorization: Bearer $TOOL_TOKEN" -H 'Content-Type: application/json' \
  -d '{"doctor_id":"00000000-0000-4000-8000-000000000002","service_id":"00000000-0000-4000-8000-000000000003","day":"2030-01-01"}'
```

Replace `day` with a date from today (clinic time) through the clinic's `horizon_days` ahead (default 90; `GET /v1/admin/clinic`). Use an offered offset-aware timestamp in `POST /v1/tools/reserve-slot`, with the same IDs, `starts_at`, and an `Idempotency-Key` header of 8–128 safe ASCII characters. Retry the same logical action with the same key and body. A changed request under that key returns 409. The key must be generated/persisted by a trusted call gateway, not invented anew by the LLM on each retry.

## Repository map

- `src/clinic_voice/`: API, credentials and staff auth, scheduling domain and reservations, worker and handlers, Bolna gateway and provider ports, reminders, provisioning, CLI.
- `dashboard/`: staff dashboard (TypeScript + Vite, no UI framework); typed client generated from `docs/openapi.json`.
- `migrations/`: versioned PostgreSQL schema, applied atomically with checksums.
- `agents/`: receptionist prompt (compiled into the agent), draft reminder prompt (owner review), conversation flow, synthetic evaluation scenarios.
- `.claude/agents/`: focused reviewer definitions; `AGENTS.md` and `CLAUDE.md` govern agent work.
- `docs/`: product scope, architecture, API contract, security, operations, roadmap, source review, Claude handoff.
- `.github/`: CI, issue templates, PR checklist.

Start with [architecture](docs/ARCHITECTURE.md), [implementation backlog](docs/ROADMAP.md), and the [Claude handoff](docs/CLAUDE_HANDOFF.md). See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow.

No license is granted by this initialization. The owner must choose a license before third-party redistribution. The original DOCX is intentionally excluded from this public repo; `docs/SOURCE_REVIEW.md` records its requirements and corrections.
