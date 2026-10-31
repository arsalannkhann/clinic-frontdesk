"""The ledger calendar: the platform database is the calendar of record (ADR 0010).

For clinics without an external calendar, a doctor's booking_calendar_id is
"ledger:<doctor uuid>" (migration 018 checks the doctor id). Appointments of such a doctor
confirm through the normal calendar.create job: the ledger accepts the insert of the
deterministic event id, and the job's fenced confirming transaction records it on the
appointment (calendar_event_id, calendar_id), exactly as for Google.

State lives only in the database, never in memory, so any worker or serverless instance
sees the same calendar:

- get_event(calendar, id) reads the appointment the id names (providers.calendar.event_id_for)
  and returns an event only if that appointment recorded this event id in this calendar:
  confirmed while the appointment is still at that version and not cancelled, cancelled
  otherwise. An insert that was never recorded (the confirming transaction rolled back) is
  absent, and the next attempt simply inserts again.
- insert_event returns the event as stored. It never raises EventAlreadyExists: an id is
  unique to one appointment version, so a repeated insert is the same event (idempotent).
- cancel_event succeeds (the appointment row is the record; nothing else to remove).
- free_busy is empty: the ledger's own busy time is the appointments table, which slot math
  and the exclusion constraint already use.

Reads use the worker's connection and commit at once, so the connection is idle again
before the handler continues (outbox.require_idle). LedgerCalendar refuses a calendar id
that is not a ledger id (ProviderPermanent 'not_ledger') rather than pretending to write it.

LedgerResolver is the worker's Ports.calendar_for: ledger-mapped doctors resolve to the
ledger; anything else goes to the wrapped resolver (Google) when one is configured, else
resolves to None (calendar_unmapped). Whatever the resolution, calls on a ledger calendar id
are always served by the ledger, so an event projected to the ledger is still verified and
withdrawn there after its doctor is remapped to Google, and vice versa.
"""

import re
from dataclasses import replace
from uuid import UUID

from clinic_voice.providers.calendar import CalendarEvent, ProviderPermanent

LEDGER_PREFIX = "ledger:"
EVENT_ID = re.compile(r"cv([0-9a-f]{32})v([1-9][0-9]{0,9})")
# No network I/O; declared for outbox.check_ports, which needs every adapter's bound.
LEDGER_TIMEOUT = 5.0


def is_ledger(calendar_id: str | None) -> bool:
    return bool(calendar_id) and calendar_id.startswith(LEDGER_PREFIX)


def ledger_calendar_id(doctor_id: UUID) -> str:
    return f"{LEDGER_PREFIX}{doctor_id}"


def _parse_event_id(event_id: str) -> tuple[UUID, int] | None:
    match = EVENT_ID.fullmatch(event_id)
    if match is None:
        return None
    return UUID(hex=match.group(1)), int(match.group(2))


class LedgerCalendar:
    """CalendarPort over the platform database (see the module docstring)."""

    timeout_seconds = LEDGER_TIMEOUT

    def __init__(self, conn):
        self._conn = conn

    def _check(self, calendar_id):
        if not is_ledger(calendar_id):
            raise ProviderPermanent("not_ledger")

    def get_event(self, calendar_id, event_id):
        self._check(calendar_id)
        parsed = _parse_event_id(event_id)
        if parsed is None:
            return None
        appointment_id, version = parsed
        try:
            row = self._conn.execute(
                """SELECT a.id, a.status, a.version, a.starts_at, a.ends_at FROM appointments a
                WHERE a.id=%s AND a.calendar_event_id=%s AND a.calendar_id=%s
                AND 'ledger:' || a.doctor_id::text = %s""",
                (appointment_id, event_id, calendar_id, calendar_id),
            ).fetchone()
        finally:
            self._conn.commit()  # Leave the connection idle for the handler.
        if row is None:
            return None
        live = row["version"] == version and row["status"] != "cancelled"
        return CalendarEvent(
            event_id,
            row["starts_at"],
            row["ends_at"],
            "",
            status="confirmed" if live else "cancelled",
        )

    def insert_event(self, calendar_id, event):
        self._check(calendar_id)
        if _parse_event_id(event.event_id) is None:
            raise ProviderPermanent("bad_event_id")
        return replace(event, status="confirmed")

    def cancel_event(self, calendar_id, event_id):
        self._check(calendar_id)

    def free_busy(self, calendar_ids, start, end):
        for calendar_id in calendar_ids:
            self._check(calendar_id)
        return []


class _Routed:
    """Calls on ledger calendar ids go to the ledger, others to `other` (or are refused with
    ProviderPermanent 'calendar_unmapped' when there is none)."""

    def __init__(self, ledger, other):
        self._ledger = ledger
        self._other = other

    def _for(self, calendar_id):
        if is_ledger(calendar_id):
            return self._ledger
        if self._other is None:
            raise ProviderPermanent("calendar_unmapped")
        return self._other

    def get_event(self, calendar_id, event_id):
        return self._for(calendar_id).get_event(calendar_id, event_id)

    def insert_event(self, calendar_id, event):
        return self._for(calendar_id).insert_event(calendar_id, event)

    def cancel_event(self, calendar_id, event_id):
        return self._for(calendar_id).cancel_event(calendar_id, event_id)

    def free_busy(self, calendar_ids, start, end):
        external = [c for c in calendar_ids if not is_ledger(c)]
        if not external:
            return []
        if self._other is None:
            raise ProviderPermanent("calendar_unmapped")
        return self._other.free_busy(external, start, end)


class LedgerResolver:
    """Ports.calendar_for: (conn, clinic_id, doctor_id, account_id) -> (port, account_id) or
    None. `inner` is the resolver for external calendars (GoogleCalendarResolver) or None.

    With account_id (an event written through that calendar account) the inner resolver
    decides. Otherwise a doctor whose booking calendar is a ledger id resolves to the ledger
    with no account; any other doctor goes to the inner resolver, or None without one."""

    def __init__(self, inner=None):
        self._inner = inner
        inner_timeout = getattr(inner, "timeout_seconds", None) if inner else None
        self.timeout_seconds = inner_timeout or LEDGER_TIMEOUT

    def __repr__(self):
        return f"LedgerResolver(inner={self._inner!r})"

    def __call__(self, conn, clinic_id, doctor_id, account_id=None):
        ledger = LedgerCalendar(conn)
        if account_id is None and doctor_id is not None:
            try:
                row = conn.execute(
                    "SELECT booking_calendar_id FROM doctors WHERE clinic_id=%s AND id=%s",
                    (clinic_id, doctor_id),
                ).fetchone()
            finally:
                conn.commit()
            if row is not None and is_ledger(row["booking_calendar_id"]):
                return _Routed(ledger, None), None
        if self._inner is None:
            return None
        resolved = self._inner(conn, clinic_id, doctor_id, account_id)
        if resolved is None:
            return None
        return _Routed(ledger, resolved[0]), resolved[1]

    # Optional resolver hooks (outbox.Ports): delegated to the external resolver.
    def mark_error(self, conn, clinic_id, account_id):
        mark = getattr(self._inner, "mark_error", None)
        if mark is not None:
            mark(conn, clinic_id, account_id)

    @property
    def revoke_grant(self):
        return getattr(self._inner, "revoke_grant", None)
