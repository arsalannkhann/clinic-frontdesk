from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from clinic_voice.domain import candidate_slots


def test_partial_overlap_and_adjacent_boundaries():
    zone = ZoneInfo("Asia/Kolkata")
    day = date(2030, 1, 1)
    busy = [(datetime(2030, 1, 1, 9, 10, tzinfo=zone), datetime(2030, 1, 1, 9, 30, tzinfo=zone))]
    result = candidate_slots(
        day, [(time(9), time(10))], 15, zone, busy, datetime(2029, 1, 1, tzinfo=UTC)
    )
    assert [v.strftime("%H:%M") for v in result] == ["09:30", "09:45"]


def test_past_and_window_end_excluded():
    zone = ZoneInfo("Asia/Kolkata")
    result = candidate_slots(
        date(2030, 1, 1),
        [(time(9), time(10))],
        30,
        zone,
        [],
        datetime(2030, 1, 1, 9, 1, tzinfo=zone),
    )
    assert [v.strftime("%H:%M") for v in result] == ["09:30"]


def test_multiple_windows_are_deduplicated_and_sorted():
    zone = ZoneInfo("Asia/Kolkata")
    result = candidate_slots(
        date(2030, 1, 1),
        [(time(10), time(11)), (time(9), time(10)), (time(9), time(10))],
        60,
        zone,
        [],
        datetime(2029, 1, 1, tzinfo=UTC),
    )
    assert [v.strftime("%H:%M") for v in result] == ["09:00", "10:00"]


ZONE = ZoneInfo("Asia/Kolkata")
DAY = date(2030, 1, 1)
LONG_AGO = datetime(2029, 1, 1, tzinfo=UTC)


def at(hour, minute=0):
    return datetime(2030, 1, 1, hour, minute, tzinfo=ZONE)


def hhmm(values):
    return [v.strftime("%H:%M") for v in values]


def test_buffer_extends_candidate_hold_against_busy():
    # A 15-minute slot plus 10-minute buffer holds 25 minutes; 09:30 would run into 09:50.
    busy = [(at(9, 50), at(10))]
    result = candidate_slots(DAY, [(time(9), time(10))], 15, ZONE, busy, LONG_AGO, buffer=10)
    assert hhmm(result) == ["09:00", "09:15"]


def test_buffer_need_not_fit_inside_window():
    result = candidate_slots(DAY, [(time(9), time(10))], 30, ZONE, [], LONG_AGO, buffer=30)
    assert hhmm(result) == ["09:00", "09:30"]


def test_existing_hold_end_is_adjacent_boundary():
    # A prior booking held until 09:25 (its blocks_until); the grid start at 09:30 is allowed.
    busy = [(at(9), at(9, 25))]
    result = candidate_slots(DAY, [(time(9), time(10))], 15, ZONE, busy, LONG_AGO)
    assert hhmm(result) == ["09:30", "09:45"]
    busy = [(at(9), at(9, 30))]
    result = candidate_slots(DAY, [(time(9), time(10))], 15, ZONE, busy, LONG_AGO)
    assert hhmm(result) == ["09:30", "09:45"]
    busy = [(at(9), at(9, 31))]
    result = candidate_slots(DAY, [(time(9), time(10))], 15, ZONE, busy, LONG_AGO)
    assert hhmm(result) == ["09:45"]


def test_earliest_cutoff_is_inclusive_and_now_is_exclusive():
    now = at(8, 30)
    result = candidate_slots(
        DAY, [(time(9), time(11))], 30, ZONE, [], now, earliest=now + timedelta(minutes=60)
    )
    assert hhmm(result) == ["09:30", "10:00", "10:30"]
    # Zero lead time: a start equal to now is still excluded.
    result = candidate_slots(DAY, [(time(9), time(10))], 30, ZONE, [], at(9), earliest=at(9))
    assert hhmm(result) == ["09:30"]


def test_grid_stays_anchored_to_opening_time():
    # The lead-time cutoff does not shift the grid: 09:10 cutoff yields 09:15, not 09:10.
    result = candidate_slots(DAY, [(time(9), time(10))], 15, ZONE, [], LONG_AGO, earliest=at(9, 10))
    assert hhmm(result) == ["09:15", "09:30", "09:45"]


def test_exception_interval_blocks_like_busy():
    busy = [(at(12), at(13))]  # Break
    result = candidate_slots(DAY, [(time(11), time(14))], 30, ZONE, busy, LONG_AGO, buffer=15)
    assert hhmm(result) == ["11:00", "13:00", "13:30"]
