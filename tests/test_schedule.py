"""Schedule exceptions, lead time and buffers against PostgreSQL.

"Now" is pinned to a fixed instant far from the host clock so every date is deterministic.
"""

from datetime import UTC, datetime, time, timedelta
from functools import partial
from threading import Barrier, Thread
from time import monotonic, sleep
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from psycopg.errors import CheckViolation, ExclusionViolation

from clinic_voice import admin, credentials, schedule_exceptions, service, staff
from clinic_voice.api import app
from clinic_voice.cli import DEMO_CLINIC_ID as CLINIC
from clinic_voice.cli import DOCTOR_ID, SERVICE_ID
from clinic_voice.config import get_settings
from clinic_voice.db import connect
from clinic_voice.domain import ACTIVE_STATUSES
from clinic_voice.locks import lock_doctor_set
from clinic_voice.schemas import HoursWindow, ReservationRequest

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2031, 3, 3, 8, 30, tzinfo=IST)  # Seeded hours are 09:00-17:00 every day.
TODAY = NOW.date()
DAY = TODAY + timedelta(days=2)
PASSWORD = "synthetic-password-01"
NEW_YORK = ZoneInfo("America/New_York")


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


def staff_actor(database, clinic=CLINIC):
    with connect(database) as conn:
        user = staff.create_user(
            conn, clinic, f"a-{uuid4().hex[:8]}@example.test", "admin", PASSWORD
        )
    return {"clinic_id": clinic, "id": user["id"]}


def set_buffer(database, minutes, service_id=SERVICE_ID):
    with connect(database) as conn:
        conn.execute(
            "UPDATE services SET buffer_after_minutes=%s WHERE id=%s", (minutes, service_id)
        )


def set_lead(database, minutes):
    with connect(database) as conn:
        conn.execute("UPDATE clinics SET lead_time_minutes=%s WHERE id=%s", (minutes, CLINIC))


def free(database, day=DAY, doctor=DOCTOR_ID, service_id=SERVICE_ID):
    with connect(database) as conn:
        return service.slots(conn, CLINIC, doctor, service_id, day)


def book(database, starts_at, doctor=DOCTOR_ID, service_id=SERVICE_ID):
    request = ReservationRequest(doctor_id=doctor, service_id=service_id, starts_at=starts_at)
    return service.reserve(database, CLINIC, request, uuid4().hex)


def rejected(database, starts_at, **kwargs):
    with pytest.raises(HTTPException) as error:
        book(database, starts_at, **kwargs)
    return error.value.status_code


def second_doctor(database):
    doctor, service_id = uuid4(), uuid4()
    with connect(database) as conn:
        conn.execute(
            "INSERT INTO doctors (id,clinic_id,name) VALUES (%s,%s,'Second')", (doctor, CLINIC)
        )
        conn.execute(
            "INSERT INTO services (id,clinic_id,doctor_id,name,duration_minutes)"
            " VALUES (%s,%s,%s,'Second',15)",
            (service_id, CLINIC, doctor),
        )
        for weekday in range(7):
            conn.execute(
                "INSERT INTO working_hours VALUES (%s,%s,%s,'09:00','17:00')",
                (CLINIC, doctor, weekday),
            )
    return doctor, service_id


def foreign_clinic(database):
    clinic, doctor = uuid4(), uuid4()
    with connect(database) as conn:
        conn.execute("INSERT INTO clinics (id,name) VALUES (%s,'Synthetic Other')", (clinic,))
        conn.execute(
            "INSERT INTO doctors (id,clinic_id,name) VALUES (%s,%s,'Other')", (doctor, clinic)
        )
    return clinic, doctor


def exception_body(starts_at, ends_at, doctor=DOCTOR_ID, kind="leave", **extra):
    body = {"kind": kind, "starts_at": starts_at.isoformat(), "ends_at": ends_at.isoformat()}
    if doctor is not None:
        body["doctor_id"] = str(doctor)
    return body | extra


