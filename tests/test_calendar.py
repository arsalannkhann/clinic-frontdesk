"""Calendar port, fake adapter, doctor calendar mapping and the external busy cache.

"Now" is pinned like tests/test_schedule.py so every date is deterministic.
"""

import re
from datetime import datetime, time, timedelta
from threading import Thread
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from clinic_voice import calendar_sync, service, staff
from clinic_voice.api import app
from clinic_voice.cli import DEMO_CLINIC_ID as CLINIC
from clinic_voice.cli import DOCTOR_ID, SERVICE_ID
from clinic_voice.config import get_settings
from clinic_voice.db import connect
from clinic_voice.providers.calendar import (
    CalendarEvent,
    EventAlreadyExists,
    FakeCalendar,
    ProviderError,
    ProviderPermanent,
    ProviderRetryable,
    ProviderTimeout,
    event_id_for,
    event_summary,
)
from clinic_voice.schemas import ReservationRequest

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2031, 3, 3, 8, 30, tzinfo=IST)
DAY = NOW.date() + timedelta(days=2)
PASSWORD = "synthetic-password-01"
CAL = "booking@example.test"
BUSY = "busy@example.test"


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setattr(staff, "SCRYPT_N", 2**10)
    monkeypatch.setattr(service, "utc_now", lambda: NOW)


@pytest.fixture
def client(database, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database)
    get_settings.cache_clear()
    with TestClient(app) as value:
        yield value
    get_settings.cache_clear()


def at(hour, minute=0, day=DAY):
    return datetime.combine(day, time(hour, minute), IST)


