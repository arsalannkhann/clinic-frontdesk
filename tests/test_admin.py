from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from clinic_voice import credentials, staff
from clinic_voice.api import app
from clinic_voice.cli import DEMO_CLINIC_ID as CLINIC
from clinic_voice.cli import DOCTOR_ID, SERVICE_ID
from clinic_voice.config import get_settings
from clinic_voice.db import connect

PASSWORD = "synthetic-password-01"
IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture(autouse=True)
def cheap_hashing(monkeypatch):
    monkeypatch.setattr(staff, "SCRYPT_N", 2**10)


@pytest.fixture
def client(database, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database)
    get_settings.cache_clear()
    with TestClient(app) as value:
        yield value
    get_settings.cache_clear()


def headers(client, database, role="owner", clinic=CLINIC):
    email = f"{role}-{uuid4().hex[:8]}@example.test"
    with connect(database) as conn:
        staff.create_user(conn, clinic, email, role, PASSWORD)
    r = client.post("/v1/staff/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def foreign(database):
    """A second clinic with a doctor and service; returns (clinic, doctor, service)."""
    clinic, doctor, service = uuid4(), uuid4(), uuid4()
    with connect(database) as conn:
        conn.execute("INSERT INTO clinics (id,name) VALUES (%s,'Synthetic Other')", (clinic,))
        conn.execute(
            "INSERT INTO doctors (id,clinic_id,name) VALUES (%s,%s,'Other')", (doctor, clinic)
        )
        conn.execute(
            "INSERT INTO services (id,clinic_id,doctor_id,name,duration_minutes)"
            " VALUES (%s,%s,%s,'Other',15)",
            (service, clinic, doctor),
        )
    return clinic, doctor, service


def audit_actions(database):
    with connect(database) as conn:
        return [r["action"] for r in conn.execute("SELECT action FROM audit_events").fetchall()]


def tool_headers(database):
    with connect(database) as conn:
        _, token = credentials.issue(conn, CLINIC, "synthetic")
    return {"Authorization": "Bearer " + token}


def check_slots(client, database, day):
    return client.post(
        "/v1/tools/check-slots",
        headers=tool_headers(database),
        json={"doctor_id": str(DOCTOR_ID), "service_id": str(SERVICE_ID), "day": day.isoformat()},
    )


# Roles


def test_role_matrix(client, database):
    viewer = headers(client, database, "viewer")
    admin = headers(client, database, "admin")
    owner = headers(client, database, "owner")
    reads = ["/clinic", "/doctors", "/services", f"/doctors/{DOCTOR_ID}/hours"]
    for role in (viewer, admin, owner):
        for path in reads:
            assert client.get("/v1/admin" + path, headers=role).status_code == 200
    writes = [
        ("patch", "/clinic", {"name": "X"}),
        ("post", "/doctors", {"name": "X"}),
        ("patch", f"/doctors/{DOCTOR_ID}", {"active": True}),
        (
            "post",
            "/services",
            {"doctor_id": str(DOCTOR_ID), "name": "X", "duration_minutes": 10},
        ),
        ("patch", f"/services/{SERVICE_ID}", {"active": True}),
        ("put", f"/doctors/{DOCTOR_ID}/hours", []),
    ]
    for method, path, body in writes:
        r = getattr(client, method)("/v1/admin" + path, headers=viewer, json=body)
        assert r.status_code == 403, path
    for role in (admin, owner):
        for method, path, body in writes:
            r = getattr(client, method)("/v1/admin" + path, headers=role, json=body)
            assert r.status_code in (200, 201), (path, r.text)
    assert client.get("/v1/admin/clinic").status_code == 401


def test_tool_credential_is_not_staff(client, database):
    r = client.get("/v1/admin/clinic", headers=tool_headers(database))
    assert r.status_code == 401


# Clinic


def test_clinic_get_and_partial_patch(client, database):
    h = headers(client, database, "admin")
    before = client.get("/v1/admin/clinic", headers=h).json()
    assert before["lead_time_minutes"] == 60 and before["horizon_days"] == 90
    r = client.patch("/v1/admin/clinic", headers=h, json={"horizon_days": 30})
    assert r.status_code == 200
    after = r.json()
    assert after["horizon_days"] == 30
    assert after["name"] == before["name"] and after["timezone"] == before["timezone"]
    assert "clinic.updated" in audit_actions(database)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": ""},
        {"name": "x" * 121},
        {"name": None},
        {"timezone": "local"},
        {"timezone": ""},
        {"timezone": "Mars/Olympus"},
        {"timezone": "../etc/passwd"},
        {"lead_time_minutes": -1},
        {"lead_time_minutes": 10081},
        {"horizon_days": 0},
        {"horizon_days": 366},
        {"clinic_id": str(uuid4())},
    ],
)
def test_clinic_patch_rejects_invalid(client, database, body):
    h = headers(client, database)
    assert client.patch("/v1/admin/clinic", headers=h, json=body).status_code == 422


