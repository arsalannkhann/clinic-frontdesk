"""Calendar provider port and an in-memory fake.

Errors carry only a short code, never provider response bodies (which may echo event data).
Google Calendar event ids may use base32hex characters (a-v and 0-9) and must be 5-1024
characters long; event_id_for stays inside that alphabet so the id can be sent to Google as is.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from threading import Lock
from typing import Literal, Protocol
from uuid import UUID


class ProviderError(Exception):
    """Base class. str(error) is the short code only."""

    def __init__(self, code: str = "provider_error"):
        super().__init__(code)
        self.code = code


class ProviderTimeout(ProviderError):
    """Outcome unknown: the write may or may not have happened. Recover by fetching the id."""

    def __init__(self, code: str = "timeout"):
        super().__init__(code)


class ProviderRetryable(ProviderError):
    """Definitely failed in a way worth retrying later (rate limit, 5xx)."""

    def __init__(self, code: str = "retryable", retry_after: float | None = None):
        super().__init__(code)
        self.retry_after = retry_after  # seconds, when the provider says how long to wait


class ProviderPermanent(ProviderError):
    """Retrying cannot help (bad request, missing calendar, revoked access)."""

    def __init__(self, code: str = "permanent"):
        super().__init__(code)


class EventAlreadyExists(ProviderError):
    def __init__(self, code: str = "already_exists"):
        super().__init__(code)


@dataclass(frozen=True)
class CalendarEvent:
    event_id: str
    start: datetime
    end: datetime
    summary: str
    description: str = ""
    status: Literal["confirmed", "cancelled"] = "confirmed"

    def __post_init__(self):
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("event times must be timezone-aware")
        if self.start >= self.end:
            raise ValueError("event start must be before end")
        if self.status not in ("confirmed", "cancelled"):
            raise ValueError("invalid event status")


class CalendarPort(Protocol):
    """Provider-neutral calendar operations.

    Adapter obligations: map a per-calendar free_busy failure to ProviderPermanent or
    ProviderRetryable, never to empty busy time (that would offer slots the calendar blocks);
    map a deleted event (for example HTTP 410) to the cancelled/None contract below. Callers
    must catch EventAlreadyExists before ProviderError, since it is a subclass.
    """

    def get_event(self, calendar_id: str, event_id: str) -> CalendarEvent | None:
        """None only if the event never existed; a cancelled event is returned with
        status 'cancelled' so callers can tell the two apart."""

    def insert_event(self, calendar_id: str, event: CalendarEvent) -> CalendarEvent:
        """Raises EventAlreadyExists if the id is taken (including by a cancelled event)."""

    def cancel_event(self, calendar_id: str, event_id: str) -> None:
        """Idempotent: a missing or already-cancelled event is not an error."""

    def free_busy(
        self, calendar_ids: list[str], start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime]]:
        """Busy intervals merged, sorted and clipped to [start, end)."""


def event_id_for(appointment_id: UUID, version: int) -> str:
    """Deterministic, opaque event id: 'cv' + 32 hex chars + 'v' + version. All characters
    are in Google's base32hex set (a-v, 0-9). The version (>= 1) lets a reschedule use a
    new id."""
    if version < 1:
        raise ValueError("version must be at least 1")
    return f"cv{appointment_id.hex}v{version}"


def event_summary(appointment_id: UUID) -> str:
    """Opaque title: a short booking reference, no patient or clinical data."""
    return "Clinic appointment " + appointment_id.hex[:8]


def merge_intervals(intervals, start, end):
    """Sort, clip to [start, end), drop empties and merge overlapping or touching ranges."""
    clipped = sorted(
        (max(s, start), min(e, end)) for s, e in intervals if s < end and e > start and s < e
    )
    merged: list[tuple[datetime, datetime]] = []
    for s, e in clipped:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


OPS = ("get_event", "insert_event", "cancel_event", "free_busy")


class FakeCalendar:
    """Deterministic in-memory calendar for tests. Thread-safe.

    Fault injection: fail_next(n, error_type, op) makes the next n calls of that operation
    raise error_type (checked first, before any other behaviour); timeout_after_write /
    timeout_before_write apply to the next insert only. `calls` logs every call in order as
    (op, calendar_id(s), event_id or None), including failing ones, so tests can assert
    ordering such as get_event before insert_event. `busy` maps calendar_id to extra busy
    intervals reported by free_busy alongside the confirmed events stored in that calendar.
    """

    def __init__(self):
        self._lock = Lock()
        self._events: dict[tuple[str, str], CalendarEvent] = {}
        self._inserts: dict[str, int] = {}
        self._failures: dict[str, list[type[ProviderError]]] = {op: [] for op in OPS}
        self.calls: list[tuple] = []
        self.timeout_after_write = False
        self.timeout_before_write = False
        self.busy: dict[str, list[tuple[datetime, datetime]]] = {}

    def fail_next(
        self, n: int, error_type: type[ProviderError] = ProviderRetryable, op: str = "insert_event"
    ):
        if op not in OPS:
            raise ValueError(f"unknown operation {op}")
        with self._lock:
            self._failures[op].extend([error_type] * n)

    def insert_count(self, event_id: str) -> int:
        """Number of successful inserts that stored this id, across all calendars (collisions
        and injected failures that stored nothing are not counted; timeout_after_write counts)."""
        with self._lock:
            return self._inserts.get(event_id, 0)

    def _enter(self, op, calendar, event_id=None):
        # Caller holds the lock.
        self.calls.append((op, calendar, event_id))
        if self._failures[op]:
            raise self._failures[op].pop(0)()

    def get_event(self, calendar_id, event_id):
        with self._lock:
            self._enter("get_event", calendar_id, event_id)
            return self._events.get((calendar_id, event_id))

    def insert_event(self, calendar_id, event):
        with self._lock:
            self._enter("insert_event", calendar_id, event.event_id)
            if self.timeout_before_write:
                self.timeout_before_write = False
                raise ProviderTimeout()
            if (calendar_id, event.event_id) in self._events:
                raise EventAlreadyExists()
            stored = replace(event, status="confirmed")
            self._events[(calendar_id, event.event_id)] = stored
            self._inserts[event.event_id] = self._inserts.get(event.event_id, 0) + 1
            if self.timeout_after_write:
                self.timeout_after_write = False
                raise ProviderTimeout()
            return stored

    def cancel_event(self, calendar_id, event_id):
        with self._lock:
            self._enter("cancel_event", calendar_id, event_id)
            event = self._events.get((calendar_id, event_id))
            if event is not None:
                self._events[(calendar_id, event_id)] = replace(event, status="cancelled")

    def free_busy(self, calendar_ids, start, end):
        with self._lock:
            self._enter("free_busy", tuple(calendar_ids))
            intervals = []
            for calendar_id in calendar_ids:
                intervals.extend(self.busy.get(calendar_id, []))
                intervals.extend(
                    (e.start, e.end)
                    for (cal, _), e in self._events.items()
                    if cal == calendar_id and e.status == "confirmed"
                )
            return merge_intervals(intervals, start, end)