def post_exception(client, h, *args, **kwargs):
    return client.post("/v1/admin/exceptions", headers=h, json=exception_body(*args, **kwargs))


def blocks_until(database, appointment_id):
    with connect(database) as conn:
        return conn.execute(
            "SELECT blocks_until FROM appointments WHERE id=%s", (appointment_id,)
        ).fetchone()["blocks_until"]


# Buffers


def test_buffer_between_bookings(database):
    set_buffer(database, 15)
    booked = book(database, at(10))
    assert blocks_until(database, booked["appointment_id"]) == at(10, 30)
    assert booked["ends_at"] == at(10, 15)
    available = free(database)
    # 09:45 would hold until 10:15 with its own buffer; 10:15 starts inside the 10:00 hold.
    assert at(9, 30) in available and at(9, 45) not in available
    assert at(10, 15) not in available and at(10, 30) in available
    assert rejected(database, at(10, 15)) == 409
    assert rejected(database, at(9, 45)) == 409
    # Adjacency exactly at blocks_until is allowed.
    assert book(database, at(10, 30))["status"] == "pending_sync"


def test_buffer_snapshot_survives_service_edit(client, database):
    set_buffer(database, 15)
    booked = book(database, at(10))
    h = headers(client, database, "admin")
    r = client.patch(
        f"/v1/admin/services/{SERVICE_ID}", headers=h, json={"buffer_after_minutes": 0}
    )
    assert r.status_code == 200
    assert blocks_until(database, booked["appointment_id"]) == at(10, 30)
    assert at(10, 15) not in free(database)
    assert rejected(database, at(10, 15)) == 409
    r = client.patch(
        f"/v1/admin/services/{SERVICE_ID}", headers=h, json={"buffer_after_minutes": 60}
    )
    assert r.status_code == 200
    assert blocks_until(database, booked["appointment_id"]) == at(10, 30)


def test_database_exclusion_honours_blocks_until(database):
    set_buffer(database, 15)
    book(database, at(10))
    columns = (
        "INSERT INTO appointments (id,clinic_id,doctor_id,service_id,starts_at,ends_at,"
        "blocks_until,status,idempotency_key,request_hash)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,'pending_sync',%s,'synthetic')"
    )

    def insert(start, end, hold):
        with connect(database) as conn:
            conn.execute(
                columns,
                (uuid4(), CLINIC, DOCTOR_ID, SERVICE_ID, start, end, hold, uuid4().hex),
            )

    # Starts after the 10:15 end but inside the buffer held until 10:30.
    with pytest.raises(ExclusionViolation):
        insert(at(10, 20), at(10, 25), at(10, 25))
    insert(at(10, 30), at(10, 45), at(10, 45))  # Adjacent to blocks_until.
    # A new hold whose buffer runs into an existing appointment is also rejected.
    with pytest.raises(ExclusionViolation):
        insert(at(9, 30), at(9, 45), at(10, 5))
    with pytest.raises(CheckViolation):
        insert(at(12), at(12, 15), at(12, 10))


# Lead time and horizon


def test_lead_time_excludes_near_slots(database):
    today = free(database, day=TODAY)
    assert today[0] == at(9, 30, TODAY)  # 08:30 + 60 minute default lead time
    set_lead(database, 180)
    today = free(database, day=TODAY)
    assert today[0] == at(11, 30, TODAY)
    assert rejected(database, at(11, 15, TODAY)) == 409
    assert book(database, at(11, 30, TODAY))["status"] == "pending_sync"
    set_lead(database, 0)
    assert free(database, day=TODAY)[0] == at(9, 0, TODAY)


def test_lead_time_spanning_days(database):
    set_lead(database, 2 * 24 * 60)  # Two days: earliest is DAY at 08:30.
    assert free(database, day=TODAY) == []
    assert free(database, day=TODAY + timedelta(days=1)) == []
    assert free(database, day=DAY)[0] == at(9)


