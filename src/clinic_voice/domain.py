from datetime import UTC, datetime, time, timedelta

# Appointment statuses that hold a doctor's time. Migration 005's exclusion constraint
# repeats this list in SQL; keep them in step.
ACTIVE_STATUSES = ("pending_sync", "confirmed", "needs_review")


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
