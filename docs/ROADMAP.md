# Implementation roadmap

## 0 Foundation complete

Local slot computation, anonymous reservations, tenant token mapping, PostgreSQL exclusion and composite foreign keys, retry idempotency, atomic outbox insertion, migrations, CLI, tests, CI, source review, voice prompts and handoff. Provider calls are not implemented.

## 1 Trusted clinic configuration and durable calendar integration

Implement credential records and rotation, staff authentication, clinic/doctor/service/hours CRUD with validated IANA zones, holiday/leave exceptions, lead time and buffers. Add encrypted Google OAuth using state/PKCE and minimum required scopes. Add durable job leases, retry backoff, dead-letter review, calendar adapter fake and live contract tests, deterministic event IDs, and timeout reconciliation. Add appointment version and calendar-sync state. Establish calendar ownership and external-conflict handling.

Progress: tenant credential records with hashed secrets, rotation, expiry and revocation are implemented (migration 002, ADR 0002) and replace the bootstrap token. Staff users, roles (owner/admin/viewer), scrypt passwords, sessions, lockout, staff management API and an audit trail are implemented (migration 003, ADR 0003). Clinic/doctor/service/hours admin API with validated IANA zones, per-clinic booking horizon, and active flags is implemented (migration 004). Schedule exceptions (clinic-wide holidays, doctor leave, breaks) with conflict reporting, lead-time enforcement and service buffers snapshotted into `appointments.blocks_until` and enforced by the exclusion constraint are implemented (migration 005). Slot math steps in absolute time (DST-safe), and every scheduling writer uses the lock order in `locks.py`. Milestone 2 reschedule and status changes must recompute `blocks_until`; pre-005 binaries cannot insert appointments once 005 is applied. The calendar port, fake adapter, per-doctor calendar mapping and the advisory external busy cache are implemented (migration 006, ADR 0004). The durable outbox worker is implemented (migrations 007 and 008): leased `FOR UPDATE SKIP LOCKED` claims, fenced completion, bounded backoff, dead-letter with `needs_review`, deterministic-id recovery of ambiguous calendar writes, external busy re-check before insert, `appointment-status` tool with confirmation wording only for persisted `confirmed`, and staff dead-letter review/requeue. Google OAuth (state digest + PKCE, encrypted refresh tokens with a rotatable keyring, revocation) and the Google Calendar adapter with per-doctor account resolution in the worker are implemented (migration 009, T5) and contract-tested with `httpx.MockTransport` only. Remaining in this milestone: owner-run validation against a real test Google account and calendar (OAuth client setup in OPERATIONS.md), reconciliation of human calendar edits, and enqueueing cancel/refresh jobs.

Acceptance: tenant A cannot reference tenant B IDs; revoked keys fail; two workers do not duplicate events; crash after provider success recovers; external busy time is respected; no unverified spoken confirmation. Exclusion concurrency tests remain green.

## 2 Appointment lifecycle and caller verification

Add minimum patient contact information with consent/retention/encryption, verified call/session context, explicit booking confirmation, opaque booking lookup, verified cancel/reschedule, audit transitions. Require appointment reference and caller verification rather than phone-only lookup. Build a trusted gateway translating Bolna tools to backend APIs with stable action IDs.

Progress (T6): call sessions (migration 011; caller number encrypted and HMAC-hashed), the Bolna tool gateway (check-slots, reserve-slot with server-derived idempotency keys, session-bound appointment-status) and the context endpoint are implemented and PostgreSQL-tested. Gateway reservations are capped at 3 active (pending_sync/confirmed) appointments per call session. Not yet: identity verification, booking references, cancel/reschedule, session end/retention, patient contact data. T7 retention: `appointments.call_session_id` references `call_sessions` without `ON DELETE`, so deleting or purging call sessions must first set `call_session_id` to NULL on their appointments (in the same transaction), or the delete fails; decide whether the caller-number ciphertext/hash is erased earlier than the session row.

Acceptance: shared-phone caller cannot enumerate or modify another person's bookings; failed reschedule preserves old slot; retry/cancel races yield one consistent state; cancelled bookings free slots only under the documented sync policy.

## 3 Bolna voice and clinic provisioning

Validate current hosted agent, tool, graph, language and voice schemas.

Progress (T6): official docs re-read on 29 October 2026 (SOURCE_REVIEW.md); an agent configuration compiler with sanitized contract fixtures, `clinic-voice bolna compile-agent`, and a `BolnaPort` with fake and httpx client (MockTransport only) exist. Nothing has been validated against the live API; provisioning, number assignment and test calls remain. Start with a working narrow booking flow; introduce graph-enforced actions when contract-tested. Persist provisioning steps and reconcile failures. Connect an existing verified test number before automating purchase. Human transfer, after-hours policy, emergency script, interruption behavior and no-confirm-before-tool-success are mandatory.

Acceptance: authorized test calls complete English/Hindi/Hinglish flows; caller correction and date read-back work; provider failures give a safe fallback. Record measured latency/cost and sanitized contract fixtures. No promises of instantaneous number allocation or error-free transcription.

## 4 Delivery and webhook recovery

Implement inbox persistence/auth/deduplication, terminal-state monotonicity, reconciliation polling, minimal confirmation messages, opt-in reminders, quiet hours and opt-out, execution IDs and versioned cancellation. Avoid duplicate contact after ambiguous provider success. Persist jobs before responding to webhooks.

Acceptance: same execution can deliver multiple statuses without losing final updates; duplicate completed events are harmless; timeout recovery avoids duplicate reminders; opt-out and cancellation suppress later dialing.

## 5 Staff dashboard and pilot

Build a TypeScript dashboard (framework decision can wait until backend contracts settle) for onboarding, schedules, appointments and pending failures. Add staff handoff queue and minimal aggregate analytics. Put access control on recordings and keep them disabled until approved consent/retention exists. Perform restore drill, tenant access audit, native-speaker evaluation and synthetic end-to-end demo before pilot approval.

Defer voice biometrics, medical diagnosis/triage, a custom telephony engine, microservices and Redis unless justified by measured requirements.
