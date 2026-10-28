from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from psycopg.errors import ForeignKeyViolation

from clinic_voice import credentials
from clinic_voice.cli import DEMO_CLINIC_ID as CLINIC
from clinic_voice.cli import DOCTOR_ID, SERVICE_ID
from clinic_voice.db import connect, migrate
from clinic_voice.schemas import ReservationRequest
from clinic_voice.service import reserve, slots


@pytest.fixture
def request_body():
    tomorrow = datetime.now(ZoneInfo("Asia/Kolkata")).date() + timedelta(days=1)
    return ReservationRequest(
        doctor_id=DOCTOR_ID,
        service_id=SERVICE_ID,
        starts_at=datetime.combine(tomorrow, datetime.min.time(), ZoneInfo("Asia/Kolkata")).replace(
            hour=9
        ),
    )


def test_atomic_outbox_and_retry(database, request_body):
    a = reserve(database, CLINIC, request_body, "retry-key-0001")
    b = reserve(database, CLINIC, request_body, "retry-key-0001")
    assert a == b
    assert a["status"] == "pending_sync"
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) AS n FROM appointments").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM outbox").fetchone()["n"] == 1


def test_offset_equivalent_retry(database, request_body):
    a = reserve(database, CLINIC, request_body, "offset-key-0001")
    request_body.starts_at = request_body.starts_at.astimezone(UTC)
    assert reserve(database, CLINIC, request_body, "offset-key-0001") == a


def test_changed_retry_rejected(database, request_body):
    reserve(database, CLINIC, request_body, "changed-key-0001")
    request_body.starts_at += timedelta(minutes=15)
    with pytest.raises(HTTPException) as error:
        reserve(database, CLINIC, request_body, "changed-key-0001")
    assert error.value.status_code == 409


def test_concurrent_distinct_requests_one_wins(database, request_body):
    barrier = Barrier(2)

    def run(key):
        barrier.wait()
        try:
            return reserve(database, CLINIC, request_body, key)["status"]
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run, ["concurrent-key-01", "concurrent-key-02"]))
    assert sorted(map(str, outcomes)) == ["409", "pending_sync"]
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) AS n FROM outbox").fetchone()["n"] == 1


def test_concurrent_same_key_replays(database, request_body):
    barrier = Barrier(2)

    def run(_):
        barrier.wait()
        return reserve(database, CLINIC, request_body, "same-key-0001")

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run, range(2)))
    assert outcomes[0] == outcomes[1]


def test_unknown_tenant_cannot_read_or_book(database, request_body):
    other = uuid4()
    with connect(database) as conn, pytest.raises(HTTPException) as error:
        slots(conn, other, DOCTOR_ID, SERVICE_ID, request_body.starts_at.date())
    assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        reserve(database, other, request_body, "other-tenant-0001")
    assert error.value.status_code == 404


def test_composite_fk_blocks_cross_tenant_write(database, request_body):
    with connect(database) as conn:
        other = uuid4()
        conn.execute("INSERT INTO clinics(id,name) VALUES (%s,'Other')", (other,))
    with pytest.raises(ForeignKeyViolation), connect(database) as conn:
        conn.execute(
            "INSERT INTO services VALUES (%s,%s,%s,'Invalid',15)", (uuid4(), other, DOCTOR_ID)
        )


def test_hours_and_grid_enforced(database, request_body):
    request_body.starts_at = request_body.starts_at.replace(hour=8)
    with pytest.raises(HTTPException) as error:
        reserve(database, CLINIC, request_body, "early-key-0001")
    assert error.value.status_code == 409


def test_adjoining_slots_permitted(database, request_body):
    reserve(database, CLINIC, request_body, "adjacent-key-01")
    request_body.starts_at += timedelta(minutes=15)
    assert reserve(database, CLINIC, request_body, "adjacent-key-02")["status"] == "pending_sync"


def test_migration_replay_and_checksum(database, tmp_path):
    migrate(database, Path("migrations"))
    (tmp_path / "001_initial.sql").write_text("SELECT 1;")
    with pytest.raises(RuntimeError, match="modified"):
        migrate(database, tmp_path)


def test_database_constraint_blocks_overlap_across_services(database, request_body):
    from psycopg.errors import ExclusionViolation

    reserve(database, CLINIC, request_body, "constraint-key-01")
    other_service = uuid4()
    with connect(database) as conn:
        conn.execute(
            "INSERT INTO services VALUES (%s,%s,%s,'Other service',30)",
            (other_service, CLINIC, DOCTOR_ID),
        )
    with pytest.raises(ExclusionViolation), connect(database) as conn:
        conn.execute(
            """INSERT INTO appointments (id,clinic_id,doctor_id,service_id,starts_at,ends_at,status,idempotency_key,request_hash)
            VALUES (%s,%s,%s,%s,%s,%s,'pending_sync','raw-overlap-01','synthetic')""",
            (
                uuid4(),
                CLINIC,
                DOCTOR_ID,
                other_service,
                request_body.starts_at + timedelta(minutes=5),
                request_body.starts_at + timedelta(minutes=35),
            ),
        )


def test_outbox_failure_rolls_back_reservation(database, request_body):
    from psycopg.errors import CheckViolation

    with connect(database) as conn:
        conn.execute(
            "ALTER TABLE outbox ADD CONSTRAINT synthetic_failure CHECK (kind <> 'calendar.create')"
        )
    try:
        with pytest.raises(CheckViolation):
            reserve(database, CLINIC, request_body, "rollback-key-01")
        with connect(database) as conn:
            assert conn.execute("SELECT count(*) AS n FROM appointments").fetchone()["n"] == 0
    finally:
        with connect(database) as conn:
            conn.execute("ALTER TABLE outbox DROP CONSTRAINT synthetic_failure")


def test_http_reservation_flow(database, request_body, monkeypatch):
    from fastapi.testclient import TestClient

    from clinic_voice.api import app
    from clinic_voice.config import get_settings

    with connect(database) as conn:
        _, token = credentials.issue(conn, CLINIC, "http-test")
    monkeypatch.setenv("DATABASE_URL", database)
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            assert client.get("/health/ready").json()["status"] == "ready"
            headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "http-test-0001"}
            query = {
                "doctor_id": str(DOCTOR_ID),
                "service_id": str(SERVICE_ID),
                "day": request_body.starts_at.date().isoformat(),
            }
            found = client.post("/v1/tools/check-slots", headers=headers, json=query)
            assert found.status_code == 200
            assert len(found.json()["slots"]) == 3
            booked = client.post(
                "/v1/tools/reserve-slot", headers=headers, json=request_body.model_dump(mode="json")
            )
            assert booked.status_code == 202
            assert booked.json()["status"] == "pending_sync"
            retry = client.post(
                "/v1/tools/reserve-slot", headers=headers, json=request_body.model_dump(mode="json")
            )
            assert retry.json() == booked.json()
            assert (
                client.post("/v1/tools/check-slots", headers=headers, json=query).json()["slots"][0]
                != found.json()["slots"][0]
            )
    finally:
        get_settings.cache_clear()