def test_horizon(database):
    assert free(database, day=TODAY + timedelta(days=90))
    for day in (TODAY + timedelta(days=91), TODAY - timedelta(days=1)):
        with connect(database) as conn, pytest.raises(HTTPException) as error:
            service.slots(conn, CLINIC, DOCTOR_ID, SERVICE_ID, day)
        assert error.value.status_code == 422
    assert rejected(database, at(10, day=TODAY + timedelta(days=91))) == 422


# Exceptions in slot math


def test_clinic_wide_holiday_removes_all_slots(client, database):
    other_doctor, other_service = second_doctor(database)
    h = headers(client, database)
    r = post_exception(
        client, h, at(0), at(0, day=DAY + timedelta(days=1)), doctor=None, kind="holiday"
    )
    assert r.status_code == 201, r.text
    assert r.json()["doctor_id"] is None and r.json()["conflicting_appointment_ids"] == []
    assert free(database) == []
    assert free(database, doctor=other_doctor, service_id=other_service) == []
    assert rejected(database, at(10)) == 409
    assert rejected(database, at(10), doctor=other_doctor, service_id=other_service) == 409
    assert free(database, day=DAY + timedelta(days=1))


def test_doctor_leave_affects_only_that_doctor(client, database):
    other_doctor, other_service = second_doctor(database)
    h = headers(client, database)
    assert post_exception(client, h, at(0), at(23, 59)).status_code == 201
    assert free(database) == []
    assert rejected(database, at(10)) == 409
    assert len(free(database, doctor=other_doctor, service_id=other_service)) == 32
    assert book(database, at(10), doctor=other_doctor, service_id=other_service)


def test_break_mid_day(client, database):
    h = headers(client, database)
    assert post_exception(client, h, at(13), at(14), kind="break", label="Lunch").status_code == 201
    available = free(database)
    assert at(12, 45) in available and at(14) in available
    assert not [s for s in available if at(13) <= s < at(14)]
    assert rejected(database, at(13, 30)) == 409
    # With a buffer, the slot whose hold would run into the break also disappears.
    set_buffer(database, 10)
    assert at(12, 45) not in free(database) and at(12, 30) in free(database)


def test_exception_spanning_midnight_and_offsets(client, database):
    h = headers(client, database)
    # Expressed in UTC: 16:00 DAY IST through 10:00 the next day IST.
    start = at(16).astimezone(ZoneInfo("UTC"))
    end = at(10, day=DAY + timedelta(days=1)).astimezone(ZoneInfo("UTC"))
    assert post_exception(client, h, start, end).status_code == 201
    assert free(database)[-1] == at(15, 45)
    assert free(database, day=DAY + timedelta(days=1))[0] == at(10, day=DAY + timedelta(days=1))


def test_conflicting_appointments_reported_not_cancelled(client, database):
    other_doctor, other_service = second_doctor(database)
    set_buffer(database, 15)
    mine = book(database, at(10))["appointment_id"]  # Held until 10:30
    theirs = book(database, at(10), doctor=other_doctor, service_id=other_service)
    theirs = theirs["appointment_id"]
    h = headers(client, database)
    # Overlaps only the buffer.
    r = post_exception(client, h, at(10, 20), at(11))
    assert r.status_code == 201 and r.json()["conflicting_appointment_ids"] == [str(mine)]
    r = post_exception(client, h, at(10, 30), at(11))  # Starts at blocks_until: no conflict.
    assert r.json()["conflicting_appointment_ids"] == [] and r.json()["conflict_count"] == 0
    r = post_exception(client, h, at(9), at(12), doctor=None, kind="holiday")
    assert set(r.json()["conflicting_appointment_ids"]) == {str(mine), str(theirs)}
    assert r.json()["conflict_count"] == 2
    with connect(database) as conn:
        statuses = conn.execute("SELECT status FROM appointments").fetchall()
    assert {s["status"] for s in statuses} == {"pending_sync"}


# Admin API


