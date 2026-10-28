# Architecture

Use a modular monolith with API and worker processes sharing PostgreSQL. Bolna handles telephony and voice orchestration; Sarvam remains behind Bolna's provider interface. No Redis is needed for the initial backend: PostgreSQL owns reservations, durable jobs, and later webhook inbox records. Add infrastructure only when measured demand justifies it.

```mermaid
flowchart LR
  Caller --> Bolna[Bolna hosted voice]
  Bolna --> Speech[Sarvam speech]
  Bolna --> Gateway[Authenticated tool gateway planned]
  Staff[Staff dashboard planned] --> API[FastAPI]
  Gateway --> API
  API --> DB[(PostgreSQL)]
  DB --> Worker[Durable worker planned]
  Worker --> Calendar[Google Calendar planned]
  Worker --> Delivery[Messaging and reminders planned]
```

Only FastAPI, PostgreSQL, local reservations, and outbox insertion exist today. All provider arrows are planned integration boundaries.

## Booking consistency

PostgreSQL is the authoritative reservation ledger. `appointments` uses a GiST exclusion constraint on clinic, doctor, and `[start,end)` timestamp range. This blocks overlapping pending/confirmed/review reservations across services and processes. A unique tenant/idempotency key plus a canonical body hash makes retries replay the same appointment or reject a changed request. A transaction commits the reservation and `calendar.create` outbox entry together. Slot discovery is advisory. A short doctor-row lock orders reservation writes and avoids exclusion-check deadlocks between simultaneous inserts; the range constraint remains the final invariant for every database writer. No provider I/O runs under this lock.

The initial API returns 202 `pending_sync`, without patient data. There is no worker today. Pending reservations remain held until explicitly resolved; monitor and clean synthetic development data manually. Do not enable pilot traffic in this state.

The next worker must claim jobs with `FOR UPDATE SKIP LOCKED`, lease expiry, bounded backoff, and a dead-letter/manual-review state. Network I/O must happen outside a long database transaction. Use an opaque deterministic Google event ID derived from appointment ID, recover ambiguous timeouts by fetching that ID, and mark confirmed only after verifying the event. Store provider IDs and state transitions durably. Replays must not duplicate calendar events, messages, or reminders.

Google FreeBusy is not a distributed lock. Before a pilot choose an explicit ownership policy: a clinic-managed dedicated booking calendar plus external busy-calendar reads is preferred. Human edits and external systems can race our ledger; incremental sync/reconciliation and a manual conflict policy are required. Never imply a database constraint prevents external calendar races.

## Planned tenant boundary

The bootstrap token maps to one configured clinic UUID and never reads tenant identity from tool arguments. All current catalog/appointment reads filter by clinic; composite foreign keys prevent cross-clinic doctor/service references. This is not production multi-tenant auth: next add credential records with rotation/revocation, roles for staff, object-level authorization, and gateway-verified call/session context. Consider PostgreSQL RLS as defense in depth, with isolated role tests, before a pilot.

Caller ID is untrusted. Do not greet with private booking information or allow cancellation using only a phone number. Add a clinic-approved verification mechanism and handle shared phones. Never ask an LLM to decide tenant identity or invent idempotency keys.

## Provisioning and lifecycle

Plan onboarding as a recoverable state machine: draft → calendar_connected → agent_created → number_assigned → verified → active, with failed/manual-review states. Persist provider IDs after each step, reconcile uncertain results, and never buy a second number on a blind retry. Connect existing clinic numbers only after routing ownership is verified.

Booking lifecycle: pending_sync → confirmed or needs_review; confirmed → cancellation_pending/reschedule_pending → resolved. New states require migrations and business tests. Reserve the new slot before releasing the old one; keep the original booking when a reschedule fails. Use appointment versions to suppress stale reminders after rescheduling/cancellation.

## Webhook and data boundaries

Plan a dedicated authenticated inbox: validate custom secret header, map agent/execution to tenant on the server, enforce body size, redact/minimize payload, persist before 2xx, process asynchronously. Deduplicate by provider event ID or execution+status+payload hash; execution ID alone drops later state updates. Reject regressions after terminal state. Poll executions to reconcile missing deliveries; do not rely on vendor retries.

No clinical notes in calendar titles, prompt variables, logs, or analytics. Use opaque booking references and minimum operational information. Encrypt refresh tokens with managed keys; separate retention for appointment metadata, recordings, transcripts, and audit records. Recording storage is disabled by default in the planned integration.