def headers(client, database, role="owner", clinic=CLINIC):
    email = f"{role}-{uuid4().hex[:8]}@example.test"
    with connect(database) as conn:
        staff.create_user(conn, clinic, email, role, PASSWORD)
    r = client.post("/v1/staff/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def event(event_id="evt00001", start=None, end=None, **kwargs):
    start = start or at(10)
    return CalendarEvent(event_id, start, end or start + timedelta(minutes=15), "s", **kwargs)


def free_starts(database):
    with connect(database) as conn:
        return {
            s.hour * 60 + s.minute for s in service.slots(conn, CLINIC, DOCTOR_ID, SERVICE_ID, DAY)
        }


def refresh(database, port, doctor=DOCTOR_ID, clinic=CLINIC, start=None, end=None):
    start = start or at(0)
    end = end or at(0, day=DAY + timedelta(days=1))
    with connect(database) as conn:
        return calendar_sync.refresh_external_busy(conn, port, clinic, doctor, start, end)


def map_doctor(database, booking=CAL, busy=(BUSY,), doctor=DOCTOR_ID):
    with connect(database) as conn:
        conn.execute(
            "UPDATE doctors SET booking_calendar_id=%s, busy_calendar_ids=%s WHERE id=%s",
            (booking, list(busy), doctor),
        )


def cached(database, doctor=DOCTOR_ID, clinic=CLINIC):
    with connect(database) as conn:
        rows = conn.execute(
            "SELECT starts_at, ends_at FROM external_busy WHERE clinic_id=%s AND doctor_id=%s"
            " ORDER BY starts_at",
            (clinic, doctor),
        ).fetchall()
    return [(r["starts_at"], r["ends_at"]) for r in rows]


def foreign_clinic(database):
    clinic, doctor, service_id = uuid4(), uuid4(), uuid4()
    with connect(database) as conn:
        conn.execute("INSERT INTO clinics (id,name) VALUES (%s,'Synthetic Other')", (clinic,))
        conn.execute(
            "INSERT INTO doctors (id,clinic_id,name) VALUES (%s,%s,'Other')", (doctor, clinic)
        )
        conn.execute(
            "INSERT INTO services (id,clinic_id,doctor_id,name,duration_minutes)"
            " VALUES (%s,%s,%s,'Other',15)",
            (service_id, clinic, doctor),
        )
        for weekday in range(7):
            conn.execute(
                "INSERT INTO working_hours VALUES (%s,%s,%s,'09:00','17:00')",
                (clinic, doctor, weekday),
            )
    return clinic, doctor, service_id


# FakeCalendar semantics


def test_insert_get_and_duplicate():
    fake = FakeCalendar()
    stored = fake.insert_event(CAL, event())
    assert stored.status == "confirmed" and fake.get_event(CAL, "evt00001") == stored
    assert fake.get_event(CAL, "missing") is None
    assert fake.get_event("other-calendar", "evt00001") is None
    with pytest.raises(EventAlreadyExists):
        fake.insert_event(CAL, event())
    assert fake.insert_count("evt00001") == 1


def test_cancel_is_idempotent_and_visible():
    fake = FakeCalendar()
    fake.cancel_event(CAL, "never-existed")  # not an error
    assert fake.get_event(CAL, "never-existed") is None
    fake.insert_event(CAL, event())
    fake.cancel_event(CAL, "evt00001")
    fake.cancel_event(CAL, "evt00001")
    assert fake.get_event(CAL, "evt00001").status == "cancelled"
    with pytest.raises(EventAlreadyExists):  # the id stays taken, as in Google
        fake.insert_event(CAL, event())


def test_timeout_after_write_stores_then_raises():
    fake = FakeCalendar()
    fake.timeout_after_write = True
    with pytest.raises(ProviderTimeout):
        fake.insert_event(CAL, event())
    assert fake.get_event(CAL, "evt00001") is not None and fake.insert_count("evt00001") == 1
    with pytest.raises(EventAlreadyExists):  # the flag applies to one insert only
        fake.insert_event(CAL, event())


def test_timeout_before_write_does_not_store():
    fake = FakeCalendar()
    fake.timeout_before_write = True
    with pytest.raises(ProviderTimeout):
        fake.insert_event(CAL, event())
    assert fake.get_event(CAL, "evt00001") is None and fake.insert_count("evt00001") == 0
    assert fake.insert_event(CAL, event()).event_id == "evt00001"


def test_fail_next_counts_and_error_type():
    fake = FakeCalendar()
    fake.fail_next(2, ProviderPermanent)
    for _ in range(2):
        with pytest.raises(ProviderPermanent):
            fake.insert_event(CAL, event())
    assert fake.get_event(CAL, "evt00001") is None
    fake.fail_next(1)
    with pytest.raises(ProviderRetryable):
        fake.insert_event(CAL, event())
    fake.insert_event(CAL, event())


def test_errors_carry_only_a_short_code():
    for error in (
        ProviderTimeout(),
        ProviderRetryable(),
        ProviderPermanent(),
        EventAlreadyExists(),
    ):
        assert isinstance(error, ProviderError) and str(error) == error.code
        assert len(error.code) < 30


def test_free_busy_merges_clips_and_includes_own_events():
    fake = FakeCalendar()
    fake.busy[BUSY] = [(at(9), at(10)), (at(9, 30), at(11)), (at(8), at(8, 30)), (at(12), at(13))]
    fake.insert_event(CAL, event("evt00001", at(11), at(11, 30)))  # touches 9-11 block
    fake.insert_event(CAL, event("evt00002", at(15), at(16)))
    fake.cancel_event(CAL, "evt00002")  # cancelled events are not busy
    fake.insert_event("unlisted", event("evt00003", at(14), at(15)))
    assert fake.free_busy([BUSY, CAL], at(9, 15), at(12, 30)) == [
        (at(9, 15), at(11, 30)),
        (at(12), at(12, 30)),
    ]
    assert fake.free_busy([BUSY], at(14), at(15)) == []
    assert fake.free_busy([], at(0), at(23)) == []


def test_fake_is_thread_safe_for_same_id():
    fake = FakeCalendar()
    outcomes = []

    def go():
        try:
            fake.insert_event(CAL, event())
            outcomes.append("ok")
        except EventAlreadyExists:
            outcomes.append("dup")

    threads = [Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(outcomes) == ["dup"] * 7 + ["ok"] and fake.insert_count("evt00001") == 1


# Ids and summaries


def test_event_id_format_is_google_safe_and_deterministic():
    appointment = uuid4()
    first = event_id_for(appointment, 1)
    assert first == "cv" + appointment.hex + "v1" == event_id_for(appointment, 1)
    assert re.fullmatch(r"[a-v0-9]{5,1024}", first)  # Google: base32hex a-v and 0-9, 5-1024
    assert event_id_for(appointment, 2) != first
    assert event_id_for(UUID(int=0), 12) == "cv" + "0" * 32 + "v12"


def test_event_summary_is_opaque():
    appointment = uuid4()
    summary = event_summary(appointment)
    assert summary == "Clinic appointment " + appointment.hex[:8]
    assert summary == event_summary(appointment)
    assert "@" not in summary and not re.search(r"\d{6,}", summary.replace(appointment.hex[:8], ""))


# Admin doctor calendar fields


def test_admin_doctor_calendar_fields_roundtrip(client, database):
    h = headers(client, database)
    url = f"/v1/admin/doctors/{DOCTOR_ID}"
    listed = {d["id"]: d for d in client.get("/v1/admin/doctors", headers=h).json()}
    assert listed[str(DOCTOR_ID)]["booking_calendar_id"] is None
    assert listed[str(DOCTOR_ID)]["busy_calendar_ids"] == []
    r = client.patch(
        url, headers=h, json={"booking_calendar_id": CAL, "busy_calendar_ids": [BUSY, "x"]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["booking_calendar_id"] == CAL and r.json()["busy_calendar_ids"] == [BUSY, "x"]
    listed = {d["id"]: d for d in client.get("/v1/admin/doctors", headers=h).json()}
    assert listed[str(DOCTOR_ID)]["busy_calendar_ids"] == [BUSY, "x"]
    r = client.patch(url, headers=h, json={"booking_calendar_id": None})
    assert r.status_code == 200 and r.json()["booking_calendar_id"] is None
    assert r.json()["busy_calendar_ids"] == [BUSY, "x"]
    assert (
        client.patch(url, headers=h, json={"busy_calendar_ids": []}).json()["busy_calendar_ids"]
        == []
    )
    created = client.post("/v1/admin/doctors", headers=h, json={"name": "New"}).json()
    assert created["booking_calendar_id"] is None and created["busy_calendar_ids"] == []


@pytest.mark.parametrize(
    "body",
    [
        {"booking_calendar_id": ""},
        {"booking_calendar_id": "x" * 256},
        {"booking_calendar_id": "a\nb"},
        {"booking_calendar_id": 5},
        {"busy_calendar_ids": ["ok"] * 11},
        {"busy_calendar_ids": [""]},
        {"busy_calendar_ids": ["x" * 256]},
        {"busy_calendar_ids": ["bad\x00id"]},
        {"busy_calendar_ids": [None]},
        {"busy_calendar_ids": None},
        {"busy_calendar_ids": "a@b"},
        {"name": None},
    ],
)
def test_admin_doctor_calendar_validation(client, database, body):
    h = headers(client, database)
    assert client.patch(f"/v1/admin/doctors/{DOCTOR_ID}", headers=h, json=body).status_code == 422


def test_admin_doctor_calendar_limits_accepted(client, database):
    h = headers(client, database)
    body = {"booking_calendar_id": "x" * 255, "busy_calendar_ids": ["y" * 255] * 10}
    assert client.patch(f"/v1/admin/doctors/{DOCTOR_ID}", headers=h, json=body).status_code == 200


def test_admin_doctor_calendar_roles_and_cross_tenant(client, database):
    other, other_doctor, _ = foreign_clinic(database)
    viewer = headers(client, database, "viewer")
    body = {"booking_calendar_id": CAL}
    assert (
        client.patch(f"/v1/admin/doctors/{DOCTOR_ID}", headers=viewer, json=body).status_code == 403
    )
    h = headers(client, database)
    assert (
        client.patch(f"/v1/admin/doctors/{other_doctor}", headers=h, json=body).status_code == 404
    )
    assert client.patch(f"/v1/admin/doctors/{uuid4()}", headers=h, json=body).status_code == 404
    with connect(database) as conn:
        row = conn.execute(
            "SELECT booking_calendar_id FROM doctors WHERE id=%s", (other_doctor,)
        ).fetchone()
    assert row["booking_calendar_id"] is None
    assert other_doctor not in {d["id"] for d in client.get("/v1/admin/doctors", headers=h).json()}


def test_database_constraints_on_calendar_ids(database):
    from psycopg.errors import CheckViolation

    for column, value in (
        ("booking_calendar_id", ""),
        ("busy_calendar_ids", ["a"] * 11),
        ("busy_calendar_ids", [""]),
        ("busy_calendar_ids", ["a", None]),
    ):
        with pytest.raises(CheckViolation), connect(database) as conn:
            conn.execute(f"UPDATE doctors SET {column}=%s WHERE id=%s", (value, DOCTOR_ID))  # noqa: S608


# refresh_external_busy


def test_refresh_stores_clipped_merged_intervals(database):
    map_doctor(database)
    fake = FakeCalendar()
    fake.busy[BUSY] = [(at(11), at(12)), (at(11, 30), at(12, 30)), (at(8), at(9, 30))]
    fake.insert_event(CAL, event("evt00001", at(14), at(15)))
    assert refresh(database, fake, start=at(9), end=at(16)) == 3
    assert cached(database) == [(at(9), at(9, 30)), (at(11), at(12, 30)), (at(14), at(15))]
    with connect(database) as conn:
        sources = conn.execute(
            "SELECT source_calendar, count(*) AS n FROM external_busy GROUP BY 1"
        ).fetchall()
    assert {r["source_calendar"]: r["n"] for r in sources} == {BUSY: 2, CAL: 1}
    with connect(database) as conn:
        row = conn.execute("SELECT fetched_at FROM external_busy LIMIT 1").fetchone()
    assert row["fetched_at"] is not None


def test_refresh_without_calendars_clears_window_and_skips_provider(database):
    class Explodes:
        def free_busy(self, *args):
            raise AssertionError("must not be called")

    with connect(database) as conn:
        conn.execute(
            "INSERT INTO external_busy (id,clinic_id,doctor_id,starts_at,ends_at,source_calendar)"
            " VALUES (%s,%s,%s,%s,%s,'x')",
            (uuid4(), CLINIC, DOCTOR_ID, at(10), at(11)),
        )
    assert refresh(database, Explodes()) == 0
    assert cached(database) == []


def test_refresh_replaces_only_its_window_doctor_and_clinic(database):
    map_doctor(database)
    other_clinic, other_doctor, _ = foreign_clinic(database)
    with connect(database) as conn:
        sibling = uuid4()
        conn.execute(
            "INSERT INTO doctors (id,clinic_id,name) VALUES (%s,%s,'Sib')", (sibling, CLINIC)
        )
        for clinic, doctor, start, end in (
            (CLINIC, DOCTOR_ID, at(10), at(11)),  # inside the window: replaced
            (
                CLINIC,
                DOCTOR_ID,
                at(10, day=DAY + timedelta(days=3)),
                at(11, day=DAY + timedelta(days=3)),
            ),
            (CLINIC, sibling, at(10), at(11)),
            (other_clinic, other_doctor, at(10), at(11)),
        ):
            conn.execute(
                "INSERT INTO external_busy (id,clinic_id,doctor_id,starts_at,ends_at,source_calendar)"
                " VALUES (%s,%s,%s,%s,%s,'old')",
                (uuid4(), clinic, doctor, start, end),
            )
    fake = FakeCalendar()
    fake.busy[BUSY] = [(at(13), at(14))]
    refresh(database, fake, start=at(9), end=at(17))
    assert cached(database) == [
        (at(13), at(14)),
        (at(10, day=DAY + timedelta(days=3)), at(11, day=DAY + timedelta(days=3))),
    ]
    assert cached(database, sibling) == [(at(10), at(11))]
    assert cached(database, other_doctor, other_clinic) == [(at(10), at(11))]


def test_refresh_unknown_or_foreign_doctor_is_404_and_writes_nothing(database):
    other_clinic, other_doctor, _ = foreign_clinic(database)
    fake = FakeCalendar()
    fake.busy[BUSY] = [(at(10), at(11))]
    for clinic, doctor in ((CLINIC, other_doctor), (CLINIC, uuid4()), (other_clinic, DOCTOR_ID)):
        with pytest.raises(HTTPException) as error:
            refresh(database, fake, doctor=doctor, clinic=clinic)
        assert error.value.status_code == 404
    assert cached(database, other_doctor, other_clinic) == []


def test_provider_error_leaves_cache_untouched(database):
    map_doctor(database)
    fake = FakeCalendar()
    fake.busy[BUSY] = [(at(10), at(11))]
    refresh(database, fake)
    before = cached(database)

    class Failing:
        def free_busy(self, *args):
            raise ProviderRetryable()

    with pytest.raises(ProviderRetryable):
        refresh(database, Failing())
    assert cached(database) == before == [(at(10), at(11))]


def test_refresh_rolls_back_if_insert_fails(database):
    map_doctor(database)
    fake = FakeCalendar()
    fake.busy[BUSY] = [(at(10), at(11))]
    refresh(database, fake)

    class Bad:
        def free_busy(self, *args):
            return [(at(12), at(13)), ("not", "datetimes")]

    with pytest.raises(Exception):  # noqa: B017
        refresh(database, Bad())
    assert cached(database) == [(at(10), at(11))]


# Effect on slots and reservations


def test_cached_busy_removes_slots_and_blocks_reserve(database):
    assert 10 * 60 in free_starts(database)
    map_doctor(database)
    fake = FakeCalendar()
    fake.busy[BUSY] = [(at(10), at(10, 30))]
    refresh(database, fake)
    starts = free_starts(database)
    assert 10 * 60 not in starts and 10 * 60 + 15 not in starts
    assert 9 * 60 + 45 in starts and 10 * 60 + 30 in starts  # half-open edges stay bookable
    request = ReservationRequest(doctor_id=DOCTOR_ID, service_id=SERVICE_ID, starts_at=at(10))
    with pytest.raises(HTTPException) as error:
        service.reserve(database, CLINIC, request, uuid4().hex)
    assert error.value.status_code == 409
    ok = ReservationRequest(doctor_id=DOCTOR_ID, service_id=SERVICE_ID, starts_at=at(10, 30))
    assert service.reserve(database, CLINIC, ok, uuid4().hex)["status"] == "pending_sync"


def test_cached_busy_applies_through_check_slots_route(client, database):
    from clinic_voice import credentials

    with connect(database) as conn:
        _, token = credentials.issue(conn, CLINIC, "calendar-test")
    body = {"doctor_id": str(DOCTOR_ID), "service_id": str(SERVICE_ID), "day": DAY.isoformat()}
    auth = {"Authorization": f"Bearer {token}"}
    first = client.post("/v1/tools/check-slots", headers=auth, json=body).json()["slots"]
    assert first and datetime.fromisoformat(first[0]) == at(9)
    map_doctor(database)
    fake = FakeCalendar()
    fake.busy[BUSY] = [(at(9), at(12))]
    refresh(database, fake)
    after = client.post("/v1/tools/check-slots", headers=auth, json=body).json()["slots"]
    assert datetime.fromisoformat(after[0]) == at(12)


def test_busy_interval_starting_before_midnight_blocks_next_day_morning(database):
    map_doctor(database)
    fake = FakeCalendar()
    fake.busy[BUSY] = [(at(22, day=DAY - timedelta(days=1)), at(9, 30))]
    refresh(database, fake, start=at(0, day=DAY - timedelta(days=1)), end=at(23))
    assert 9 * 60 not in free_starts(database) and 9 * 60 + 30 in free_starts(database)


def test_other_clinics_external_busy_does_not_affect_slots(database):
    other_clinic, other_doctor, _ = foreign_clinic(database)
    before = free_starts(database)
    with connect(database) as conn:
        for clinic, doctor in ((other_clinic, other_doctor), (CLINIC, uuid4())):
            if doctor != other_doctor:
                conn.execute(
                    "INSERT INTO doctors (id,clinic_id,name) VALUES (%s,%s,'Sib')", (doctor, CLINIC)
                )
            conn.execute(
                "INSERT INTO external_busy (id,clinic_id,doctor_id,starts_at,ends_at,source_calendar)"
                " VALUES (%s,%s,%s,%s,%s,'x')",
                (uuid4(), clinic, doctor, at(0), at(23)),
            )
    assert free_starts(database) == before
    with connect(database) as conn:
        assert (
            service.slots(conn, other_clinic, other_doctor, _service(database, other_doctor), DAY)
            == []
        )


def _service(database, doctor):
    with connect(database) as conn:
        return conn.execute("SELECT id FROM services WHERE doctor_id=%s", (doctor,)).fetchone()[
            "id"
        ]


def test_external_busy_requires_composite_doctor_fk(database):
    from psycopg.errors import ForeignKeyViolation

    other_clinic, other_doctor, _ = foreign_clinic(database)
    with pytest.raises(ForeignKeyViolation), connect(database) as conn:
        conn.execute(
            "INSERT INTO external_busy (id,clinic_id,doctor_id,starts_at,ends_at,source_calendar)"
            " VALUES (%s,%s,%s,%s,%s,'x')",
            (uuid4(), CLINIC, other_doctor, at(10), at(11)),
        )


def test_refresh_refuses_connection_with_pending_work(database):
    map_doctor(database)
    fake = FakeCalendar()
    with connect(database) as conn:
        conn.execute(
            "INSERT INTO external_busy (id,clinic_id,doctor_id,starts_at,ends_at,source_calendar)"
            " VALUES (%s,%s,%s,%s,%s,'pending')",
            (uuid4(), CLINIC, DOCTOR_ID, at(10), at(11)),
        )
        with pytest.raises(RuntimeError):
            calendar_sync.refresh_external_busy(conn, fake, CLINIC, DOCTOR_ID, at(0), at(23))
        conn.rollback()  # the caller's write was never committed by the failed call
    assert cached(database) == []


def test_concurrent_refreshes_leave_no_duplicates(database):
    map_doctor(database)
    fake = FakeCalendar()
    fake.busy[BUSY] = [(at(10), at(11)), (at(13), at(14))]
    errors = []

    def go():
        try:
            refresh(database, fake)
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    threads = [Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert cached(database) == [(at(10), at(11)), (at(13), at(14))]