def test_exception_role_matrix(client, database):
    viewer = headers(client, database, "viewer")
    admin_h = headers(client, database, "admin")
    owner = headers(client, database, "owner")
    for role in (viewer, admin_h, owner):
        assert client.get("/v1/admin/exceptions", headers=role).status_code == 200
    assert post_exception(client, viewer, at(13), at(14)).status_code == 403
    created = []
    for role in (admin_h, owner):
        r = post_exception(client, role, at(13), at(14))
        assert r.status_code == 201
        created.append(r.json()["id"])
    for exception_id in created:
        url = f"/v1/admin/exceptions/{exception_id}"
        assert client.delete(url, headers=viewer).status_code == 403
    assert client.delete(f"/v1/admin/exceptions/{created[0]}", headers=admin_h).status_code == 204
    assert client.delete(f"/v1/admin/exceptions/{created[1]}", headers=owner).status_code == 204
    assert client.get("/v1/admin/exceptions", headers=viewer).json() == []
    assert client.get("/v1/admin/exceptions").status_code == 401
    with connect(database) as conn:
        _, token = credentials.issue(conn, CLINIC, "synthetic")
    tool = {"Authorization": "Bearer " + token}
    assert client.get("/v1/admin/exceptions", headers=tool).status_code == 401
    assert post_exception(client, tool, at(13), at(14)).status_code == 401


def test_exception_cross_tenant_is_404(client, database):
    other, other_doctor = foreign_clinic(database)
    h = headers(client, database)
    b = headers(client, database, clinic=other)
    for doctor in (other_doctor, uuid4()):
        assert post_exception(client, h, at(13), at(14), doctor=doctor).status_code == 404
    theirs = post_exception(client, b, at(0), at(23, 59), doctor=None, kind="holiday")
    assert theirs.status_code == 201
    theirs = theirs.json()["id"]
    assert client.delete(f"/v1/admin/exceptions/{theirs}", headers=h).status_code == 404
    assert client.delete(f"/v1/admin/exceptions/{uuid4()}", headers=h).status_code == 404
    assert client.get("/v1/admin/exceptions", headers=h).json() == []
    assert [e["id"] for e in client.get("/v1/admin/exceptions", headers=b).json()] == [theirs]
    # The other clinic's holiday does not touch this clinic's slots.
    assert len(free(database)) == 32
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) AS n FROM schedule_exceptions").fetchone()["n"] == 1


def test_exception_list_range_and_audit(client, database):
    h = headers(client, database)
    early = post_exception(client, h, at(9), at(10)).json()["id"]
    late = post_exception(client, h, at(15), at(16), doctor=None, kind="break").json()["id"]
    listed = client.get("/v1/admin/exceptions", headers=h).json()
    assert [e["id"] for e in listed] == [early, late]
    assert listed[0]["label"] == "" and listed[0]["doctor_id"] == str(DOCTOR_ID)

    def window(start, end):
        params = {"from": start.isoformat(), "to": end.isoformat()}
        r = client.get("/v1/admin/exceptions", headers=h, params=params)
        assert r.status_code == 200, r.text
        return [e["id"] for e in r.json()]

    assert window(at(10), at(15)) == []  # Half-open on both sides.
    assert window(at(9, 59), at(15, 1)) == [early, late]
    r = client.get("/v1/admin/exceptions", headers=h, params={"from": at(12).isoformat()})
    assert [e["id"] for e in r.json()] == [late]
    assert client.delete(f"/v1/admin/exceptions/{early}", headers=h).status_code == 204
    assert client.delete(f"/v1/admin/exceptions/{early}", headers=h).status_code == 404
    with connect(database) as conn:
        rows = conn.execute(
            "SELECT action,target_id FROM audit_events WHERE action LIKE 'exception.%%'"
        ).fetchall()
    assert sorted((r["action"], str(r["target_id"])) for r in rows) == sorted(
        [("exception.created", early), ("exception.created", late), ("exception.deleted", early)]
    )


