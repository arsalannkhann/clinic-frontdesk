from datetime import UTC, datetime, time, timedelta


def candidate_slots(day, hours, duration, zone, busy, now, buffer=0, earliest=None):
    """Bookable starts on a duration grid from each opening time.

    Half-open intervals: a candidate holds [start, start + duration + buffer) against busy
    intervals, so a booking may start exactly where another hold ends. Only start + duration
    must fit inside the working window. Starts must be after now and at or after earliest
    (now + lead time) when given.
    """
    result = set()
    step = timedelta(minutes=duration)
    hold = step + timedelta(minutes=buffer)
    cutoff = now if earliest is None else max(now, earliest)
    for opening, closing in hours:
        cursor = datetime.combine(day, opening, zone)
        end = datetime.combine(day, closing, zone)
        while cursor + step <= end:
            if (
                cursor > now
                and cursor >= cutoff
                and not any(cursor < b_end and cursor + hold > b_start for b_start, b_end in busy)
            ):
                result.add(cursor)
            cursor += step
    return sorted(result)


def utc_now():
    return datetime.now(UTC)


def demo_hours():
    return time(9), time(17)
