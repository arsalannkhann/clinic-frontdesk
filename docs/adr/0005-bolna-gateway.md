# ADR 0005 Bolna tool gateway and call sessions

Status: accepted, 29 October 2026.

Bolna calls our HTTP endpoints as custom function tools during a call. The official docs (docs/SOURCE_REVIEW.md) describe the tool shape and call-context variables, but they do not say whether the platform or the LLM writes those variables into a tool request, and nothing has been checked against the live API.

Decisions:

- Authentication reuses the per-clinic tool credential (ADR 0002). The compiled agent puts `Bearer cvt_...` into each tool's `api_token`, the field Bolna documents as the Authorization header value, so the gateway uses the same `tenant` dependency as `/v1/tools`. No undocumented custom-header field is emitted. The token is a compile-time argument held only in memory; review output and fixtures are redacted.
- `execution_id`, `from_number` and `agent_id` arrive in the body through `%(name)s` templating and are treated as provider-asserted. They scope a call session (`call_sessions`, unique per clinic and execution id) and never select the tenant or prove identity. The caller number is encrypted with the existing keyring (associated data bound to clinic and session) and hashed with `PHONE_HASH_KEY` for the lookups that T7 will need. Malformed or withheld numbers and agent ids are ignored, not rejected, so an unrendered template cannot break a call; a malformed `execution_id` is rejected.
- Writes get server-derived idempotency keys, `bolna:<execution_id>:<tool>:<sha256(canonical args)[:24]>`, over the resolved ids and the UTC start. Bolna retries replay; the LLM never supplies keys. The generic API refuses the `bolna:` prefix, so only the gateway can create session-bound keys.
- Appointments created through the gateway record `call_session_id` (composite foreign key). The gateway's status tool only reports appointments from the same session, a stopgap until verified booking lookup (T7).
- The context endpoint publishes doctor and service names only, so the tools take names and the gateway resolves them by exact match after case and whitespace folding. Fuzzy matching is rejected: a wrong doctor is worse than a clarification. Duplicate active names make the tool return 409; staff must rename.
- The compiled agent is a `simple_llm_agent`. The design flow in `agents/flow.yaml` is not enforced by Bolna; safety rules live in the gateway (no confirmation before a persisted `confirmed`, session-bound status) rather than in prompt text.

Consequences: anyone holding a clinic's tool credential can act inside any execution id of that clinic, as with `/v1/tools`; the credential stays the security boundary. Sessions are never ended (no webhook receiver yet) and caller-number retention is undecided. Every provider shape must be re-verified with an owner-authorized test agent before use.
