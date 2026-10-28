# Initialization validation

Validated on 29 October 2026 with Python 3.14 and local PostgreSQL 17. These results cover the local foundation only; no live provider credentials or patient data were used.

| Check | Evidence |
| --- | --- |
| Ruff lint and format | `ruff check .` and `ruff format --check .` passed |
| Full application tests | 23 passed with TEST_DATABASE_URL set to a disposable PostgreSQL database |
| Reservation concurrency | Distinct actions for one slot yield one pending reservation and one conflict; concurrent same-key retries return the same booking |
| Database invariants | Direct overlapping insert across services rejected; composite FK prevents cross-tenant references |
| Atomic durability | Forced outbox constraint failure leaves zero committed reservations |
| API behavior | Slot lookup, reserve, retry, pending response and readiness exercised through HTTP TestClient |
| Security boundaries | Missing/wrong auth, caller-supplied tenant, naive timestamps and validation echo behavior tested |
| Migrations | Replay is harmless; changed applied SQL rejected; demo migration and synthetic seed succeeded |
| OpenAPI | Export matches committed schema |
| Docker | Final image built; running container returned ready against seeded DB and used UID 10001; smoke container removed |

The expanded concurrency run exposed a GiST exclusion-check deadlock. Reservation writes now take a short doctor-row lock after the action-key lock, and both full tests and focused concurrency tests passed after the fix. The range exclusion remains the final database safeguard.

Local `.env` contains a generated development credential, is mode 0600, and is ignored by Git. The local Compose database contains synthetic demo records only. An application readiness result reports `integrations: not_configured`; it does not imply a working phone line or external calendar.

Without TEST_DATABASE_URL, database tests skip; that is not sufficient validation for scheduling changes. GitHub Actions is configured to run the full PostgreSQL suite on Python 3.12 and 3.14. Inspect the actual workflow result for the pushed commit rather than treating the workflow definition as evidence of a pass.

## Tenant credential records (branch `claude/tenant-credentials`)

Validated on 29 October 2026, Python 3.14.6, local PostgreSQL 17: `ruff check .` and `ruff format --check .` passed; `pytest -q` with `TEST_DATABASE_URL` on `clinic_voice_test` gave 40 passed. New PostgreSQL tests cover digest-only storage, tenant A token cannot read tenant B doctor/service (and vice versa), wrong secret, unknown id, revoked, expired, rotation grace overlap, immediate rotation, repeat rotation never extending expiry, revoked credentials not rotatable, throttled last-used tracking and the CLI issue/list/revoke path. Malformed tokens are rejected without a database. A local smoke run against the dev database migrated 002, issued a credential, received 200 from check-slots, revoked it and received 401. The OpenAPI export did not change.
