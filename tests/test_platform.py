"""Platform foundation (P1): pooler-safe connections, migration 018 and Supabase hardening,
the ledger calendar, the worker tick and inline confirm, the public clinic API, and
`clinic-voice seed-platform`. Synthetic data only."""

import ast
import logging
import os
import re
import shutil
import threading
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from clinic_voice import db, public, routes_public, seed_platform, service, tick
from clinic_voice.api import app
from clinic_voice.appointments import cancel_locked, lock_appointment
from clinic_voice.cli import DEMO_CLINIC_ID as CLINIC
from clinic_voice.cli import DOCTOR_ID, SERVICE_ID, build_ports, seed
from clinic_voice.config import Settings, get_settings
from clinic_voice.crypto import Keyring, decode_hash_key, generate_key, phone_hash
from clinic_voice.db import connect, migrate
from clinic_voice.errors import PublicError
from clinic_voice.outbox import Ports, run_batch
from clinic_voice.patients import decrypt_field
from clinic_voice.providers.calendar import (
    CalendarEvent,
    FakeCalendar,
    ProviderPermanent,
    ProviderRetryable,
    event_id_for,
)
from clinic_voice.providers.ledger import LedgerCalendar, LedgerResolver
from clinic_voice.schemas import PublicBookingRequest, ReservationRequest

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2031, 3, 3, 8, 30, tzinfo=IST)  # A Monday.
DAY = NOW.date() + timedelta(days=2)
SLOT = datetime.combine(DAY, time(10, 0), IST)
KEYS = "k1:" + generate_key()
HASH_KEY = generate_key()
TICK_SECRET = "synthetic-tick-secret-" + "0" * 32
SLUG = "demo-clinic"
NAME = "Synthetic Patientname"
PHONE = "+919000000101"
NOTES = "SYNTHETIC-NOTE-MARKER sensitive tooth"
OTHER_CLINIC = UUID("00000000-0000-4000-8000-0000000000c1")
BASE = f"/v1/public/clinics/{SLUG}"


# --- Fixtures ---------------------------------------------------------------------------


@pytest.fixture
def env(database, monkeypatch):
    monkeypatch.setattr(service, "utc_now", lambda: NOW.astimezone(UTC))
    monkeypatch.setenv("DATABASE_URL", database)
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEYS", KEYS)
    monkeypatch.setenv("PHONE_HASH_KEY", HASH_KEY)
    monkeypatch.setenv("WORKER_TICK_SECRET", TICK_SECRET)
    get_settings.cache_clear()
    tick._ports_cache.clear()
    with connect(database) as conn:
        conn.execute(
            "UPDATE clinics SET public_slug=%s, public_enabled=true,"
            " public_tagline='Synthetic tagline' WHERE id=%s",
            (SLUG, CLINIC),
        )
        conn.execute(
            "UPDATE doctors SET booking_calendar_id='ledger:' || id::text WHERE id=%s",
            (DOCTOR_ID,),
        )
    with TestClient(app) as client:
        yield {"client": client, "url": database}
    get_settings.cache_clear()
    tick._ports_cache.clear()


def booking(starts_at=SLOT, phone=PHONE, name=NAME, **extra):
    body = {
        "service_id": str(SERVICE_ID),
        "doctor_id": str(DOCTOR_ID),
        "starts_at": starts_at if isinstance(starts_at, str) else starts_at.isoformat(),
        "name": name,
        "phone": phone,
        "consent_contact": True,
    }
    return {**body, **extra}


def appointment_by_reference(url, reference):
    with connect(url) as conn:
        return conn.execute(
            "SELECT * FROM appointments WHERE booking_reference=%s", (reference,)
        ).fetchone()


def create_job(url, appointment_id):
    with connect(url) as conn:
        return conn.execute(
            "SELECT * FROM outbox WHERE appointment_id=%s AND kind='calendar.create'",
            (appointment_id,),
        ).fetchone()


def reserve_direct(url, starts_at):
    return service.reserve(
        url,
        CLINIC,
        ReservationRequest(doctor_id=DOCTOR_ID, service_id=SERVICE_ID, starts_at=starts_at),
        f"test:{uuid4()}",
    )


def assert_envelope(response, status, code):
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"} and body["error"]["code"] == code
    assert set(body["error"]) == {"code", "message"}


# --- Pooler-safe connections -------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "url", "pooled"),
    [
        (None, "postgresql://u@db.example:6543/postgres", True),
        (None, "postgresql://u@db.example:5432/postgres", False),
        (None, "postgresql://u@localhost/postgres", False),
        ("transaction", "postgresql://u@db.example:5432/postgres", True),
        ("session", "postgresql://u@db.example:6543/postgres", False),
        ("none", "postgresql://u@db.example:6543/postgres", False),
    ],
)
def test_transaction_pooler_detection(monkeypatch, mode, url, pooled):
    if mode is None:
        monkeypatch.delenv("DATABASE_POOLER", raising=False)
    else:
        monkeypatch.setenv("DATABASE_POOLER", mode)
    assert db.transaction_pooled(url) is pooled


def test_invalid_pooler_mode_is_refused(monkeypatch):
    monkeypatch.setenv("DATABASE_POOLER", "statement")
    with pytest.raises(ValueError):
        db.transaction_pooled("postgresql://u@db.example/postgres")


def test_connect_disables_prepared_statements_only_when_pooled(monkeypatch):
    seen = []
    monkeypatch.setattr(db.psycopg, "connect", lambda url, **options: seen.append(options))
    monkeypatch.delenv("DATABASE_POOLER", raising=False)
    db.connect("postgresql://u@db.example:6543/postgres")
    db.connect("postgresql://u@db.example:5432/postgres")
    assert seen[0]["prepare_threshold"] is None
    assert "prepare_threshold" not in seen[1]


