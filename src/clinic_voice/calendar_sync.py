"""Cache of external calendar busy time (ADR 0004). Advisory input to slot math only."""

from uuid import uuid4

from fastapi import HTTPException


def refresh_external_busy(conn, port, clinic_id, doctor_id, start, end):
    """Replace the doctor's cached busy rows overlapping [start, end) with fresh FreeBusy data.

    Fetches the doctor's busy calendars plus the booking calendar if mapped. The booking
    calendar's events written by this system come back as busy too; that is harmless because
    the same times already block locally. Provider errors propagate before any write, so the
    cache is never partially updated. Cached rows that straddle a window edge are replaced
    whole (their out-of-window part is dropped until a later refresh covers it). Returns the
    number of intervals cached.

    The provider call runs with no transaction open; the caller's connection must not already
    be inside one (this function commits the read-only lookup). The delete and inserts then
    commit together when the caller's connection context exits.
    """
    doctor = conn.execute(
        "SELECT booking_calendar_id, busy_calendar_ids FROM doctors WHERE clinic_id=%s AND id=%s",
        (clinic_id, doctor_id),
    ).fetchone()
    if not doctor:
        raise HTTPException(404, "Not found")
    conn.commit()
    calendars = list(doctor["busy_calendar_ids"])
    if doctor["booking_calendar_id"] and doctor["booking_calendar_id"] not in calendars:
        calendars.append(doctor["booking_calendar_id"])
    # With nothing mapped there is nothing to ask the provider; just clear the window.
    intervals = port.free_busy(calendars, start, end) if calendars else []
    conn.execute(
        "DELETE FROM external_busy WHERE clinic_id=%s AND doctor_id=%s"
        " AND starts_at < %s AND ends_at > %s",
        (clinic_id, doctor_id, end, start),
    )
    stored = 0
    for s, e in intervals:
        s, e = max(s, start), min(e, end)
        if s >= e:
            continue
        conn.execute(
            "INSERT INTO external_busy (id,clinic_id,doctor_id,starts_at,ends_at,source_calendar)"
            " VALUES (%s,%s,%s,%s,%s,%s)",
            (uuid4(), clinic_id, doctor_id, s, e, "freebusy"),
        )
        stored += 1
    return stored
