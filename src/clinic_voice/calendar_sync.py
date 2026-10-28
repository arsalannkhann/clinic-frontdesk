"""Cache of external calendar busy time (ADR 0004). Advisory input to slot math only."""

from uuid import uuid4

from psycopg.pq import TransactionStatus

from clinic_voice.errors import NotFound


def refresh_external_busy(conn, port, clinic_id, doctor_id, start, end):
    """Replace the doctor's cached busy time inside [start, end) with fresh FreeBusy data.

    Returns True if the cache was written, False if skipped because a refresh that began
    fetching after this one already wrote an overlapping row (a stale overwrite).

    Fetches the doctor's busy calendars plus the booking calendar if mapped. The booking
    calendar's events written by this system come back as busy too; that is harmless because
    the same times already block locally. Provider errors propagate before any write, so the
    cache is never partially updated. Cached rows fully inside the window are replaced; rows
    straddling an edge keep their out-of-window part (trimmed, or split in two when a row
    covers the whole window). Rows that ended in the past are pruned.

    FreeBusy is queried once per calendar so each cached row records its real source
    calendar; slot math just unions the rows. Rows are stamped with the database clock taken
    before the fetch, which orders refreshes by when they began fetching. The provider calls
    run with no transaction open, so the connection must be idle and not in autocommit mode
    (RuntimeError otherwise, leaving the caller's pending work untouched); only our own
    read-only lookup is committed. The writes then commit together when the caller's
    connection context exits.

    Concurrent refreshes of one doctor are serialized by an advisory lock on
    'busy:<clinic>:<doctor>' taken in the write transaction, standalone: that namespace
    differs from the 'doctors:<clinic>' and idempotency-key locks in locks.py and is never
    held while waiting for doctor row locks, so it adds no lock-ordering constraint.
    """
    if conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
        raise RuntimeError(
            "refresh_external_busy needs an idle, non-autocommit connection with no open transaction"
        )
    doctor = conn.execute(
        "SELECT booking_calendar_id, busy_calendar_ids, clock_timestamp() AS started"
        " FROM doctors WHERE clinic_id=%s AND id=%s",
        (clinic_id, doctor_id),
    ).fetchone()
    if not doctor:
        raise NotFound()
    conn.commit()
    started = doctor["started"]
    calendars = list(doctor["busy_calendar_ids"])
    if doctor["booking_calendar_id"] and doctor["booking_calendar_id"] not in calendars:
        calendars.append(doctor["booking_calendar_id"])
    # With nothing mapped there is nothing to ask the provider; just clear the window.
    fetched = [(c, port.free_busy([c], start, end)) for c in calendars]
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"busy:{clinic_id}:{doctor_id}",)
    )
    if conn.execute(
        "SELECT 1 FROM external_busy WHERE clinic_id=%s AND doctor_id=%s"
        " AND starts_at < %s AND ends_at > %s AND fetched_at > %s LIMIT 1",
        (clinic_id, doctor_id, end, start, started),
    ).fetchone():
        conn.rollback()
        return False
    scope = (clinic_id, doctor_id)
    conn.execute(
        "DELETE FROM external_busy WHERE clinic_id=%s AND doctor_id=%s AND ends_at < now()", scope
    )
    conn.execute(
        "DELETE FROM external_busy WHERE clinic_id=%s AND doctor_id=%s"
        " AND starts_at >= %s AND ends_at <= %s",
        (*scope, start, end),
    )
    # A row covering the whole window keeps its tail as a second row.
    conn.execute(
        "INSERT INTO external_busy (id,clinic_id,doctor_id,starts_at,ends_at,source_calendar,fetched_at)"
        " SELECT gen_random_uuid(), clinic_id, doctor_id, %s, ends_at, source_calendar, fetched_at"
        " FROM external_busy WHERE clinic_id=%s AND doctor_id=%s AND starts_at < %s AND ends_at > %s",
        (end, *scope, start, end),
    )
    conn.execute(
        "UPDATE external_busy SET ends_at=%s WHERE clinic_id=%s AND doctor_id=%s"
        " AND starts_at < %s AND ends_at > %s",
        (start, *scope, start, start),
    )
    conn.execute(
        "UPDATE external_busy SET starts_at=%s WHERE clinic_id=%s AND doctor_id=%s"
        " AND starts_at >= %s AND starts_at < %s AND ends_at > %s",
        (end, *scope, start, end, end),
    )
    for calendar, intervals in fetched:
        for s, e in intervals:
            s, e = max(s, start), min(e, end)
            if s >= e:
                continue
            conn.execute(
                "INSERT INTO external_busy"
                " (id,clinic_id,doctor_id,starts_at,ends_at,source_calendar,fetched_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (uuid4(), clinic_id, doctor_id, s, e, calendar, started),
            )
    return True
