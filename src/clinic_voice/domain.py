import re
import secrets
from datetime import UTC, datetime, time, timedelta

# Appointment statuses that hold a doctor's time. Migration 005's exclusion constraint
# repeats this list in SQL; keep them in step.
ACTIVE_STATUSES = ("pending_sync", "confirmed", "needs_review")

# Booking references: 8 Crockford base32 characters (no I, L, O, U), 40 random bits.
# Migration 012 repeats the alphabet and format in SQL; keep them in step.
REFERENCE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
REFERENCE_LENGTH = 8
REFERENCE_PATTERN = re.compile(r"[0-9A-HJKMNP-TV-Z]{8}")
_CROCKFORD_ALIASES = str.maketrans({"O": "0", "I": "1", "L": "1"})


def new_booking_reference() -> str:
    """A fresh reference from the CSPRNG. Uniqueness per clinic is enforced by the database;
    callers retry on collision."""
    return "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(REFERENCE_LENGTH))


def normalize_reference(value: str) -> str | None:
    """Canonical form of a spoken or typed reference, or None if it cannot be one.
    Case, spaces and hyphens are ignored; O reads as 0 and I/L as 1 (Crockford)."""
    compact = re.sub(r"[\s-]", "", value).upper().translate(_CROCKFORD_ALIASES)
    return compact if REFERENCE_PATTERN.fullmatch(compact) else None


def spoken_reference(reference: str) -> str:
    """Characters separated by spaces so text-to-speech reads them one by one."""
    return " ".join(reference)


def candidate_slots(day, hours, duration, zone, busy, now, buffer=0, earliest=None):
    """Bookable starts on a duration grid from each opening time.

    The grid steps in absolute time (UTC), so DST transitions neither invent nonexistent
    local times nor duplicate instants. Half-open intervals: a candidate holds
    [start, start + duration + buffer) against busy intervals, so a booking may start exactly
    where another hold ends. Only start + duration must fit inside the working window. Starts
    must be after now and at or after earliest (now + lead time) when given. Returns distinct
    instants in ascending order, expressed in the clinic zone.
    """
    result = set()
    step = timedelta(minutes=duration)
    hold = step + timedelta(minutes=buffer)
    cutoff = now if earliest is None else max(now, earliest)
    for opening, closing in hours:
        cursor = datetime.combine(day, opening, zone).astimezone(UTC)
        end = datetime.combine(day, closing, zone).astimezone(UTC)
        while cursor + step <= end:
            if (
                cursor > now
                and cursor >= cutoff
                and not any(cursor < b_end and cursor + hold > b_start for b_start, b_end in busy)
            ):
                result.add(cursor)
            cursor += step
    # Sort in UTC: same-tzinfo comparisons of zone-local datetimes ignore fold.
    return [instant.astimezone(zone) for instant in sorted(result)]


def utc_now():
    return datetime.now(UTC)


def demo_hours():
    return time(9), time(17)
