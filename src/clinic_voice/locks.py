"""Scheduling lock order. Every writer that changes what a doctor can be booked for takes
locks in this global order, and never in reverse, so the paths cannot deadlock:

1. Action advisory lock: reserve(), and the gateway's cancel and reschedule, serialize one
   idempotency key ("<clinic>:<key>").
1a. Call session row FOR NO KEY UPDATE (Bolna gateway only): serializes the per-session
   appointment cap check in reserve(), and verification failure counting in
   bolna.bookings.verify (which takes no other lock).
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

No provider or network I/O may run while these are held.

Separate namespace: calendar_sync takes "busy:<clinic>:<doctor>" (advisory, per doctor) while
refreshing the external busy cache. It is taken standalone, never together with the locks
above, so it imposes no ordering.
"""


def lock_doctor_set(conn, clinic_id, exclusive):
    function = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    # The "doctors:" prefix keeps this key apart from "<clinic>:<idempotency key>" locks.
    conn.execute(
        f"SELECT {function}(hashtextextended(%s, 0))",  # noqa: S608
        (f"doctors:{clinic_id}",),
    )


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