def test_pooled_connection_works_against_postgres(database, monkeypatch):
    monkeypatch.setenv("DATABASE_POOLER", "transaction")
    with connect(database) as conn:
        assert conn.prepare_threshold is None
        for _ in range(8):  # More than psycopg's default prepare threshold (5).
            assert conn.execute("SELECT %s::int AS n", (1,)).fetchone()["n"] == 1


def _sql_literals(path: Path) -> str:
    """String literals of a module, excluding docstrings (which describe the audit)."""
    tree = ast.parse(path.read_text())
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return "\n".join(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    )


def test_no_session_state_in_source():
    """Nothing may rely on session state that transaction pooling breaks."""
    source = "\n".join(_sql_literals(p) for p in Path("src/clinic_voice").rglob("*.py"))
    for pattern in (
        r"pg_advisory_lock(_shared)?\(",
        r"\bLISTEN\b",
        r"TEMP(ORARY)? TABLE",
        r"cursor\(\s*name",
        r"(?:^|;)\s*SET (SESSION )?(?!LOCAL)[a-z_.]+ (TO|=)",
    ):
        assert not re.search(pattern, source, re.M), pattern


# --- Migration 018 and hardening ---------------------------------------------------------


@pytest.fixture
def scratch_database():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not configured; PostgreSQL required")
    base = conninfo_to_dict(url).get("dbname", "")
    if not base.endswith("_test"):
        pytest.fail("Refusing destructive integration tests against a database not ending _test")
    name = base.removesuffix("_test") + "_platform_migration_test"
    drop = sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(drop)
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield make_conninfo(url, dbname=name)
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(drop)


def test_018_applies_on_top_of_017(scratch_database, tmp_path):
    for path in sorted(Path("migrations").glob("0*.sql")):
        if path.name < "018":
            shutil.copy(path, tmp_path / path.name)
    migrate(scratch_database, tmp_path)
    seed(scratch_database)
    starts_at = datetime(2031, 1, 6, 4, 30, tzinfo=UTC)
    with connect(scratch_database) as conn:
        conn.execute(
            "INSERT INTO consents (id,clinic_id,patient_id,purpose,source) SELECT %s,%s,p.id,"
            "'reminder_call','voice_call' FROM (SELECT %s::uuid AS id) p WHERE false",
            (uuid4(), CLINIC, uuid4()),
        )
        conn.execute(
            "INSERT INTO appointments (id,clinic_id,doctor_id,service_id,starts_at,ends_at,"
            "blocks_until,status,idempotency_key,request_hash)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,'confirmed','pre-018-key','synthetic')",
            (
                uuid4(),
                CLINIC,
                DOCTOR_ID,
                SERVICE_ID,
                starts_at,
                starts_at + timedelta(minutes=15),
                starts_at + timedelta(minutes=15),
            ),
        )
    migrate(scratch_database, Path("migrations"))
    with connect(scratch_database) as conn:
        row = conn.execute("SELECT channel FROM appointments").fetchone()
        assert row["channel"] == "voice"
        clinic = conn.execute("SELECT public_enabled, public_slug FROM clinics").fetchone()
        assert clinic == {"public_enabled": False, "public_slug": None}
        missing = conn.execute(
            """SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relkind IN ('r','p') AND NOT c.relrowsecurity"""
        ).fetchall()
        assert missing == []


def test_every_table_has_row_level_security(database):
    """Migrations that add tables must enable RLS on them (ADR 0010). Enabling RLS is
    unconditional, so this holds on plain PostgreSQL as on Supabase."""
    with connect(database) as conn:
        rows = conn.execute(
            """SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relkind IN ('r','p')"""
        ).fetchall()
    assert len(rows) > 20
    assert [r["relname"] for r in rows if not r["relrowsecurity"]] == []
    # FORCE would subject the owner (the backend's only role) to RLS without policies.
    assert [r["relname"] for r in rows if r["relforcerowsecurity"]] == []


def test_owner_still_reads_and_writes_with_rls(env):
    with connect(env["url"]) as conn:
        user = conn.execute("SELECT current_user AS u").fetchone()["u"]
        owner = conn.execute(
            "SELECT pg_get_userbyid(relowner) AS o FROM pg_class WHERE relname='clinics'"
        ).fetchone()["o"]
        assert user == owner
    assert env["client"].get(BASE).status_code == 200


def _hardening_block() -> str:
    text = Path("migrations/018_platform.sql").read_text()
    blocks = re.findall(r"DO \$\$.*?\$\$;", text, re.S)
    (block,) = [b for b in blocks if "'anon'" in b]
    return block


def test_client_role_hardening_is_a_noop_without_the_roles(database):
    with connect(database) as conn:
        roles = conn.execute(
            "SELECT count(*) AS n FROM pg_roles WHERE rolname IN ('anon','authenticated')"
        ).fetchone()["n"]
        if roles:
            pytest.skip("this cluster has Supabase client roles")
        conn.execute(_hardening_block())  # Runs (and does nothing) on plain PostgreSQL.
        conn.rollback()


def test_client_role_hardening_revokes_existing_and_default_privileges(database):
    """The guarded block, run with uniquely named stand-ins for anon/authenticated inside a
    rolled-back transaction (roles are cluster-wide; nothing persists)."""
    suffix = uuid4().hex[:8]
    anon, authenticated = f"p1_anon_{suffix}", f"p1_auth_{suffix}"
    block = (
        _hardening_block()
        .replace("'anon'", f"'{anon}'")
        .replace("'authenticated'", f"'{authenticated}'")
    )
    with connect(database) as conn:
        for role in (anon, authenticated):
            conn.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
            conn.execute(
                sql.SQL("GRANT SELECT ON clinics, appointments TO {}").format(sql.Identifier(role))
            )
            conn.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {}"
                ).format(sql.Identifier(role))
            )
        assert conn.execute(
            "SELECT has_table_privilege(%s, 'clinics', 'SELECT') AS p", (anon,)
        ).fetchone()["p"]
        conn.execute(block)
        conn.execute("CREATE TABLE p1_future_table (id int)")
        for role in (anon, authenticated):
            for table in ("clinics", "appointments", "p1_future_table"):
                assert not conn.execute(
                    "SELECT has_table_privilege(%s, %s, 'SELECT') AS p", (role, table)
                ).fetchone()["p"]
        conn.rollback()


