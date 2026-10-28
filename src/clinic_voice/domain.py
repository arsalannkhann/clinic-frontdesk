from datetime import UTC, datetime, time, timedelta


def candidate_slots(day, hours, duration, zone, busy, now):
    """Half-open intervals: adjacent bookings are permitted; past slots are excluded."""
    result = set()
    step = timedelta(minutes=duration)
    for opening, closing in hours:
        cursor = datetime.combine(day, opening, zone)
        end = datetime.combine(day, closing, zone)
        while cursor + step <= end:
            if cursor > now and not any(
                cursor < b_end and cursor + step > b_start for b_start, b_end in busy
            ):
                result.add(cursor)
            cursor += step
    return sorted(result)


def utc_now():
    return datetime.now(UTC)


def demo_hours():
    return time(9), time(17)