def test_clinic_timezone_valid_zone(client, database):
    h = headers(client, database)
    r = client.patch("/v1/admin/clinic", headers=h, json={"timezone": "America/New_York"})
    assert r.status_code == 200 and r.json()["timezone"] == "America/New_York"


def test_other_clinic_untouched(client, database):
    other, _, _ = foreign(database)
    h = headers(client, database)
    client.patch("/v1/admin/clinic", headers=h, json={"name": "Renamed"})
    with connect(database) as conn:
        row = conn.execute("SELECT name FROM clinics WHERE id=%s", (other,)).fetchone()
    assert row["name"] == "Synthetic Other"


# Doctors and services


def test_doctor_create_update_list(client, database):
    h = headers(client, database)
    r = client.post("/v1/admin/doctors", headers=h, json={"name": "  Dr Synthetic "})
    assert (
        r.status_code == 201 and r.json()["active"] is True and r.json()["name"] == "Dr Synthetic"
    )
    doctor_id = r.json()["id"]
    r = client.patch(f"/v1/admin/doctors/{doctor_id}", headers=h, json={"active": False})
    assert r.status_code == 200 and r.json()["active"] is False
    ids = {d["id"] for d in client.get("/v1/admin/doctors", headers=h).json()}
    assert {doctor_id, str(DOCTOR_ID)} <= ids
    actions = audit_actions(database)
    assert "doctor.created" in actions and "doctor.updated" in actions


def test_service_create_update(client, database):
    h = headers(client, database)
    body = {
        "doctor_id": str(DOCTOR_ID),
        "name": "Scaling",
        "duration_minutes": 30,
        "buffer_after_minutes": 10,
    }
    r = client.post("/v1/admin/services", headers=h, json=body)
    assert r.status_code == 201, r.text
    assert r.json()["buffer_after_minutes"] == 10 and r.json()["active"] is True
    sid = r.json()["id"]
    r = client.patch(
        f"/v1/admin/services/{sid}", headers=h, json={"duration_minutes": 45, "active": False}
    )
    assert r.status_code == 200
    assert r.json()["duration_minutes"] == 45 and r.json()["doctor_id"] == str(DOCTOR_ID)
    # doctor_id cannot be changed
    r = client.patch(f"/v1/admin/services/{sid}", headers=h, json={"doctor_id": str(uuid4())})
    assert r.status_code == 422
    actions = audit_actions(database)
    assert "service.created" in actions and "service.updated" in actions
    listed = client.get("/v1/admin/services", headers=h).json()
    assert sid in {s["id"] for s in listed}


@pytest.mark.parametrize(
    "extra",
    [
        {"duration_minutes": 4},
        {"duration_minutes": 241},
        {"buffer_after_minutes": -1},
        {"buffer_after_minutes": 121},
        {"clinic_id": str(uuid4())},
    ],
)
def test_service_create_validation(client, database, extra):
    h = headers(client, database)
    body = {"doctor_id": str(DOCTOR_ID), "name": "X", "duration_minutes": 15} | extra
    assert client.post("/v1/admin/services", headers=h, json=body).status_code == 422


def test_clinic_id_in_body_rejected(client, database):
    h = headers(client, database)
    r = client.post("/v1/admin/doctors", headers=h, json={"name": "X", "clinic_id": str(uuid4())})
    assert r.status_code == 422
    r = client.patch(f"/v1/admin/doctors/{DOCTOR_ID}", headers=h, json={"clinic_id": str(CLINIC)})
    assert r.status_code == 422


# Cross-tenant


def test_cross_tenant_ids_are_404(client, database):
    _, other_doctor, other_service = foreign(database)
    h = headers(client, database)
    unknown = uuid4()
    for doctor in (other_doctor, unknown):
        assert (
            client.patch(
                f"/v1/admin/doctors/{doctor}", headers=h, json={"active": False}
            ).status_code
            == 404
        )
        r = client.post(
            "/v1/admin/services",
            headers=h,
            json={"doctor_id": str(doctor), "name": "X", "duration_minutes": 15},
        )
        assert r.status_code == 404
        assert client.get(f"/v1/admin/doctors/{doctor}/hours", headers=h).status_code == 404
        assert (
            client.put(f"/v1/admin/doctors/{doctor}/hours", headers=h, json=[]).status_code == 404
        )
    for service in (other_service, unknown):
        r = client.patch(f"/v1/admin/services/{service}", headers=h, json={"active": False})
        assert r.status_code == 404
    # Nothing in the other clinic changed.
    with connect(database) as conn:
        assert conn.execute("SELECT active FROM doctors WHERE id=%s", (other_doctor,)).fetchone()[
            "active"
        ]
        assert conn.execute("SELECT active FROM services WHERE id=%s", (other_service,)).fetchone()[
            "active"
        ]
    ids = {s["id"] for s in client.get("/v1/admin/services", headers=h).json()}
    assert str(other_service) not in ids