@pytest.mark.parametrize(
    "body",
    [
        exception_body(at(14), at(14)),
        exception_body(at(14), at(13)),
        exception_body(at(0), at(0) + timedelta(days=366, seconds=1)),
        exception_body(at(13), at(14), kind="vacation"),
        exception_body(at(13), at(14), label="x" * 81),
        exception_body(at(13), at(14), label="Lunch\n"),
        exception_body(at(13), at(14), label="Lu\x00nch"),
        exception_body(at(13), at(14), label="Lu\x1bnch"),
        exception_body(at(13), at(14), label="Lunch\x7f"),
        exception_body(at(13), at(14), label="\tLunch"),
        exception_body(at(13), at(14), clinic_id=str(CLINIC)),
        {"kind": "leave", "starts_at": "2031-03-05T13:00:00", "ends_at": "2031-03-05T14:00:00"},
        {"kind": "leave", "starts_at": at(13).isoformat()},
    ],
)
def test_exception_validation(client, database, body):
    h = headers(client, database)
    assert client.post("/v1/admin/exceptions", headers=h, json=body).status_code == 422
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) AS n FROM schedule_exceptions").fetchone()["n"] == 0


def test_exception_accepts_366_days_and_strips_label(client, database):
    h = headers(client, database)
    r = post_exception(client, h, at(0), at(0) + timedelta(days=366), label="  Renovation  ")
    assert r.status_code == 201 and r.json()["label"] == "Renovation"


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2031-03-05T10:00:00"},
        {"to": "not-a-date"},
        {"from": "2031-03-05T10:00:00+05:30", "to": "2031-03-05T10:00:00+05:30"},
        {"limit": "0"},
        {"limit": "1001"},
    ],
)
def test_exception_list_rejects_bad_range(client, database, params):
    h = headers(client, database, "viewer")
    assert client.get("/v1/admin/exceptions", headers=h, params=params).status_code == 422


def test_exception_list_limit(client, database):
    h = headers(client, database, "viewer")
    owner = headers(client, database)
    for hour in (9, 10, 11):
        assert post_exception(client, owner, at(hour), at(hour, 30)).status_code == 201
    assert len(client.get("/v1/admin/exceptions", headers=h).json()) == 3
    r = client.get("/v1/admin/exceptions", headers=h, params={"limit": 2})
    assert [e["starts_at"] for e in r.json()] == [
        e["starts_at"] for e in client.get("/v1/admin/exceptions", headers=h).json()[:2]
    ]


