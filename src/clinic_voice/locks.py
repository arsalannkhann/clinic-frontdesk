"""Scheduling lock order. Every writer that changes what a doctor can be booked for takes
locks in this global order, and never in reverse, so the paths cannot deadlock:

1. Action advisory lock: reserve(), and the gateway's cancel and reschedule, serialize one
   idempotency key ("<clinic>:<key>").
1a. Call session row FOR NO KEY UPDATE (Bolna gateway only): serializes the per-session
   appointment cap check in reserve(), and verification failure counting in
   bolna.bookings.verify (which takes no other lock).
1b. Clinic row FOR SHARE (Bolna gateway only, gateway.require_line(lock=True)): every
   booking-changing tool re-checks the clinic's line inside its write transaction (reserve,
   cancel, reschedule, opt-out; opt-out takes no earlier lock). Changes that close the line
   update the clinic row (admin.update_clinic, test_calls_enabled off), which takes no other
   lock, so it waits for in-flight tool writes and no tool write commits after it.
2. Doctor-set advisory lock ("doctors:<clinic>"): shared by create_doctor, exclusive for a
   clinic-wide schedule exception, so no doctor appears mid-way through that exception.
3. Doctor rows FOR UPDATE in ascending id order.
4. Patient advisory lock ("patient:<clinic>:<phone hash>"), then the matched patient row
   FOR UPDATE: patients.attach inside reserve(), after the doctor rows, so concurrent
   bookings from one phone attach consistently and a concurrent purge cannot delete the
   matched row underneath (a row already deleted makes attach create a new patient).
5. Appointment rows FOR UPDATE: cancel and reschedule (appointments.py, bolna/bookings.py).
   A reschedule locks the doctor row (3) first; the slot check of the new time runs under it.
6. Outbox rows FOR UPDATE, after the appointment row: staff resolve(requeue) and
   admin.requeue_outbox (which reads the job unlocked, returns 409 at once unless it is
   already dead, locks the appointment, then locks and re-checks the job). Retention purge
   locks patient rows, then updates their appointments; it only selects patients with no
   active, unfinished appointment, so it does not meet a reschedule (appointment row, then
   that patient's retention update).

   The worker does not follow this order everywhere. Its success path (_confirm) updates
   the appointment before its own fenced outbox row, but dead-lettering runs the other way:
   fail_dead and claim()'s lease_expired path update the outbox row first and then the
   on_dead hook updates the appointment (calendar.create -> needs_review). requeue cannot
   meet these: until such a transaction commits the job still reads 'processing', and
   requeue refuses it before taking any lock. Residual: claim()'s lease_expired path can
   meet a stale worker's _confirm on the same appointment in reverse order; PostgreSQL's
   deadlock detector aborts one, and both are retried (the job through its lease, the claim
   batch through the worker loop's backoff).

Webhooks (T8), outside the scheduling locks above: the webhook.process handler locks its
webhook_inbox row, then the call session row FOR NO KEY UPDATE, then (fenced) its own outbox
row. admin.requeue_outbox of a dead webhook.process job locks the inbox row before the
outbox row. The dead-letter hook (outbox row already locked) updates the inbox row only
with SKIP LOCKED, so it never waits on a processor. Retention deletes inbox rows before it
locks call sessions. Housekeeping's reconciliation enqueue locks call session rows with
SKIP LOCKED and only inserts new outbox rows.

Contacts (T9, reminders.py and handlers/reminders.py) extend level 5: appointment row
(FOR UPDATE by a change, FOR SHARE by a send or the housekeeping planner, or the row the
confirming calendar.create transaction updated), then a planner's per-appointment advisory
lock ("contact-plan:<clinic>:<appointment>"), then the contact-budget advisory lock of a
clinic-local day ("contact-budget:<clinic>:<date>"; a planner may take several days, always
in ascending order), then reminder rows, then (a send only) the clinic row FOR SHARE, then
the job's own fenced outbox row. A send re-reads the clinic's contact settings under that
clinic row lock, so a settings change (outbound off, quiet hours, budget) either commits
before the send's checks or waits until the contact is marked sending; the clinic update
takes no other lock. A cancel updates reminder rows after its appointment row; an opt-out
updates only reminder rows (no appointment lock) and then inserts reminder.stop jobs, and a
send re-reads consent and opt-out only after locking its reminder row, so an opt-out
committed before that lock always suppresses the send.

Staff callback calls (P2, callbacks.py) take the contact-budget lock of today, then the
callback_requests row FOR UPDATE, then the clinic row FOR SHARE, and nothing else, so they
fit the contact order above (budget lock before the clinic row) and share its budget.

Provisioning (T10, provisioning.py and handlers/provisioning.py), outside the scheduling
locks: staff requests (advance, select_number, activate, reset) lock the provisioning row
FOR UPDATE (inserting it first when missing), then read the clinic row (FOR SHARE in
activate and the manual-setup check), then insert an outbox row (advance only); they never
lock an existing outbox row. The worker's provision.step takes its own fenced outbox row
(extend_lease), then the provisioning row FOR UPDATE, then updates the clinic row
(bolna_agent_id compare-and-set) and tenant_credentials rows. The dead-letter hook runs with
the outbox row already locked, then updates the provisioning row and tenant_credentials.
The CLI's activate-manual follows the same order: the provisioning row (when it exists),
then the clinic row FOR UPDATE, then inserts or updates the provisioning row.
Gateway line checks take only the clinic row FOR SHARE (level 1b), never provisioning.

Clinic row invariant: every lock on a clinics row taken while the transaction already holds
other locks is FOR SHARE (gateway.require_line(lock=True) at level 1b, a contact send's
settings re-read, provisioning.activate and the manual-setup check). Shared locks never
conflict with each other, so these paths cannot deadlock on the clinic row. Every writer of
the clinics row (admin.update_clinic, the CLI's set-agent, provision.step's agent
compare-and-set) takes that row lock either alone or after only its own provisioning/outbox
rows, never while holding a scheduling lock, so it simply waits for the shared holders. The
one exception, activate_manual's clinic row FOR UPDATE, runs after only the provisioning row
in an operator CLI transaction that takes no other lock. A new path must keep this: never
take the clinic row FOR UPDATE (or update it) while holding a scheduling or contact lock.

No provider or network I/O may run while these are held.

Separate namespace: calendar_sync takes "busy:<clinic>:<doctor>" (advisory, per doctor) while
refreshing the external busy cache. It is taken standalone, never together with the locks
above, so it imposes no ordering.
"""