# Hours


def window(weekday, opens, closes):
    return {"weekday": weekday, "opens": opens, "closes": closes}


def test_hours_replace_and_read(client, database):
    h = headers(client, database)
    new = [window(0, "08:00", "12:00"), window(0, "13:00", "17:30"), window(2, "09:00", "10:00")]
    r = client.put(f"/v1/admin/doctors/{DOCTOR_ID}/hours", headers=h, json=new)
    assert r.status_code == 200 and r.json() == new
    assert client.get(f"/v1/admin/doctors/{DOCTOR_ID}/hours", headers=h).json() == new
    assert "hours.replaced" in audit_actions(database)
    # Adjacent windows are allowed; empty list makes the doctor unbookable.
    r = client.put(f"/v1/admin/doctors/{DOCTOR_ID}/hours", headers=h, json=[])
    assert r.status_code == 200 and r.json() == []
    day = datetime.now(IST).date() + timedelta(days=1)
    assert check_slots(client, database, day).json()["slots"] == []


@pytest.mark.parametrize(
    "body",
    [
        [window(0, "09:00", "09:00")],
        [window(0, "10:00", "09:00")],
        [window(0, "09:00", "12:00"), window(0, "11:00", "13:00")],
        [window(0, "09:00", "12:00"), window(0, "09:00", "12:00")],
        [window(7, "09:00", "10:00")],
        [window(0, "9:00", "10:00")],
        [window(0, "09:00", "24:00")],
        [window(0, "09:00", "10:00")] * 51,
        [{"weekday": 0, "opens": "09:00", "closes": "10:00", "clinic_id": str(CLINIC)}],
    ],
)
def test_hours_validation_and_atomicity(client, database, body):
    h = headers(client, database)
    before = client.get(f"/v1/admin/doctors/{DOCTOR_ID}/hours", headers=h).json()
    r = client.put(f"/v1/admin/doctors/{DOCTOR_ID}/hours", headers=h, json=body)
    assert r.status_code == 422
    assert client.get(f"/v1/admin/doctors/{DOCTOR_ID}/hours", headers=h).json() == before
    assert before  # seeded hours still present


def test_hours_replacement_rolls_back_on_failure(client, database, monkeypatch):
    from clinic_voice import admin

    h = headers(client, database)
    before = client.get(f"/v1/admin/doctors/{DOCTOR_ID}/hours", headers=h).json()
    real = admin.audit

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic failure after delete and insert")

    monkeypatch.setattr(admin, "audit", boom)
    with TestClient(app, raise_server_exceptions=False) as raw:
        r = raw.put(
            f"/v1/admin/doctors/{DOCTOR_ID}/hours", headers=h, json=[window(1, "10:00", "11:00")]
        )
    assert r.status_code == 500
    monkeypatch.setattr(admin, "audit", real)
    assert client.get(f"/v1/admin/doctors/{DOCTOR_ID}/hours", headers=h).json() == before


# Slot behavior


def test_horizon_respected_by_slot_lookup(client, database):
    h = headers(client, database)
    today = datetime.now(IST).date()
    assert check_slots(client, database, today + timedelta(days=30)).status_code == 200
    assert client.patch("/v1/admin/clinic", headers=h, json={"horizon_days": 10}).status_code == 200
    assert check_slots(client, database, today + timedelta(days=10)).status_code == 200
    assert check_slots(client, database, today + timedelta(days=11)).status_code == 422
    client.patch("/v1/admin/clinic", headers=h, json={"horizon_days": 120})
    assert check_slots(client, database, today + timedelta(days=100)).status_code == 200


def reserve(client, database, starts_at):
    return client.post(
        "/v1/tools/reserve-slot",
        headers=tool_headers(database) | {"Idempotency-Key": uuid4().hex},
        json={
            "doctor_id": str(DOCTOR_ID),
            "service_id": str(SERVICE_ID),
            "starts_at": starts_at,
        },
    )


@pytest.mark.parametrize("target", ["service", "doctor"])
def test_inactive_is_unbookable(client, database, target):
    h = headers(client, database)
    day = datetime.now(IST).date() + timedelta(days=2)
    slot = check_slots(client, database, day).json()["slots"][0]
    assert reserve(client, database, slot).status_code == 202
    path = (
        f"/v1/admin/services/{SERVICE_ID}"
        if target == "service"
        else f"/v1/admin/doctors/{DOCTOR_ID}"
    )
    assert client.patch(path, headers=h, json={"active": False}).status_code == 200
    assert check_slots(client, database, day).status_code == 404
    other = check_slots(client, database, day + timedelta(days=1))
    assert other.status_code == 404
    assert reserve(client, database, slot).status_code == 404
    assert client.patch(path, headers=h, json={"active": True}).status_code == 200
    assert check_slots(client, database, day).status_code == 200