# --- Ledger calendar ---------------------------------------------------------------------


def test_ledger_confirms_through_the_outbox_and_derives_events_from_the_database(env):
    url = env["url"]
    result = reserve_direct(url, SLOT)
    appointment_id = result["appointment_id"]
    calendar_id = f"ledger:{DOCTOR_ID}"
    event_id = event_id_for(appointment_id, 1)
    with connect(url) as conn:
        assert LedgerCalendar(conn).get_event(calendar_id, event_id) is None  # Not recorded.
        assert run_batch(conn, "test", Ports(calendar_for=LedgerResolver())) == 1
        row = conn.execute(
            "SELECT status, calendar_event_id, calendar_id, calendar_account_id"
            " FROM appointments WHERE id=%s",
            (appointment_id,),
        ).fetchone()
        assert row == {
            "status": "confirmed",
            "calendar_event_id": event_id,
            "calendar_id": calendar_id,
            "calendar_account_id": None,
        }
        # A fresh port (another instance) sees the same event: state lives in the database.
        event = LedgerCalendar(conn).get_event(calendar_id, event_id)
        assert event.status == "confirmed" and event.start == SLOT
        assert LedgerCalendar(conn).get_event(f"ledger:{uuid4()}", event_id) is None
        cancel_locked(conn, lock_appointment(conn, CLINIC, appointment_id))
        conn.commit()
        assert run_batch(conn, "test", Ports(calendar_for=LedgerResolver())) == 1
        cancel = conn.execute(
            "SELECT status FROM outbox WHERE appointment_id=%s AND kind='calendar.cancel'",
            (appointment_id,),
        ).fetchone()
        assert cancel["status"] == "done"
        assert LedgerCalendar(conn).get_event(calendar_id, event_id).status == "cancelled"


def test_ledger_port_contract(database):
    with connect(database) as conn:
        port = LedgerCalendar(conn)
        event = CalendarEvent(event_id_for(uuid4(), 1), SLOT, SLOT + timedelta(minutes=15), "x")
        calendar_id = f"ledger:{DOCTOR_ID}"
        assert port.insert_event(calendar_id, event).status == "confirmed"
        assert port.insert_event(calendar_id, event) == port.insert_event(calendar_id, event)
        assert port.cancel_event(calendar_id, event.event_id) is None
        assert port.free_busy([calendar_id], SLOT, SLOT + timedelta(hours=1)) == []
        assert port.get_event(calendar_id, "not-an-event-id") is None
        for call in (
            lambda: port.get_event("doctor@example.com", event.event_id),
            lambda: port.insert_event("doctor@example.com", event),
            lambda: port.cancel_event("doctor@example.com", event.event_id),
        ):
            with pytest.raises(ProviderPermanent):
                call()
        assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_ledger_calendar_id_must_name_its_own_doctor(database):
    with connect(database) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "UPDATE doctors SET booking_calendar_id=%s WHERE id=%s",
            (f"ledger:{uuid4()}", DOCTOR_ID),
        )


def test_resolver_routes_ledger_doctors_and_delegates_others(env):
    url = env["url"]
    fake = FakeCalendar()

    def inner(conn, clinic_id, doctor_id, account_id=None):
        return fake, None

    inner.timeout_seconds = 5
    with connect(url) as conn:
        port, account = LedgerResolver(inner)(conn, CLINIC, DOCTOR_ID)
        assert account is None
        event = CalendarEvent(event_id_for(uuid4(), 1), SLOT, SLOT + timedelta(minutes=15), "x")
        port.insert_event(f"ledger:{DOCTOR_ID}", event)
        assert fake.calls == []  # The ledger id never reaches the external calendar.
        conn.execute("UPDATE doctors SET booking_calendar_id='doctor@example.com'")
        conn.commit()
        assert LedgerResolver()(conn, CLINIC, DOCTOR_ID) is None  # No Google: unmapped.
        port, _ = LedgerResolver(inner)(conn, CLINIC, DOCTOR_ID)
        port.insert_event("doctor@example.com", event)
        assert fake.calls == [("insert_event", "doctor@example.com", event.event_id)]
        assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_worker_ledger_mode_is_allowed_in_production(monkeypatch):
    settings = Settings(database_url="postgresql://u@db.example/clinic_voice", app_env="production")
    ports = build_ports("ledger", "postgresql://u@db.example/clinic_voice", settings)
    assert isinstance(ports.calendar_for, LedgerResolver)
    with pytest.raises(SystemExit):
        build_ports("fake", "postgresql://u@db.example/clinic_voice", settings)


# --- Worker tick -------------------------------------------------------------------------


def test_tick_is_404_without_a_configured_secret(env, monkeypatch):
    monkeypatch.delenv("WORKER_TICK_SECRET")
    get_settings.cache_clear()
    r = env["client"].post("/v1/internal/worker/tick", headers={"Authorization": "Bearer x"})
    assert r.status_code == 404


@pytest.mark.parametrize(
    "header",
    [None, "Bearer wrong", f"Basic {TICK_SECRET}", f"Bearer {TICK_SECRET}x", TICK_SECRET],
)
def test_tick_refuses_missing_or_wrong_secret(env, header):
    reserve_direct(env["url"], SLOT)
    headers = {} if header is None else {"Authorization": header}
    r = env["client"].post("/v1/internal/worker/tick", headers=headers)
    assert r.status_code == 401
    with connect(env["url"]) as conn:
        assert conn.execute("SELECT status FROM outbox").fetchone()["status"] == "pending"


