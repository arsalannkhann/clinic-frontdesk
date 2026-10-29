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
   admin.requeue_outbox (which reads the job's appointment id unlocked, locks the
   appointment, then locks and re-checks the job). The worker also updates an appointment
   row before its own outbox row. Retention purge locks patient rows, then updates their
   appointments; it only selects patients with no active, unfinished appointment, so it does
   not meet a reschedule (appointment row, then that patient's retention update).

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
