"""Upgrade path for migration 005 on a database that already holds appointments.

Uses a separate scratch database (name ending _test) so the shared fixture is untouched.
"""

import os
import re
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.errors import NotNullViolation

from clinic_voice.cli import DEMO_CLINIC_ID as CLINIC
from clinic_voice.cli import DOCTOR_ID, SERVICE_ID, seed
from clinic_voice.db import connect, migrate


@pytest.fixture
def scratch_database():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not configured; PostgreSQL required")
    base = conninfo_to_dict(url).get("dbname", "")
    if not base.endswith("_test"):
        pytest.fail("Refusing destructive integration tests against a database not ending _test")
    name = base.removesuffix("_test") + "_migration_test"
    drop = sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(drop)
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield make_conninfo(url, dbname=name)
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(drop)


def test_005_backfills_blocks_until_and_rebuilds_exclusion(scratch_database, tmp_path):
    for path in sorted(Path("migrations").glob("00[1-4]_*.sql")):
        shutil.copy(path, tmp_path / path.name)
    migrate(scratch_database, tmp_path)
    seed(scratch_database)
    starts_at = datetime(2031, 1, 6, 4, 30, tzinfo=UTC)
    ends_at = starts_at + timedelta(minutes=15)
    appointment = uuid4()
    with connect(scratch_database) as conn:
        conn.execute(
            "INSERT INTO appointments (id,clinic_id,doctor_id,service_id,starts_at,ends_at,"
            "status,idempotency_key,request_hash)"
            " VALUES (%s,%s,%s,%s,%s,%s,'confirmed','pre-005-key','synthetic')",
            (appointment, CLINIC, DOCTOR_ID, SERVICE_ID, starts_at, ends_at),
        )

    migrate(scratch_database, Path("migrations"))

    with connect(scratch_database) as conn:
        row = conn.execute(
            "SELECT ends_at, blocks_until FROM appointments WHERE id=%s", (appointment,)
        ).fetchone()
        definition = conn.execute(
            "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint"
            " WHERE conname='no_overlapping_appointments'"
        ).fetchone()["d"]
    assert row["blocks_until"] == row["ends_at"] == ends_at
    assert "blocks_until" in definition and "ends_at" not in definition
    # blocks_until has no default: a pre-005 binary's insert fails instead of booking silently.
    with pytest.raises(NotNullViolation), connect(scratch_database) as conn:
        conn.execute(
            "INSERT INTO appointments (id,clinic_id,doctor_id,service_id,starts_at,ends_at,"
            "status,idempotency_key,request_hash)"
            " VALUES (%s,%s,%s,%s,%s,%s,'confirmed','old-binary-key','synthetic')",
            (uuid4(), CLINIC, DOCTOR_ID, SERVICE_ID, ends_at, ends_at + timedelta(minutes=15)),
        )


def test_007_keeps_existing_outbox_rows_and_versions_uniqueness(scratch_database, tmp_path):
    for path in sorted(Path("migrations").glob("00[1-6]_*.sql")):
        shutil.copy(path, tmp_path / path.name)
    migrate(scratch_database, tmp_path)
    seed(scratch_database)
    starts_at = datetime(2031, 1, 6, 4, 30, tzinfo=UTC)
    appointment, job = uuid4(), uuid4()
    with connect(scratch_database) as conn:
        conn.execute(
            "INSERT INTO appointments (id,clinic_id,doctor_id,service_id,starts_at,ends_at,"
            "blocks_until,status,idempotency_key,request_hash)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,'pending_sync','pre-007-key','synthetic')",
            (
                appointment,
                CLINIC,
                DOCTOR_ID,
                SERVICE_ID,
                starts_at,
                starts_at + timedelta(minutes=15),
                starts_at + timedelta(minutes=15),
            ),
        )
        conn.execute(
            "INSERT INTO outbox (id,clinic_id,appointment_id,kind) VALUES (%s,%s,%s,'calendar.create')",
            (job, CLINIC, appointment),
        )

    migrate(scratch_database, Path("migrations"))

    with connect(scratch_database) as conn:
        row = conn.execute(
            "SELECT o.status, o.attempts, o.appointment_version, o.payload, o.max_attempts,"
            " a.version FROM outbox o JOIN appointments a ON a.id=o.appointment_id WHERE o.id=%s",
            (job,),
        ).fetchone()
        assert row == {
            "status": "pending",
            "attempts": 0,
            "appointment_version": 1,
            "payload": {},
            "max_attempts": 8,
            "version": 1,
        }
        # A rescheduled version may enqueue its own create; the same version may not.
        conn.execute(
            "INSERT INTO outbox (id,clinic_id,appointment_id,kind,appointment_version)"
            " VALUES (%s,%s,%s,'calendar.create',2)",
            (uuid4(), CLINIC, appointment),
        )
    with pytest.raises(psycopg.errors.UniqueViolation), connect(scratch_database) as conn:
        conn.execute(
            "INSERT INTO outbox (id,clinic_id,appointment_id,kind) VALUES (%s,%s,%s,'calendar.create')",
            (uuid4(), CLINIC, appointment),
        )


def test_012_backfills_unique_booking_references(scratch_database, tmp_path):
    for path in sorted(Path("migrations").glob("0*.sql")):
        if path.name < "012":
            shutil.copy(path, tmp_path / path.name)
    migrate(scratch_database, tmp_path)
    seed(scratch_database)
    starts_at = datetime(2031, 1, 6, 4, 30, tzinfo=UTC)
    with connect(scratch_database) as conn:
        for index in range(5):
            start = starts_at + timedelta(minutes=15 * index)
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
                    f"pre-012-{index}",
                ),
            )

    migrate(scratch_database, Path("migrations"))

    with connect(scratch_database) as conn:
        references = [
            r["booking_reference"]
            for r in conn.execute("SELECT booking_reference FROM appointments").fetchall()
        ]
        # The column default covers inserts that do not name a reference.
        default = conn.execute("SELECT new_booking_reference() AS r").fetchone()["r"]
    assert len(set(references)) == 5
    assert all(re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{8}", r) for r in [*references, default])
