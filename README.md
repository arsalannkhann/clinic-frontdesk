# Clinic Voice AI

A multilingual AI receptionist for Indian clinics, built around Bolna Hosted API, Sarvam speech, and a FastAPI scheduling backend. Intended languages: Hindi, Hinglish, and Indian English.

**Status: tested initialization foundation, not a live receptionist.** The backend can list local slots and atomically reserve them with a durable calendar job. It does not yet connect to Bolna, telephony, WhatsApp, or SMS: a Bolna tool gateway, call sessions and an agent configuration compiler exist but are tested only locally and with mocked HTTP, never against a live Bolna agent. Google OAuth and a Google Calendar adapter exist but have only been exercised against mocked HTTP fixtures, never a live Google account (see docs/OPERATIONS.md for the owner-run setup). Reservations return `202 pending_sync`; they are never presented as confirmed appointments.

## Run locally

Requires Python 3.12–3.14, Docker Compose, and Git. All examples use synthetic data. Run commands from the repository root.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.lock
pip install --no-deps -e .
cp .env.example .env
docker compose up -d --wait db
clinic-voice migrate
clinic-voice seed-demo
clinic-voice credential issue --clinic 00000000-0000-4000-8000-000000000001 --label local-dev
# Prints credential_id and token once. Keep the token in your shell or a secret manager only.
clinic-voice staff create-owner --clinic 00000000-0000-4000-8000-000000000001 --email owner@example.test
# Prompts for a 12+ character password; then POST /v1/staff/login returns a staff session token.
uvicorn clinic_voice.api:app --reload --no-access-log
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

Replace `day` with a date within the next 90 days. Use an offered offset-aware timestamp in `POST /v1/tools/reserve-slot`, with the same IDs, `starts_at`, and an `Idempotency-Key` header of 8–128 safe ASCII characters. Retry the same logical action with the same key and body. A changed request under that key returns 409. The key must be generated/persisted by a trusted call gateway, not invented anew by the LLM on each retry.

## Repository map

- `src/clinic_voice/`: API, tenant credential records, scheduling domain, transactional reservations, CLI.
- `dashboard/`: staff dashboard (TypeScript + Vite, no UI framework); typed client generated from `docs/openapi.json`.
- `migrations/`: versioned PostgreSQL schema, applied atomically with checksums.
- `agents/`: receptionist/reminder prompts, conversation flow, synthetic evaluation scenarios.
- `.claude/agents/`: focused reviewer definitions; `AGENTS.md` and `CLAUDE.md` govern agent work.
- `docs/`: product scope, architecture, API contract, security, operations, roadmap, source review, Claude handoff.
- `.github/`: CI, issue templates, PR checklist.

Start with [architecture](docs/ARCHITECTURE.md), [implementation backlog](docs/ROADMAP.md), and the [Claude handoff](docs/CLAUDE_HANDOFF.md). See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow.

No license is granted by this initialization. The owner must choose a license before third-party redistribution. The original DOCX is intentionally excluded from this public repo; `docs/SOURCE_REVIEW.md` records its requirements and corrections.