def test_conflict_ids_capped_with_true_count(client, database):
    with connect(database) as conn:
        for index in range(105):
            start = at(9, day=DAY) + timedelta(days=index // 30, minutes=15 * (index % 30))
            conn.execute(
                "INSERT INTO appointments (id,clinic_id,doctor_id,service_id,starts_at,ends_at,"
                "blocks_until,status,idempotency_key,request_hash)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,'confirmed',%s,'synthetic')",
                (
                    uuid4(),
                    CLINIC,
                    DOCTOR_ID,
                    SERVICE_ID,
                    start,
                    start + timedelta(minutes=15),
                    start + timedelta(minutes=15),
                    uuid4().hex,
                ),
            )
    h = headers(client, database)
    r = post_exception(client, h, at(0), at(0, day=DAY + timedelta(days=5)))
    assert r.status_code == 201
    assert len(r.json()["conflicting_appointment_ids"]) == 100
    assert r.json()["conflict_count"] == 105


def test_delete_exception_restores_slots(client, database):
    h = headers(client, database)
    before = free(database)
    created = post_exception(client, h, at(9), at(17), doctor=None, kind="holiday").json()
    assert free(database) == []
    assert client.delete(f"/v1/admin/exceptions/{created['id']}", headers=h).status_code == 204
    assert free(database) == before
    assert book(database, at(10))["status"] == "pending_sync"


def test_cancelled_appointments_do_not_block(client, database):
    set_buffer(database, 15)
    booked = book(database, at(10))["appointment_id"]
    with connect(database) as conn:
        conn.execute("UPDATE appointments SET status='cancelled' WHERE id=%s", (booked,))
    available = free(database)
    assert at(9, 45) in available and at(10) in available and at(10, 15) in available
    h = headers(client, database)
    r = post_exception(client, h, at(10), at(11))
    assert r.json()["conflicting_appointment_ids"] == [] and r.json()["conflict_count"] == 0
    with connect(database) as conn:
        conn.execute("DELETE FROM schedule_exceptions")
    assert book(database, at(10))["status"] == "pending_sync"


def test_cross_midnight_buffer_hits_next_day_exception(client, database):
    set_buffer(database, 30)
    with connect(database) as conn:
        conn.execute(
            "UPDATE working_hours SET opens='23:00', closes='23:59'"
            " WHERE doctor_id=%s AND weekday=%s",
            (DOCTOR_ID, DAY.weekday()),
        )
    assert free(database) == [at(23), at(23, 15), at(23, 30)]
    h = headers(client, database)
    next_day = DAY + timedelta(days=1)
    assert post_exception(client, h, at(0, day=next_day), at(1, day=next_day)).status_code == 201
    # 23:30 holds until 00:15 the next day; 23:15 holds until exactly 00:00 (adjacent).
    assert free(database) == [at(23), at(23, 15)]
    assert rejected(database, at(23, 30)) == 409


# Non-IST clinics and DST


def new_york_clinic(monkeypatch, database, now, duration=15):
    monkeypatch.setattr(service, "utc_now", lambda: now)
    with connect(database) as conn:
        conn.execute("UPDATE clinics SET timezone='America/New_York' WHERE id=%s", (CLINIC,))
        conn.execute("UPDATE services SET duration_minutes=%s WHERE id=%s", (duration, SERVICE_ID))


def test_non_ist_clinic_uses_its_own_zone(monkeypatch, client, database):
    new_york_clinic(monkeypatch, database, datetime(2030, 6, 3, 12, tzinfo=NEW_YORK))
    day = datetime(2030, 6, 5).date()
    available = free(database, day=day)
    assert len(available) == 32
    assert available[0].astimezone(UTC) == datetime(2030, 6, 5, 13, tzinfo=UTC)  # 09:00 EDT
    # Today in New York (lead time from 12:00 local) and the horizon are judged in that zone.
    assert free(database, day=datetime(2030, 6, 3).date())[0] == datetime(
        2030, 6, 3, 13, tzinfo=NEW_YORK
    )
    h = headers(client, database)
    start = datetime(2030, 6, 5, 9, tzinfo=NEW_YORK)
    r = post_exception(client, h, start, start + timedelta(hours=1), doctor=None, kind="break")
    assert r.status_code == 201
    assert free(database, day=day)[0] == datetime(2030, 6, 5, 10, tzinfo=NEW_YORK)
    assert book(database, datetime(2030, 6, 5, 14, tzinfo=UTC))["status"] == "pending_sync"


def test_reserve_rejects_nonexistent_local_time(monkeypatch, database):
    new_york_clinic(monkeypatch, database, datetime(2030, 3, 8, 12, tzinfo=NEW_YORK), 45)
    day = datetime(2030, 3, 10).date()  # Spring forward: 02:00-03:00 does not exist.
    with connect(database) as conn:
        conn.execute(
            "UPDATE working_hours SET opens='01:00', closes='04:00'"
            " WHERE doctor_id=%s AND weekday=%s",
            (DOCTOR_ID, day.weekday()),
        )
    available = free(database, day=day)
    assert [v.astimezone(UTC).strftime("%H:%M") for v in available] == ["06:00", "06:45"]
    # 02:30 local never happens; wall-clock stepping would have offered it (and 03:15).
    assert rejected(database, datetime(2030, 3, 10, 2, 30, tzinfo=NEW_YORK)) == 409
    assert rejected(database, datetime(2030, 3, 10, 3, 15, tzinfo=NEW_YORK)) == 409
    booked = book(database, datetime(2030, 3, 10, 6, 45, tzinfo=UTC))
    assert booked["ends_at"] == datetime(2030, 3, 10, 7, 30, tzinfo=UTC)


# Serialization with reservations


class Worker(Thread):
    """Runs call() and re-raises its exception in the test thread."""

    def __init__(self, call):
        super().__init__(daemon=True)
        self.call, self.value, self.error = call, None, None

    def run(self):
        try:
            self.value = self.call()
        except BaseException as error:  # noqa: BLE001 - re-raised by result()
            self.error = error

    def result(self, timeout=10):
        self.join(timeout)
        assert not self.is_alive(), "worker did not finish"
        if self.error is not None:
            raise self.error
        return self.value


def wait_for_lock_waiter(database, timeout=5.0):
    """Poll until some other backend of the test database is waiting on a lock."""
    deadline = monotonic() + timeout
    with connect(database) as conn:
        conn.autocommit = True  # Fresh pg_stat_activity snapshot on every query.
        while monotonic() < deadline:
            waiting = conn.execute(
                "SELECT count(*) AS n FROM pg_stat_activity WHERE datname=current_database()"
                " AND wait_event_type='Lock' AND pid <> pg_backend_pid()"
            ).fetchone()["n"]
            if waiting:
                return
            sleep(0.01)
    raise AssertionError("no backend waited for the lock")


def start_blocked(database, holder, call):
    """Start call() in a worker and confirm it is waiting on a lock the holder owns."""
    worker = Worker(call)
    worker.start()
    try:
        wait_for_lock_waiter(database)
    except AssertionError:
        holder.rollback()
        worker.join(10)
        raise
    return worker


def holding_doctor_lock(database):
    holder = connect(database)
    holder.execute("SELECT id FROM doctors WHERE id=%s FOR UPDATE", (DOCTOR_ID,))
    return holder


def create(database, actor, doctor, start, end, kind="leave"):
    with connect(database) as conn:
        return schedule_exceptions.create_exception(conn, actor, doctor, kind, start, end, "")


def attempt(database, start):
    try:
        return book(database, start)["status"]
    except HTTPException as error:
        return error.status_code


@pytest.mark.parametrize("doctor", [DOCTOR_ID, None])
def test_exception_waits_for_reservation_and_reports_it(database, doctor):
    actor = staff_actor(database)
    holder = holding_doctor_lock(database)
    worker = start_blocked(
        database, holder, lambda: create(database, actor, doctor, at(10), at(11))
    )
    # The lock holder plays a reservation that won the lock first and commits inside the window.
    appointment = uuid4()
    holder.execute(
        "INSERT INTO appointments (id,clinic_id,doctor_id,service_id,starts_at,ends_at,"
        "blocks_until,status,idempotency_key,request_hash)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,'pending_sync','held-lock-0001','synthetic')",
        (appointment, CLINIC, DOCTOR_ID, SERVICE_ID, at(10, 15), at(10, 30), at(10, 30)),
    )
    holder.commit()
    holder.close()
    assert worker.result()["conflicting_appointment_ids"] == [appointment]


def test_reservation_waits_for_exception_and_is_rejected(database):
    actor = staff_actor(database)
    holder = holding_doctor_lock(database)
    worker = start_blocked(database, holder, lambda: attempt(database, at(10)))
    # The lock holder plays an exception creation that won the lock first.
    holder.execute(
        "INSERT INTO schedule_exceptions (id,clinic_id,doctor_id,kind,starts_at,ends_at,created_by)"
        " VALUES (%s,%s,%s,'leave',%s,%s,%s)",
        (uuid4(), CLINIC, DOCTOR_ID, at(9, 30), at(11), actor["id"]),
    )
    holder.commit()
    holder.close()
    assert worker.result() == 409
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) AS n FROM appointments").fetchone()["n"] == 0


