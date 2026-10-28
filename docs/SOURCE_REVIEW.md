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
- Google OAuth and Calendar (T5), reviewed on 29 October 2026; only `httpx.MockTransport` fixtures were exercised, never a live Google endpoint:
  - [OAuth 2.0 for web server applications](https://developers.google.com/identity/protocols/oauth2/web-server): authorization endpoint `https://accounts.google.com/o/oauth2/v2/auth`, `access_type=offline`, `prompt=consent`, `include_granted_scopes`, `state`, `error` on the redirect; token endpoint `https://oauth2.googleapis.com/token` (authorization_code and refresh_token grants; `scope` lists granted scopes; `refresh_token` only with offline access); revoke endpoint `https://oauth2.googleapis.com/revoke` (POST, form `token`); redirect URIs must be https except loopback. Uncertainty: this page does not document PKCE for web clients.
  - [OAuth 2.0 for iOS and desktop apps](https://developers.google.com/identity/protocols/oauth2/native-app): PKCE parameters (`code_verifier` 43-128 characters, `code_challenge` = base64url SHA-256 without padding, `code_challenge_method` S256). PKCE with a confidential web client is applied here on the basis of RFC 7636 and Google's PKCE support; confirm with a live test client that the token endpoint accepts `code_verifier` together with `client_secret`.
  - [Calendar API scopes](https://developers.google.com/workspace/calendar/api/auth): chosen `calendar.events` (events insert/get/delete) and `calendar.freebusy` (availability). Narrower `calendar.events.owned` was not chosen because a booking calendar shared to (not owned by) the connecting account would fail; `calendar.app.created` would require the system to create the calendar. Revisit when the onboarding flow decides calendar ownership. Both scopes are classed sensitive by Google, so a public OAuth client needs verification.
  - [events.insert](https://developers.google.com/workspace/calendar/api/v3/reference/events/insert), [events.get](https://developers.google.com/workspace/calendar/api/v3/reference/events/get), [events.delete](https://developers.google.com/workspace/calendar/api/v3/reference/events/delete), [Events resource](https://developers.google.com/workspace/calendar/api/v3/reference/events): client ids in base32hex, 5-1024 characters; `dateTime` RFC 3339 with offset; `transparency` opaque; `extendedProperties.private`; deleted events have status `cancelled` and get "always returns them", but only the id is guaranteed and they eventually disappear (adapter: placeholder times for cancelled events without times; 404 thereafter maps to None). The insert page does not document the duplicate-id status; 409 comes from the errors guide.
  - [Calendar API errors](https://developers.google.com/workspace/calendar/api/guides/errors): 401 refresh; 403 `rateLimitExceeded`/`userRateLimitExceeded` and 429 backoff; 404 not found; 409 duplicate id; 410 deleted (deleting an already deleted event); 500 backoff. 403 `quotaExceeded` has no retry guidance and is treated as permanent. 404 can also mean an inaccessible calendar; the adapter's get then returns None and the following insert fails permanently.
  - Reachability check before treating a DELETE 404 as success: an `events.list` call with `maxResults=1` on the same calendar (covered by `calendar.events`). Revoke treats HTTP 400 with `error=invalid_token` as already revoked; this response shape is the standard OAuth one but was not confirmed on Google's pages and needs a live check.
  - [freebusy.query](https://developers.google.com/workspace/calendar/api/v3/reference/freebusy/query): `calendars.(id).errors[]` with reasons `groupTooBig`, `tooManyCalendarsRequested`, `notFound`, `internalError` (more may be added); scopes include `calendar.freebusy`.
- [PostgreSQL ranges](https://www.postgresql.org/docs/current/rangetypes.html): half-open time ranges and exclusion constraints.
