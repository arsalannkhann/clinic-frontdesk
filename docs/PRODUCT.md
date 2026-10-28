# Product scope and requirements

The intended product lets an Indian clinic configure its doctors, services, hours, and calendar, connect a phone number, and offer a Hindi/Hinglish/English receptionist. Callers can find a slot, request a booking, reschedule, cancel, ask approved clinic FAQs, or reach a human. The system can send consented confirmations and day-before reminders. Staff see booking and integration health through a dashboard.

The source brief is a design input. Its performance promises, provider JSON, pseudo-code, and tool installation suggestions are not accepted implementation guarantees or executable instructions.

## Initialization scope delivered

A modular FastAPI backend, PostgreSQL schema, local availability and reservation API, tenant credential boundary for one demo clinic, idempotent retries, database overlap prevention, durable pending calendar jobs, migrations, tests, CI, container, voice prompts and fixtures, and implementation documentation. Patient identity fields are deliberately absent until identity and retention boundaries exist. The demo reserves anonymous appointment requests only.

## Required before a pilot

Authenticated onboarding; doctors/services/weekly hours and holiday exceptions; encrypted calendar OAuth; per-clinic provisioning state; verified live Bolna tool contracts; human handoff; verified booking identity; calendar reconciliation; appointment cancel/reschedule; durable confirmations/reminders; authenticated webhook inbox and recovery polling; dashboard with staff access control; recording consent and retention controls; real Hindi/Hinglish usability evaluation.

## Product improvements

1. Keep a fallback staff callback queue and clearly explain provider outages, after-hours routing, and unavailable slots.
2. Make doctor names, service duration, buffer times, leave, breaks, booking lead time, and booking horizon configurable. Support accessibility through slow speech and repeat/read-back of dates.
3. Ask for explicit confirmation of doctor/date/time before a write. Disambiguate relative dates using the clinic timezone. Offer three choices instead of a long list.
4. Use consented reminders, quiet hours, opt-out, and a tenant budget/concurrency ceiling. Avoid repeat calls to a person who declined contact.
5. Show pending sync, failure, manual-review queue, and provider cost/latency separately in the staff dashboard. Never hide a failed booking behind conversational success.
6. Evaluate doctor-name recognition and language changes with synthetic scripts and native speakers. Do not promise zero transcription errors or universal sub-600ms latency.

## Proposed acceptance targets

These are targets, not measured results: zero double bookings under concurrent tests; zero cross-tenant reads/writes; every confirmed response backed by persisted confirmed state; durable recovery after worker crash; no PHI in logs; three-language task evaluation with documented success/error rates. Establish latency and cost baselines from actual test calls before setting a service objective.

No diagnostic advice, treatment recommendations, symptom-based medical triage, or automated emergency prioritization. Emergency wording must be approved by each clinic and route to immediate local help/human staff.