def test_tick_runs_due_jobs_and_returns_counts_only(env):
    result = reserve_direct(env["url"], SLOT)
    r = env["client"].post(
        "/v1/internal/worker/tick", headers={"Authorization": f"Bearer {TICK_SECRET}"}
    )
    assert r.status_code == 200
    assert r.json() == {"claimed": 1, "housekeeping": "ran"}
    assert r.headers["cache-control"] == "no-store"
    with connect(env["url"]) as conn:
        status = conn.execute(
            "SELECT status FROM appointments WHERE id=%s", (result["appointment_id"],)
        ).fetchone()["status"]
    assert status == "confirmed"


def test_tick_short_secret_is_a_configuration_error(monkeypatch):
    with pytest.raises(ValueError):
        Settings(database_url="postgresql://u@h/d", worker_tick_secret="short")


def test_tick_stops_at_its_budget_and_releases_unstarted_jobs(env):
    url = env["url"]
    for hour in range(10, 16):
        reserve_direct(url, datetime.combine(DAY, time(hour), IST))
    readings = iter([0.0, 0.0, 0.0])
    clock = lambda: next(readings, 100.0)  # noqa: E731 - spent after the first job.
    ran = []
    result = tick.run_tick(
        url,
        Ports(calendar_for=LedgerResolver()),
        10.0,
        housekeeping=lambda conn, ports: ran.append(True),
        clock=clock,
    )
    assert result == {"claimed": tick.TICK_BATCH, "housekeeping": "ran"} and ran
    with connect(url) as conn:
        rows = conn.execute(
            "SELECT status, count(*) AS n FROM outbox GROUP BY status ORDER BY status"
        ).fetchall()
        assert {r["status"]: r["n"] for r in rows} == {"done": 1, "pending": 5}
        released = conn.execute(
            "SELECT count(*) AS n FROM outbox WHERE status='pending' AND attempts=1"
            " AND max_attempts=9"
        ).fetchone()["n"]
    assert released == tick.TICK_BATCH - 1


def test_tick_housekeeping_failure_does_not_fail_the_tick(env):
    def broken(conn, ports):
        raise RuntimeError("synthetic")

    result = tick.run_tick(env["url"], Ports(calendar_for=LedgerResolver()), 2.0, broken)
    assert result == {"claimed": 0, "housekeeping": "failed"}


def test_tick_ports_without_providers_leave_bolna_and_messaging_jobs(env):
    ports = tick.build_ports(get_settings())
    assert ports.bolna is None and ports.messaging is None
    assert isinstance(ports.calendar_for, LedgerResolver)


# --- Public API: catalogue ---------------------------------------------------------------


def test_public_profile_and_catalogue(env):
    client = env["client"]
    r = client.get(BASE)
    assert r.status_code == 200
    assert r.json() == {
        "name": "Synthetic Demo Clinic",
        "tagline": "Synthetic tagline",
        "timezone": "Asia/Kolkata",
        "address": "",
        "map_url": None,
        "phone_display": "",
        "email": "",
        "hours_note": "",
        "about": "",
        "voice_number": None,
        "chat_enabled": False,
    }
    assert r.headers["cache-control"] == "public, max-age=60"
    services = client.get(f"{BASE}/services").json()
    assert services == [
        {
            "id": str(SERVICE_ID),
            "name": "Consultation",
            "description": "",
            "duration_minutes": 15,
            "colour": None,
        }
    ]
    doctors = client.get(f"{BASE}/doctors").json()
    assert doctors == [
        {
            "id": str(DOCTOR_ID),
            "name": "Demo Doctor",
            "public_bio": "",
            "photo_url": None,
            "colour": None,
            "service_ids": [str(SERVICE_ID)],
        }
    ]
    # No internal fields (calendar ids, settings) anywhere.
    assert "ledger" not in r.text + str(services) + str(doctors)


@pytest.mark.parametrize("path", ["", "/services", "/doctors"])
def test_unknown_or_private_clinic_is_404(env, path):
    client = env["client"]
    assert_envelope(client.get(f"/v1/public/clinics/no-such-clinic{path}"), 404, "not_found")
    with connect(env["url"]) as conn:
        conn.execute("UPDATE clinics SET public_enabled=false WHERE id=%s", (CLINIC,))
    assert_envelope(client.get(f"{BASE}{path}"), 404, "not_found")


def test_unknown_public_route_uses_the_envelope(env):
    assert_envelope(env["client"].get(f"{BASE}/nothing-here"), 404, "not_found")


def test_hidden_service_is_not_public(env):
    with connect(env["url"]) as conn:
        conn.execute("UPDATE services SET public=false WHERE id=%s", (SERVICE_ID,))
    assert env["client"].get(f"{BASE}/services").json() == []
    assert env["client"].get(f"{BASE}/doctors").json()[0]["service_ids"] == []


def test_catalogue_groups_shared_offerings(env):
    other_doctor, other_row = uuid4(), uuid4()
    with connect(env["url"]) as conn:
        conn.execute(
            "INSERT INTO doctors (id,clinic_id,name) VALUES (%s,%s,'Second Doctor')",
            (other_doctor, CLINIC),
        )
        conn.execute(
            "INSERT INTO services (id,clinic_id,doctor_id,name,duration_minutes,catalog_id)"
            " VALUES (%s,%s,%s,'Consultation',15,%s)",
            (other_row, CLINIC, other_doctor, SERVICE_ID),
        )
        for day in range(7):
            conn.execute(
                "INSERT INTO working_hours VALUES (%s,%s,%s,'09:00','10:00')",
                (CLINIC, other_doctor, day),
            )
    client = env["client"]
    assert [s["id"] for s in client.get(f"{BASE}/services").json()] == [str(SERVICE_ID)]
    slots = client.get(
        f"{BASE}/slots", params={"service_id": str(SERVICE_ID), "date": DAY.isoformat()}
    ).json()["slots"]
    assert {s["doctor_id"] for s in slots} == {str(DOCTOR_ID), str(other_doctor)}
    r = client.post(
        f"{BASE}/bookings",
        json=booking(datetime.combine(DAY, time(9, 15), IST), doctor_id=str(other_doctor)),
    )
    assert r.status_code == 202 and r.json()["doctor_name"] == "Second Doctor"
    assert appointment_by_reference(env["url"], r.json()["reference"])["service_id"] == other_row


