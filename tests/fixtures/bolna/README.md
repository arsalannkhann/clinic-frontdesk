Sanitized Bolna contract fixtures. Shapes follow the official API reference checked on
2026-10-29 (docs/SOURCE_REVIEW.md); values are synthetic (example.test hosts, +9100000xxxxx
numbers, placeholder ids). None was captured from the live API.

- agent_synthetic_clinic.json: compiler output for the seeded demo clinic, token redacted.
- api_*.json: provider response bodies used with httpx.MockTransport.