def test_create_doctor_waits_for_clinic_wide_exception(database):
    actor = staff_actor(database)
    holder = connect(database)
    lock_doctor_set(holder, CLINIC, exclusive=True)  # Plays a clinic-wide exception in flight.

    def add_doctor():
        with connect(database) as conn:
            return admin.create_doctor(conn, actor, "Late arrival")

    worker = start_blocked(database, holder, add_doctor)
    holder.rollback()
    holder.close()
    assert worker.result()["active"] is True


def test_clinic_wide_exception_waits_for_doctor_creation(database):
    actor = staff_actor(database)
    holder = connect(database)
    new_doctor = admin.create_doctor(holder, actor, "In flight")  # Shared doctor-set lock held.
    worker = start_blocked(
        database, holder, lambda: create(database, actor, None, at(9), at(17), "holiday")
    )
    holder.commit()
    holder.close()
    worker.result()
    with connect(database) as conn:
        # The clinic-wide exception locked the new doctor's row too: it is fully committed.
        assert conn.execute("SELECT 1 FROM doctors WHERE id=%s", (new_doctor["id"],)).fetchone()


def test_hours_and_service_edits_race_clinic_wide_exception_without_deadlock(database):
    actor = staff_actor(database)
    other_doctor, other_service = second_doctor(database)
    hours = [HoursWindow(weekday=DAY.weekday(), opens="09:00", closes="17:00")]

    def run(barrier, function, *args):
        barrier.wait()
        with connect(database) as conn:
            return function(conn, actor, *args)

    for trial in range(5):
        barrier = Barrier(4)
        start = at(9) + timedelta(minutes=30 * trial)
        calls = [
            (
                schedule_exceptions.create_exception,
                None,
                "break",
                start,
                start + timedelta(minutes=15),
                "",
            ),
            (admin.replace_hours, other_doctor, hours),
            (admin.replace_hours, DOCTOR_ID, hours),
            (admin.update_service, other_service, {"buffer_after_minutes": trial}),
        ]
        workers = [Worker(partial(run, barrier, *call)) for call in calls]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.result()  # Raises DeadlockDetected if the lock order were inconsistent.
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) AS n FROM schedule_exceptions").fetchone()["n"] == 5