def test_slots(env):
    client = env["client"]
    params = {"service_id": str(SERVICE_ID), "date": DAY.isoformat(), "days": 2}
    r = client.get(f"{BASE}/slots", params=params)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["timezone"] == "Asia/Kolkata"
    starts = [datetime.fromisoformat(s["starts_at"]) for s in body["slots"]]
    assert len(starts) == 2 * 32  # 09:00-17:00 in 15-minute starts, two days.
    assert starts[0] == datetime.combine(DAY, time(9), IST)
    first = body["slots"][0]
    assert datetime.fromisoformat(first["ends_at"]) - starts[0] == timedelta(minutes=15)
    reserve_direct(env["url"], datetime.combine(DAY, time(9), IST))
    again = client.get(f"{BASE}/slots", params=params).json()["slots"]
    assert len(again) == len(starts) - 1  # The ledger's own booking is busy.


@pytest.mark.parametrize(
    ("params", "status", "code"),
    [
        ({"date": (NOW.date() - timedelta(days=1)).isoformat()}, 422, "invalid_date"),
        ({"date": (NOW.date() + timedelta(days=400)).isoformat()}, 422, "invalid_date"),
        ({"date": DAY.isoformat(), "days": 8}, 422, "invalid_request"),
        ({"date": "2031-02-30"}, 422, "invalid_request"),
        ({"date": DAY.isoformat(), "service_id": str(uuid4())}, 404, "not_found"),
        ({"date": DAY.isoformat(), "doctor_id": str(uuid4())}, 404, "not_found"),
    ],
)
def test_slot_refusals(env, params, status, code):
    query = {"service_id": str(SERVICE_ID), **params}
    assert_envelope(env["client"].get(f"{BASE}/slots", params=query), status, code)


# --- Public API: bookings ----------------------------------------------------------------


def test_web_booking_confirms_inline_with_the_ledger(env):
    url = env["url"]
    r = env["client"].post(f"{BASE}/bookings", json=booking(notes=NOTES))
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "confirmed"
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{8}", body["reference"])
    assert datetime.fromisoformat(body["starts_at"]) == SLOT
    assert body["starts_at"].endswith("+05:30")
    assert (body["doctor_name"], body["service_name"]) == ("Demo Doctor", "Consultation")
    assert r.headers["cache-control"] == "no-store"
    row = appointment_by_reference(url, body["reference"])
    assert row["channel"] == "web" and row["status"] == "confirmed"
    assert row["calendar_id"] == f"ledger:{DOCTOR_ID}"
    assert create_job(url, row["id"])["status"] == "done"
    keyring = Keyring.from_setting(KEYS)
    with connect(url) as conn:
        patient = conn.execute(
            "SELECT * FROM patients WHERE id=%s", (row["patient_id"],)
        ).fetchone()
        consents = conn.execute(
            "SELECT purpose, source FROM consents WHERE patient_id=%s ORDER BY purpose",
            (row["patient_id"],),
        ).fetchall()
        audit = conn.execute("SELECT action, target_id FROM audit_events").fetchall()
    name = decrypt_field(
        keyring, CLINIC, patient["id"], "name", patient["name_key_id"], patient["name_ciphertext"]
    )
    assert name == NAME
    assert bytes(patient["phone_hash"]) == phone_hash(decode_hash_key(HASH_KEY), PHONE)
    assert NAME.encode() not in bytes(patient["name_ciphertext"])
    assert [c["source"] for c in consents] == ["web"] * 3
    notes = keyring.decrypt(
        row["notes_key_id"], row["notes_ciphertext"], public.notes_aad(CLINIC, row["id"])
    )
    assert notes.decode() == NOTES
    assert {"action": "public.booking_created", "target_id": row["id"]} in audit


def test_web_booking_never_attaches_to_an_existing_patient(env):
    client = env["client"]
    first = client.post(f"{BASE}/bookings", json=booking()).json()
    second = client.post(
        f"{BASE}/bookings", json=booking(datetime.combine(DAY, time(11), IST))
    ).json()
    a = appointment_by_reference(env["url"], first["reference"])
    b = appointment_by_reference(env["url"], second["reference"])
    assert a["patient_id"] != b["patient_id"]


def test_web_booking_is_pending_sync_when_inline_job_fails(env, monkeypatch):
    class Failing:
        timeout_seconds = 5

        def __call__(self, conn, clinic_id, doctor_id, account_id=None):
            conn.commit()
            raise ProviderRetryable()

    monkeypatch.setattr(routes_public, "ports_for", lambda settings: Ports(calendar_for=Failing()))
    r = env["client"].post(f"{BASE}/bookings", json=booking())
    assert r.status_code == 202 and r.json()["status"] == "pending_sync"
    row = appointment_by_reference(env["url"], r.json()["reference"])
    job = create_job(env["url"], row["id"])
    assert (row["status"], job["status"], job["last_error"]) == (
        "pending_sync",
        "pending",
        "retryable",
    )
    # The tick, with working ports, confirms it later.
    with connect(env["url"]) as conn:
        conn.execute("UPDATE outbox SET available_at=now()")
    tick.run_tick(env["url"], Ports(calendar_for=LedgerResolver()), 2.0, lambda c, p: None)
    assert appointment_by_reference(env["url"], r.json()["reference"])["status"] == "confirmed"


def test_web_booking_without_inline_budget_waits_for_the_tick(env, monkeypatch):
    monkeypatch.setenv("INLINE_CONFIRM_BUDGET_SECONDS", "0")
    get_settings.cache_clear()
    r = env["client"].post(f"{BASE}/bookings", json=booking())
    assert r.json()["status"] == "pending_sync"


