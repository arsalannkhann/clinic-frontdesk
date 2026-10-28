from datetime import UTC, date, datetime, time
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
