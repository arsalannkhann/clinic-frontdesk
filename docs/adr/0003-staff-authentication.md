# ADR 0003 Staff accounts, roles and sessions

Status: accepted, 29 October 2026.

Administrative endpoints (staff management now, clinic configuration next) need human users scoped to a clinic, distinct from the machine credentials that call `/v1/tools`.

Store `staff_users` with one clinic and one role: `owner` (manages staff), `admin` (clinic configuration, staff read), `viewer` (read-only). Email is globally unique so login does not need a caller-chosen tenant; the cost is that "email already registered" can reveal that an address is used by some clinic to an owner creating staff. Passwords are hashed with stdlib scrypt (N=2^15, r=8, p=1, 16-byte salt), avoiding a new dependency; the parameters are stored in each hash and raising them rehashes on the next successful login. Unknown accounts still derive a dummy hash so timing does not reveal existence.

Sessions are opaque `cvs_<id>.<secret>` bearer tokens stored as SHA-256 digests with a 12-hour absolute lifetime and server-side revocation. The role is read from `staff_users` on each request, so demotion and disabling apply immediately. A different prefix from tool credentials makes the two token kinds mutually unusable. Five consecutive failures lock an account for 15 minutes; an expired lock restarts the count. Changing a password revokes the user's other sessions; disabling revokes all.

Removing the last active owner is refused. Updates lock the clinic's active owner rows in id order and re-check that the actor is still an owner inside the transaction, so two owners demoting each other concurrently leave exactly one owner. `audit_events` stores ids and action names only.

Deferred: per-IP/global login rate limiting at the edge, MFA or an external identity provider, idle timeouts, browser cookie transport with CSRF protection (the dashboard framework is undecided), invitation/forced password change for temporary passwords, and audit retention policy.