def test_google_mapped_doctor_is_never_confirmed_inline(env):
    with connect(env["url"]) as conn:
        conn.execute("UPDATE doctors SET booking_calendar_id='doctor@example.com'")
    r = env["client"].post(f"{BASE}/bookings", json=booking())
    assert r.status_code == 202 and r.json()["status"] == "pending_sync"
    row = appointment_by_reference(env["url"], r.json()["reference"])
    job = create_job(env["url"], row["id"])
    assert (job["status"], job["attempts"]) == ("pending", 0)  # Untouched, left to the tick.


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"phone": "9000000101"}, "invalid_request"),
        ({"phone": "+91 90000 00101"}, "invalid_request"),
        ({"phone": "+0123456789"}, "invalid_request"),
        ({"name": ""}, "invalid_request"),
        ({"name": "x" * 121}, "invalid_request"),
        ({"name": "Line\nBreak"}, "invalid_request"),
        ({"notes": "n" * 501}, "invalid_request"),
        ({"starts_at": "2031-03-05T10:00:00"}, "invalid_request"),
        ({"consent_contact": "yes"}, "invalid_request"),
        ({"extra": "field"}, "invalid_request"),
        ({"consent_contact": False}, "consent_required"),
    ],
)
def test_booking_validation(env, change, code):
    r = env["client"].post(f"{BASE}/bookings", json=booking(**change))
    assert_envelope(r, 422, code)
    for value in change.values():
        if isinstance(value, str) and len(value) > 3:
            assert value not in r.text
    with connect(env["url"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM appointments").fetchone()["n"] == 0


@pytest.mark.parametrize(
    ("change", "status", "code"),
    [
        (
            {"starts_at": datetime.combine(DAY, time(10, 7), IST).isoformat()},
            409,
            "slot_unavailable",
        ),
        ({"starts_at": datetime.combine(DAY, time(20), IST).isoformat()}, 409, "slot_unavailable"),
        ({"starts_at": (NOW - timedelta(hours=1)).isoformat()}, 409, "slot_unavailable"),
        ({"starts_at": (NOW + timedelta(minutes=30)).isoformat()}, 409, "slot_unavailable"),
        ({"starts_at": (NOW + timedelta(days=400)).isoformat()}, 409, "slot_unavailable"),
        ({"doctor_id": str(uuid4())}, 404, "not_found"),
        ({"service_id": str(uuid4())}, 404, "not_found"),
    ],
)
def test_booking_refuses_times_that_are_not_offered(env, change, status, code):
    assert_envelope(env["client"].post(f"{BASE}/bookings", json=booking(**change)), status, code)


def test_booking_refuses_another_clinics_doctor(env):
    other_doctor = uuid4()
    with connect(env["url"]) as conn:
        conn.execute("INSERT INTO clinics (id,name) VALUES (%s,'Other')", (OTHER_CLINIC,))
        conn.execute(
            "INSERT INTO doctors (id,clinic_id,name) VALUES (%s,%s,'Other Doctor')",
            (other_doctor, OTHER_CLINIC),
        )
    r = env["client"].post(f"{BASE}/bookings", json=booking(doctor_id=str(other_doctor)))
    assert_envelope(r, 404, "not_found")


def test_booking_taken_slot_is_409(env):
    client = env["client"]
    assert client.post(f"{BASE}/bookings", json=booking()).status_code == 202
    r = client.post(f"{BASE}/bookings", json=booking(phone="+919000000102"))
    assert_envelope(r, 409, "slot_unavailable")


def test_concurrent_bookings_of_one_slot_one_wins(env):
    settings = get_settings()
    with connect(env["url"]) as conn:
        clinic = public.clinic_by_slug(conn, SLUG)
    barrier = threading.Barrier(4)
    outcomes = []

    def attempt(n):
        request = PublicBookingRequest(**booking(phone=f"+91900000020{n}"))
        barrier.wait()
        try:
            outcomes.append(
                public.book(env["url"], settings, clinic, request, f"10.0.0.{n}")["status"]
            )
        except PublicError as error:
            outcomes.append(error.code)

    threads = [threading.Thread(target=attempt, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["pending_sync"] + ["slot_unavailable"] * 3
    with connect(env["url"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM appointments").fetchone()["n"] == 1


def test_idempotency_key_replays_and_binds_the_contact(env):
    client = env["client"]
    headers = {"Idempotency-Key": "web-form-submission-0001"}
    first = client.post(f"{BASE}/bookings", json=booking(), headers=headers)
    again = client.post(f"{BASE}/bookings", json=booking(), headers=headers)
    assert first.status_code == again.status_code == 202
    assert first.json()["reference"] == again.json()["reference"]
    other = client.post(f"{BASE}/bookings", json=booking(phone="+919000000199"), headers=headers)
    assert_envelope(other, 409, "idempotency_conflict")
    assert other.json()["error"]["message"] and first.json()["reference"] not in other.text


def test_tools_api_refuses_reserved_web_keys(env):
    from clinic_voice.api import tool_caller
    from clinic_voice.deps import ToolCaller

    app.dependency_overrides[tool_caller] = lambda: ToolCaller(CLINIC, UUID(int=0))
    try:
        r = env["client"].post(
            "/v1/tools/reserve-slot",
            headers={"Idempotency-Key": "web:0123456789"},
            json={
                "doctor_id": str(DOCTOR_ID),
                "service_id": str(SERVICE_ID),
                "starts_at": SLOT.isoformat(),
            },
        )
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 422


def test_booking_rate_limits_per_ip_and_per_phone(env, monkeypatch):
    monkeypatch.setenv("PUBLIC_BOOKING_IP_PER_HOUR", "2")
    monkeypatch.setenv("PUBLIC_BOOKING_PHONE_PER_DAY", "1")
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
    get_settings.cache_clear()
    client = env["client"]

    def post(hour, phone, address):
        return client.post(
            f"{BASE}/bookings",
            json=booking(datetime.combine(DAY, time(hour), IST), phone=phone),
            headers={"X-Forwarded-For": f"198.51.100.200, {address}"},
        )

    assert post(10, "+919000000301", "203.0.113.1").status_code == 202
    assert_envelope(post(11, "+919000000301", "203.0.113.2"), 429, "rate_limited")  # phone
    assert post(12, "+919000000302", "203.0.113.1").status_code == 202
    assert_envelope(post(13, "+919000000303", "203.0.113.1"), 429, "rate_limited")  # IP
    # Only the trusted hop counts: a spoofed left-most entry does not change the bucket.
    assert post(14, "+919000000304", "203.0.113.3").status_code == 202
    with connect(env["url"]) as conn:
        rows = conn.execute("SELECT key_hash FROM public_rate_limits").fetchall()
    stored = b"".join(bytes(r["key_hash"]) for r in rows)
    assert b"203.0.113" not in stored and b"9000000301" not in stored


def test_public_writes_need_the_encryption_settings(env, monkeypatch):
    monkeypatch.setenv("PHONE_HASH_KEY", "")
    get_settings.cache_clear()
    client = env["client"]
    assert_envelope(client.post(f"{BASE}/bookings", json=booking()), 503, "unavailable")
    assert_envelope(
        client.post(f"{BASE}/callbacks", json={"name": NAME, "phone": PHONE}), 503, "unavailable"
    )
    assert_envelope(
        client.post(f"{BASE}/bookings/lookup", json={"reference": "ABCDEFGH", "phone": PHONE}),
        503,
        "unavailable",
    )


# --- Public API: lookup ------------------------------------------------------------------


def test_lookup_by_reference_and_phone(env):
    client = env["client"]
    created = client.post(f"{BASE}/bookings", json=booking()).json()
    spaced = " ".join(created["reference"]).lower()
    r = client.post(f"{BASE}/bookings/lookup", json={"reference": spaced, "phone": PHONE})
    assert r.status_code == 200
    assert r.json() == {**created, "status": "confirmed"}


def test_lookup_mismatches_are_one_uniform_404(env):
    client = env["client"]
    reference = client.post(f"{BASE}/bookings", json=booking()).json()["reference"]
    with connect(env["url"]) as conn:
        conn.execute(
            "INSERT INTO clinics (id,name,public_slug,public_enabled) VALUES (%s,'Other','other-clinic',true)",
            (OTHER_CLINIC,),
        )
    cases = [
        (BASE, {"reference": reference, "phone": "+919000000999"}),  # wrong phone
        (BASE, {"reference": "ZZZZZZZZ", "phone": PHONE}),  # unknown reference
        (BASE, {"reference": "not a reference", "phone": PHONE}),  # malformed
        ("/v1/public/clinics/other-clinic", {"reference": reference, "phone": PHONE}),
    ]
    bodies = set()
    for base, body in cases:
        r = client.post(f"{base}/bookings/lookup", json=body)
        assert_envelope(r, 404, "not_found")
        bodies.add(r.text)
    assert len(bodies) == 1


def test_lookup_rate_limit(env, monkeypatch):
    monkeypatch.setenv("PUBLIC_LOOKUP_IP_PER_HOUR", "2")
    get_settings.cache_clear()
    body = {"reference": "ZZZZZZZZ", "phone": PHONE}
    for _ in range(2):
        assert env["client"].post(f"{BASE}/bookings/lookup", json=body).status_code == 404
    assert_envelope(env["client"].post(f"{BASE}/bookings/lookup", json=body), 429, "rate_limited")


# --- Public API: callbacks ---------------------------------------------------------------


def test_callback_request_is_stored_encrypted(env):
    r = env["client"].post(
        f"{BASE}/callbacks", json={"name": NAME, "phone": PHONE, "reason": NOTES}
    )
    assert r.status_code == 201 and r.json()["status"] == "open"
    request_id = UUID(r.json()["id"])
    with connect(env["url"]) as conn:
        row = conn.execute("SELECT * FROM callback_requests WHERE id=%s", (request_id,)).fetchone()
        audit = conn.execute("SELECT action, target_id FROM audit_events").fetchall()
    assert (row["source"], row["status"], row["patient_id"]) == ("web", "open", None)
    keyring = Keyring.from_setting(KEYS)
    for field, expected in (("name", NAME), ("phone", PHONE), ("reason", NOTES)):
        blob = bytes(row[f"{field}_ciphertext"])
        assert expected.encode() not in blob
        aad = public.callback_aad(CLINIC, request_id, field)
        assert keyring.decrypt(row[f"{field}_key_id"], blob, aad).decode() == expected
    assert {"action": "public.callback_requested", "target_id": request_id} in audit


def test_callback_validation_and_rate_limit(env, monkeypatch):
    monkeypatch.setenv("PUBLIC_CALLBACK_IP_PER_HOUR", "1")
    get_settings.cache_clear()
    client = env["client"]
    assert_envelope(
        client.post(f"{BASE}/callbacks", json={"name": NAME, "phone": "12345"}),
        422,
        "invalid_request",
    )
    assert client.post(f"{BASE}/callbacks", json={"name": NAME, "phone": PHONE}).status_code == 201
    assert_envelope(
        client.post(f"{BASE}/callbacks", json={"name": NAME, "phone": "+919000000777"}),
        429,
        "rate_limited",
    )


def test_no_patient_data_in_logs(env, caplog):
    caplog.set_level(logging.DEBUG)
    client = env["client"]
    created = client.post(f"{BASE}/bookings", json=booking(notes=NOTES)).json()
    client.post(f"{BASE}/bookings/lookup", json={"reference": created["reference"], "phone": PHONE})
    client.post(f"{BASE}/bookings/lookup", json={"reference": "ZZZZZZZZ", "phone": PHONE})
    client.post(f"{BASE}/callbacks", json={"name": NAME, "phone": PHONE, "reason": NOTES})
    client.post(f"{BASE}/bookings", json=booking(phone="bad-" + PHONE))
    text = caplog.text
    assert "public booking appointment=" in text
    for secret in (NAME, PHONE, PHONE[1:], NOTES, created["reference"]):
        assert secret not in text


# --- seed-platform -----------------------------------------------------------------------


@pytest.fixture
def empty(database, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database)
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEYS", KEYS)
    monkeypatch.setenv("PHONE_HASH_KEY", HASH_KEY)
    get_settings.cache_clear()
    with connect(database) as conn:
        conn.execute("TRUNCATE public_rate_limits, clinics CASCADE")
    yield database
    get_settings.cache_clear()


def test_seed_refuses_a_database_with_other_clinics(database, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database)
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEYS", KEYS)
    monkeypatch.setenv("PHONE_HASH_KEY", HASH_KEY)
    get_settings.cache_clear()
    with pytest.raises(seed_platform.SeedRefused):
        seed_platform.seed(database, get_settings())
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) AS n FROM clinics").fetchone()["n"] == 1
    get_settings.cache_clear()


def test_seed_needs_the_encryption_settings(empty, monkeypatch):
    monkeypatch.setenv("PHONE_HASH_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(seed_platform.SeedRefused):
        seed_platform.seed(empty, get_settings())


def test_seed_platform_is_synthetic_consistent_and_idempotent(empty):
    first = seed_platform.seed(empty, get_settings())
    assert 60 <= first["past"] + first["future"] <= 75
    assert first["callbacks"] == 4 and first["cancelled"] >= 1
    clinic_id = seed_platform.CLINIC_ID
    keyring = Keyring.from_setting(KEYS)
    zone = ZoneInfo("Asia/Kolkata")
    today = datetime.now(UTC).astimezone(zone).date()
    with connect(empty) as conn:
        clinic = conn.execute("SELECT * FROM clinics").fetchone()
        assert (clinic["public_slug"], clinic["timezone"], clinic["seed_tag"]) == (
            "aster-dental",
            "Asia/Kolkata",
            "platform-seed",
        )
        assert clinic["outbound_enabled"] is False and clinic["public_enabled"] is True
        assert "Indiranagar" in clinic["public_address"]
        assert conn.execute("SELECT count(*) AS n FROM staff_users").fetchone()["n"] == 0
        doctors = conn.execute("SELECT id, booking_calendar_id, colour FROM doctors").fetchall()
        assert len(doctors) == 5
        assert all(d["booking_calendar_id"] == f"ledger:{d['id']}" and d["colour"] for d in doctors)
        catalogue = conn.execute(
            "SELECT count(DISTINCT catalog_id) AS n FROM services WHERE clinic_id=%s", (clinic_id,)
        ).fetchone()["n"]
        assert catalogue == 10
        exceptions = conn.execute(
            "SELECT kind, label, starts_at FROM schedule_exceptions"
        ).fetchall()
        assert {(e["kind"], e["label"]) for e in exceptions} == {
            ("holiday", "Diwali"),
            ("leave", "Annual leave"),
        }
        diwali = next(e for e in exceptions if e["label"] == "Diwali")
        assert diwali["starts_at"] == datetime(2026, 11, 8, tzinfo=zone)
        rows = conn.execute(
            """SELECT a.*, p.phone_ciphertext, p.phone_key_id, p.id AS pid FROM appointments a
            JOIN patients p ON p.clinic_id=a.clinic_id AND p.id=a.patient_id"""
        ).fetchall()
        jobs = conn.execute("SELECT status, count(*) AS n FROM outbox GROUP BY status").fetchall()
    assert {j["status"] for j in jobs} == {"done"}
    assert {r["channel"] for r in rows} == {"voice", "web", "chat", "staff"}
    assert {r["status"] for r in rows} == {"confirmed", "cancelled"}
    days = {r["starts_at"].astimezone(zone).date() for r in rows}
    assert min(days) >= today - timedelta(days=7) and max(days) <= today + timedelta(days=14)
    assert any(d < today for d in days) and any(d > today for d in days)
    for row in rows:
        if row["status"] == "confirmed":
            assert row["calendar_event_id"] == event_id_for(row["id"], row["version"])
            assert row["calendar_id"] == f"ledger:{row['doctor_id']}"
        phone = decrypt_field(
            keyring, clinic_id, row["pid"], "phone", row["phone_key_id"], row["phone_ciphertext"]
        )
        assert re.fullmatch(r"\+91900000\d{4}", phone)
        local = row["starts_at"].astimezone(zone)
        assert local.date() != date(2026, 11, 8) or row["status"] == "cancelled"
    # Idempotent: nothing is added or duplicated.
    again = seed_platform.seed(empty, get_settings())
    assert again == {"past": 0, "future": 0, "cancelled": 0, "callbacks": 0, "jobs": 0}
    with connect(empty) as conn:
        assert conn.execute("SELECT count(*) AS n FROM appointments").fetchone()["n"] == len(rows)
        assert conn.execute("SELECT count(*) AS n FROM doctors").fetchone()["n"] == 5
        assert conn.execute("SELECT count(*) AS n FROM schedule_exceptions").fetchone()["n"] == 2
        assert conn.execute("SELECT count(*) AS n FROM callback_requests").fetchone()["n"] == 4
        conn.execute("INSERT INTO clinics (id,name) VALUES (%s,'Real clinic')", (uuid4(),))
    with pytest.raises(seed_platform.SeedRefused):
        seed_platform.seed(empty, get_settings())


def test_seeded_clinic_serves_the_public_api(empty, monkeypatch):
    seed_platform.seed(empty, get_settings())
    with TestClient(app) as client:
        profile = client.get("/v1/public/clinics/aster-dental").json()
        assert profile["name"] == "Aster Dental Studio" and profile["phone_display"].startswith(
            "+91 90000"
        )
        services = client.get("/v1/public/clinics/aster-dental/services").json()
        doctors = client.get("/v1/public/clinics/aster-dental/doctors").json()
    assert len(services) == 10 and len(doctors) == 5
    assert all(s["colour"] and s["description"] for s in services)
    assert {sid for d in doctors for sid in d["service_ids"]} == {s["id"] for s in services}
