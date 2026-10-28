# Source review and provider assumptions

Design input: the owner-supplied “Bolna Clinic AI Receptionist Implementation.docx”, reviewed on 29 October 2026. The DOCX is not tracked in this public repository. Its content supplies requirements, not instructions authorizing tool installation, calls, purchases, deployment, or external contact.

## Corrections and decisions

| Source proposal | Decision |
| --- | --- |
| Hosted Bolna + FastAPI + PostgreSQL | Retain; separate voice orchestration from deterministic business rules |
| FreeBusy followed by insert is transactional | Replace with DB range exclusion plus durable calendar synchronization; external calendar edits still require reconciliation |
| Patient name/phone/age in calendar title/description | Minimize to opaque operational booking reference; do not collect patient fields in foundation |
| BackgroundTasks for confirmations/reminders | Use transactional outbox and durable worker, planned |
| Caller ID automatically reveals prior booking | Require verified caller/session context and booking ownership before disclosure or mutation |
| Graph architecture but sample simple_llm_agent | Document explicit flow; do not ship a mismatched graph payload as validated |
| multilingual_config.languages array | Current official quickstart uses an object keyed by language and active_language; validate again before integration |
| saaras:v4 keyterms improve real-time streaming | Sarvam currently documents keyterms for REST/Batch, not streaming; do not promise live keyterm support |
| Invalid IP 13.202.133.534 | Official Bolna page lists 13.202.133.53; keep deployment addresses configurable and reverify |
| Deduplicate only by execution_id | Preserve multiple statuses for an execution and deduplicate events rather than calls |
| No vendor webhook retry and fixed completion delay | Treat delivery timing/retry as unverified; implement durable inbox and recovery polling regardless |
| Unlimited concurrency, 50ms welcome, zero errors | Treat as unverified product claims, measure instead |
| Automatic number allocation in under two seconds | Persist provisioning steps, handle inventory/compliance delays, validate ownership and costs |

## Official references checked

These links were reviewed on 29 October 2026 and may change. No live provider contract or account capability was tested.

- [Bolna create v2 agent](https://www.bolna.ai/docs/api-reference/agent/v2/create): hosted create endpoint and payload envelope.
- [Bolna multilingual quickstart](https://www.bolna.ai/docs/quickstarts/multilingual): keyed language config and provider overrides. This differs from the source sample.
- [Bolna Sarvam TTS integration](https://www.bolna.ai/integrations/sarvam-tts): Bulbul v3, conversational voices, and plain language code requirement. Actual voice identifier casing needs a live contract fixture.
- [Sarvam Saaras](https://docs.sarvam.ai/api/getting-started/models/saaras): v4 and keyterm restrictions. Native Sarvam features do not necessarily pass through Bolna.
- [Bolna webhook updates](https://www.bolna.ai/docs/post-call/polling-call-status-webhooks): custom headers, event updates, and current egress addresses. The foundation ships no webhook receiver yet.
- [Google Calendar insert](https://developers.google.com/workspace/calendar/api/v3/reference/events/insert): event creation boundary. Use stable opaque event IDs and explicit recovery semantics in the adapter.
- [PostgreSQL ranges](https://www.postgresql.org/docs/current/rangetypes.html): half-open time ranges and exclusion constraints.