def _advisory(conn, key: str, shared: bool = False) -> None:
    """Transaction-scoped advisory lock on a text key (hashtextextended, seed 0). Every key
    starts with a namespace prefix (or is "<clinic uuid>:<idempotency key>", which no prefix
    can collide with), so the helpers below never share a key by accident."""
    function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    conn.execute(f"SELECT {function}(hashtextextended(%s, 0))", (key,))  # noqa: S608


def lock_action(conn, clinic_id, key) -> None:
    """Level 1: serialize one idempotency key of a clinic (reserve, and the gateway's cancel
    and reschedule)."""
    _advisory(conn, f"{clinic_id}:{key}")


def lock_doctor_set(conn, clinic_id, exclusive):
    """Level 2: shared by create_doctor, exclusive for a clinic-wide schedule exception."""
    # The "doctors:" prefix keeps this key apart from "<clinic>:<idempotency key>" locks.
    _advisory(conn, f"doctors:{clinic_id}", shared=not exclusive)


def lock_patient_phone(conn, clinic_id, phone_hash: bytes) -> None:
    """Level 4: one (clinic, caller phone hash) while a booking attaches its patient."""
    _advisory(conn, f"patient:{clinic_id}:{phone_hash.hex()}")


def lock_contact_plan(conn, clinic_id, appointment_id) -> None:
    """Level 5 (contacts): contact planning of one appointment, after its row lock."""
    _advisory(conn, f"contact-plan:{clinic_id}:{appointment_id}")


def lock_contact_budget(conn, clinic_id, day) -> None:
    """Level 5 (contacts): the contact budget of one clinic-local day; several days are
    always taken in ascending order."""
    _advisory(conn, f"contact-budget:{clinic_id}:{day.isoformat()}")


def lock_calendar_accounts(conn, clinic_id) -> None:
    """Outside the scheduling order: serializes a clinic's calendar connects and
    disconnects (OAuth callbacks). Taken alone."""
    _advisory(conn, f"calendar_accounts:{clinic_id}")


def lock_busy_refresh(conn, clinic_id, doctor_id) -> None:
    """Separate namespace: calendar_sync's external busy refresh of one doctor. Taken
    standalone, never together with the scheduling locks."""
    _advisory(conn, f"busy:{clinic_id}:{doctor_id}")


def lock_doctors(conn, clinic_id, ids=None):
    """Lock doctor rows of one clinic in ascending id order; return the ids locked.

    ids=None locks every doctor in the clinic. Unknown or foreign ids are simply absent from
    the result, so callers can return 404 when it is empty.
    """
    if ids is None:
        rows = conn.execute(
            "SELECT id FROM doctors WHERE clinic_id=%s ORDER BY id FOR UPDATE", (clinic_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id FROM doctors WHERE clinic_id=%s AND id = ANY(%s) ORDER BY id FOR UPDATE",
            (clinic_id, list(ids)),
        ).fetchall()
    return [r["id"] for r in rows]
