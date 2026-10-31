# Clinic Voice AI

One booking platform for Indian dental and medical clinics, with three ways in for patients and one console for staff. Patients book on the clinic's website, ask the chat assistant, or call the voice receptionist (Bolna, with Sarvam or ElevenLabs speech) in Hindi, Hinglish or Indian English. All three channels write to the same PostgreSQL ledger, so a slot taken by phone disappears from the website at once and double-booking is impossible.

**Status: a working pilot on synthetic data, not yet serving a real clinic.** A demo clinic, the fictional *Aster Dental Studio* in Bengaluru, runs on Vercel and Supabase. Its patients, phone numbers and appointments are made up. Before a real clinic goes live, the clinic owner has to: approve the scripts, connect a phone number, choose a messaging provider and set the hosting plan (see [Before a real clinic](#before-a-real-clinic)).

## How it fits together

```
patients ── website + chat (Next.js, Vercel) ──┐  server-side proxy, httpOnly staff cookie
staff ───── staff console (same app, /staff) ──┤
callers ─── Bolna voice agent ─────────────────┤
                                               ▼
                        API (FastAPI on Vercel Python, dub1)
                          /v1/public  /v1/chat  /v1/staff  /v1/admin  /v1/bolna  /v1/webhooks
                                               │
                        PostgreSQL (Supabase, transaction pooler)
                          pg_cron ── every minute ──► /v1/internal/worker/tick
```

- **One booking core.** The web booking form, the chat confirmation card, staff bookings and the voice agent's tools all go through the same reservation code. That code follows the same lock order (`locks.py`) and relies on a PostgreSQL range exclusion, which rejects overlapping bookings at the database level.
- **The chat assistant can only propose.** It finds real free slots through tools and shows a confirmation card. The booking happens only when the patient confirms in that card with their name, phone and consent. The model is OpenAI-compatible and configured by environment variables; the default is Sarvam `sarvam-105b`.
- **Ledger calendar.** For clinics without Google Calendar, the platform itself is the calendar of record, and bookings confirm as soon as they are made. Google Calendar is supported for clinics that use it.
- **Outbound callbacks are staff-initiated only.** Patients request a callback from the website, the chat or a call. Staff place it with "Call now", which only works when outbound calling is switched on, a calling number is set, and the call falls inside quiet hours and the daily budget. Nothing dials automatically, and the demo clinic's synthetic contacts can never be called.
- **Tenant isolation.** The clinic is always derived from the credential or staff session, never from the request. Patient details and chat transcripts are encrypted at rest, and logs hold ids and codes only.

## What is implemented

- **Scheduling.** Clinics, doctors, services, weekly hours, holidays, leave and breaks, lead time, buffers and a booking horizon. Idempotent reservations commit together with their calendar job.
- **Patient channels:**
  - a public clinic API, with rate limits per IP and per phone;
  - booking lookup by reference and phone, with a uniform 404 for any mismatch;
  - callback requests;
  - the chat assistant, with server-side encrypted history, an emergency script, turn and cost caps, and a graceful fallback.
- **Voice.**
  - **Bolna tool gateway:** check slots, reserve, status, verify, cancel, reschedule, opt-out.
  - **Webhook inbox:** stores Bolna's call events, with reconciliation polling.
  - **Agent compiler and provisioning:** builds the agent configuration and runs clinic setup as a state machine.
- **Staff:**
  - owner, admin and viewer roles, sessions, lockout and an audit trail;
  - a calendar (day and week views) and appointments with a needs-review queue;
  - a callback queue, chat and call transcripts, real stats, and clinic profile and outbound settings.
- **Background work.** A durable outbox worker with leases, fencing, backoff and a dead-letter queue. It runs as a long-lived worker locally, or as a bounded tick triggered by Supabase `pg_cron` on serverless hosting.
- **Reminders and confirmations.** Consent, quiet hours, budgets and opt-outs are implemented. They are off by default, and no messaging provider is connected yet.

## Repository map

- `src/clinic_voice/`: the API, scheduling, patients, chat, callbacks, Bolna gateway and provider ports, worker, provisioning and CLI.
- `web/`: Next.js 15 app containing the clinic website, booking, chat widget and staff console, plus a dev mock mode used when `API_BASE_URL` is unset.
- `dashboard/`: operator console for provisioning, the outbox and reminders (TypeScript and Vite). Its typed client is generated from `docs/openapi.json`.
- `migrations/`: versioned PostgreSQL schema. Applied files are frozen, so changes go in a new numbered file. From 018 on, migrations enable row-level security and lock out Supabase's `anon` and `authenticated` roles.
- `agents/`: the receptionist prompt, the conversation flow and synthetic evaluation scenarios.
- `docs/`: the [platform plan](docs/plans/2026-10-31-platform.md), [architecture](docs/ARCHITECTURE.md), [API](docs/API.md), [operations](docs/OPERATIONS.md), [security](docs/SECURITY.md), [roadmap](docs/ROADMAP.md), [ADRs](docs/adr/), and the [Claude handoff](docs/CLAUDE_HANDOFF.md).
- `index.py`, `vercel.json`, `.vercelignore`: the API's Vercel project (repository root). The web project uses `web/vercel.json`.

## Run it locally

You need Python 3.12–3.14, Docker Compose, Node 20.19+ or 22.12+, and Git. Run commands from the repository root.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.lock
pip install --no-deps -e .
cp .env.example .env            # APP_ENV=development; DATABASE_URL points at clinic_voice_dev
docker compose up -d --wait db
docker compose exec db createdb -U clinic clinic_voice_dev
clinic-voice migrate
clinic-voice seed-platform      # synthetic Aster Dental Studio (slug aster-dental)
clinic-voice staff create-owner --clinic 2e8cac77-52f3-532f-b098-543a61318556 --email owner@example.test
# The seed's ids are deterministic; this is Aster Dental Studio. Prompts for a 12+ character password.
uvicorn clinic_voice.api:app --reload --no-access-log
```

Start the website and staff console in another shell:

```sh
cd web && npm ci && npm run dev
```

Set `API_BASE_URL=http://127.0.0.1:8000` in `web/.env.local` to use the local API. Leave it unset to run the whole UI against built-in mock data, with no backend at all.

The chat assistant needs `CHAT_LLM_API_KEY`. Without it, the chat answers with guided quick replies. Bolna, Google and messaging stay off unless you set their keys. `.env.example` lists every setting.

## Verify

```sh
ruff check . && ruff format --check .
docker compose exec db createdb -U clinic clinic_voice_test
TEST_DATABASE_URL=postgresql://clinic:clinic_local_only@localhost:55432/clinic_voice_test pytest -q
(cd web && npm ci && npm run lint && npm run typecheck && npm test && npm run build)
(cd dashboard && npm ci && npm run typecheck && npm run build && npm test)
```

The PostgreSQL tests refuse any database whose name does not end in `_test`, because they delete table contents. Never point them at real data. CI also checks that `docs/openapi.json` matches the code.

## Deploy (Vercel + Supabase)

The demo runs as two Vercel projects in one repository. Both run in region `dub1`, next to the Supabase project in `eu-west-1`.

| Project | Root | Key settings |
| --- | --- | --- |
| API | repository root | `DATABASE_URL` (Supabase transaction pooler, port 6543), `DATABASE_POOLER=transaction`, `APP_ENV=production`, `TOKEN_ENCRYPTION_KEYS`, `PHONE_HASH_KEY`, `PUBLIC_PROXY_SECRET`, `WORKER_TICK_SECRET`, `BOLNA_API_KEY`, `BOLNA_GATEWAY_BASE_URL`, `CHAT_LLM_API_KEY` |
| Web | `web/` | `API_BASE_URL`, `PUBLIC_PROXY_SECRET` (the same value as the API's), `CLINIC_SLUG`, `SITE_URL` |

1. Run `clinic-voice migrate` once against the Supabase session pooler (port 5432). Migrations never run when the app starts.
2. Seed or onboard the clinic, then create its owner with `clinic-voice staff create-owner`.
3. Schedule the worker tick with `pg_cron` and `pg_net`. Keep the bearer secret in Supabase Vault and set `timeout_milliseconds` to 25000. [OPERATIONS.md](docs/OPERATIONS.md) has the details.
4. Create the clinic's Bolna agent against the API's public URL, then run `clinic-voice bolna set-agent` and `clinic-voice provisioning activate-manual`.

In production, the API refuses public and chat traffic that doesn't carry the proxy secret, so only the web app can reach those routes.

## Before a real clinic

- **Scripts and disclosure.** The clinic approves its own emergency and handoff wording. Keep the AI disclosure in the voice prompt.
- **Telephony.** Connect a Bolna phone number. Switch on `outbound_enabled` only after the owner authorizes patient contact.
- **Messaging.** Choose a messaging provider that meets the idempotency criteria and DLT/TRAI rules.
- **Hosting.** Vercel Hobby is non-commercial, so move to a paid plan. Consider a Mumbai (`ap-south-1`) database for lower voice latency.
- **Security operations.** Rotate every key that was shared during setup. Agree a retention period with the clinic and schedule the purge. Run a restore drill.

The full list is in [docs/ROADMAP.md](docs/ROADMAP.md) and [docs/CLAUDE_HANDOFF.md](docs/CLAUDE_HANDOFF.md).

No license is granted yet; the owner must choose one before third-party redistribution.