@pytest.mark.parametrize("doctor", [DOCTOR_ID, None])
def test_exception_reservation_race_smoke(database, doctor):
    """Smoke test: which side wins each trial is not controlled (the lock-holder tests above
    prove both orderings). Whatever the interleaving, the invariant must hold."""
    actor = staff_actor(database)
    reported = set()
    for trial in range(8):
        start = at(9) + timedelta(minutes=30 * trial)
        barrier = Barrier(2)

        def make(start=start, barrier=barrier):
            barrier.wait()
            return create(database, actor, doctor, start, start + timedelta(minutes=15), "break")

        def reserve(start=start, barrier=barrier):
            barrier.wait()
            try:
                return book(database, start)
            except HTTPException as error:
                return error.status_code

        workers = [Worker(make), Worker(reserve)]
        for worker in workers:
            worker.start()
        exception, booking = (worker.result() for worker in workers)
        conflicts = exception["conflicting_appointment_ids"]
        if booking == 409:
            assert conflicts == []  # The exception committed first and blocked the booking.
        else:
            assert conflicts == [booking["appointment_id"]]  # The booking committed first.
            reported.add(booking["appointment_id"])
    # Invariant: no active appointment sits inside a committed exception unless it was
    # reported as conflicting when that exception was created.
    with connect(database) as conn:
        inside = conn.execute(
            """SELECT a.id FROM appointments a JOIN schedule_exceptions e
            ON e.clinic_id=a.clinic_id AND (e.doctor_id IS NULL OR e.doctor_id=a.doctor_id)
            AND a.starts_at < e.ends_at AND a.blocks_until > e.starts_at
            WHERE a.status = ANY(%s)""",
            (list(ACTIVE_STATUSES),),
        ).fetchall()
    assert {r["id"] for r in inside} == reported
