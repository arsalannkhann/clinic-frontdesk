"""Staff platform (P2): /v1/staff/me, calendar, bookings, callbacks and "Call now",
outbound-status, conversations, stats, and the admin public profile. Synthetic data only;
the Bolna port is the in-memory fake."""

import hashlib
import logging
from datetime import UTC, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from clinic_voice import chat, routes_staff, service, staff, tick
from clinic_voice.api import app
from clinic_voice.cli import DEMO_CLINIC_ID as CLINIC
from clinic_voice.cli import DOCTOR_ID, SERVICE_ID
from clinic_voice.config import get_settings
from clinic_voice.crypto import Keyring, decode_hash_key, generate_key, phone_hash
from clinic_voice.db import connect
from clinic_voice.providers.bolna import FakeBolna
from clinic_voice.providers.calendar import ProviderPermanent
from clinic_voice.public import store_callback

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2031, 3, 3, 11, 0, tzinfo=IST)  # A Monday, outside default quiet hours.
SLOT = datetime.combine(NOW.date() + timedelta(days=1), time(10, 0), IST)
KEYS = "k1:" + generate_key()
HASH_KEY = generate_key()
PASSWORD = "synthetic-password-01"
NAME = "Synthetic Staffpatient"
PHONE = "+919000000711"
AGENT = "11111111-2222-4333-8444-555555555555"
FROM_NUMBER = "+919000001999"


@pytest.fixture(autouse=True)
def cheap_hashing(monkeypatch):
    monkeypatch.setattr(staff, "SCRYPT_N", 2**10)


@pytest.fixture
def env(database, monkeypatch):
    clock = {"now": NOW.astimezone(UTC)}
    monkeypatch.setattr(service, "utc_now", lambda: clock["now"])
    monkeypatch.setenv("DATABASE_URL", database)
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEYS", KEYS)
    monkeypatch.setenv("PHONE_HASH_KEY", HASH_KEY)
    get_settings.cache_clear()
    tick._ports_cache.clear()
    bolna = FakeBolna()
    port = {"bolna": bolna}
    monkeypatch.setattr(routes_staff, "bolna_port", lambda settings: port["bolna"])
    with connect(database) as conn:
        conn.execute(
            "UPDATE clinics SET timezone='Asia/Kolkata', public_slug='demo-clinic' WHERE id=%s",
            (CLINIC,),
        )
        conn.execute(
            "UPDATE doctors SET booking_calendar_id='ledger:' || id::text WHERE id=%s",
            (DOCTOR_ID,),
        )
    with TestClient(app) as client:
        yield {"client": client, "url": database, "bolna": bolna, "port": port, "clock": clock}
    get_settings.cache_clear()
    tick._ports_cache.clear()


def login(env, role="owner", clinic=CLINIC):
    email = f"{role}-{uuid4().hex[:8]}@example.test"
    with connect(env["url"]) as conn:
        user = staff.create_user(conn, clinic, email, role, PASSWORD)
    r = env["client"].post("/v1/staff/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"], "_id": str(user["id"])}


def h(headers):
    return {k: v for k, v in headers.items() if not k.startswith("_")}


def other_clinic(url):
    clinic, doctor, svc = uuid4(), uuid4(), uuid4()
    with connect(url) as conn:
        conn.execute(
            "INSERT INTO clinics (id,name,timezone) VALUES (%s,'Synthetic Other','Asia/Kolkata')",
            (clinic,),
        )
        conn.execute(
            "INSERT INTO doctors (id,clinic_id,name) VALUES (%s,%s,'Other')", (doctor, clinic)
        )
        conn.execute(
            "INSERT INTO services (id,clinic_id,doctor_id,name,duration_minutes)"
            " VALUES (%s,%s,%s,'Other',15)",
            (svc, clinic, doctor),
        )
    return clinic, doctor, svc


def actions(url):
    with connect(url) as conn:
        return [r["action"] for r in conn.execute("SELECT action FROM audit_events ORDER BY id")]


def assert_code(response, status, code):
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code


def book(env, headers, starts_at=SLOT, key=None, **changes):
    body = {
        "service_id": str(SERVICE_ID),
        "doctor_id": str(DOCTOR_ID),
        "starts_at": starts_at if isinstance(starts_at, str) else starts_at.isoformat(),
        "name": NAME,
        "phone": PHONE,
        **changes,
    }
    extra = {"Idempotency-Key": key} if key else {}
    return env["client"].post("/v1/staff/bookings", json=body, headers={**h(headers), **extra})


def callback(url, clinic=CLINIC, phone=PHONE, synthetic=False, reason="Synthetic reason"):
    keyring = Keyring.from_setting(KEYS)
    with connect(url) as conn:
        request_id, _ = store_callback(
            conn, keyring, decode_hash_key(HASH_KEY), clinic, NAME, phone, reason, "web"
        )
        if synthetic:
            conn.execute("UPDATE callback_requests SET synthetic=true WHERE id=%s", (request_id,))
    return request_id


def enable_outbound(url, **overrides):
    values = {
        "outbound_enabled": True,
        "bolna_outbound_agent_id": AGENT,
        "bolna_outbound_from_number": FROM_NUMBER,
        **overrides,
    }
    sets = ", ".join(f"{k}=%s" for k in values)
    with connect(url) as conn:
        conn.execute(
            f"UPDATE clinics SET {sets} WHERE id=%s",  # noqa: S608
            (*values.values(), CLINIC),
        )


def call_now(env, headers, request_id):
    return env["client"].post(f"/v1/staff/callbacks/{request_id}/call", headers=h(headers))


def callback_row(url, request_id):
    with connect(url) as conn:
        return conn.execute("SELECT * FROM callback_requests WHERE id=%s", (request_id,)).fetchone()


# --- Me --------------------------------------------------------------------------------------


def test_me_includes_the_clinic(env):
    owner = login(env)
    r = env["client"].get("/v1/staff/me", headers=h(owner))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"id", "email", "role", "clinic"}
    assert body["role"] == "owner" and body["id"] == owner["_id"]
    assert body["clinic"] == {
        "id": str(CLINIC),
        "name": "Synthetic Demo Clinic",
        "timezone": "Asia/Kolkata",
        "public_slug": "demo-clinic",
    }


# --- Staff bookings and the calendar ---------------------------------------------------------


def test_staff_booking_is_created_confirmed_and_audited(env):
    owner = login(env)
    r = book(env, owner, notes="Synthetic note")
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"reference", "status", "starts_at", "doctor_name", "service_name"}
    assert body["status"] == "confirmed"
    assert datetime.fromisoformat(body["starts_at"]) == SLOT
    with connect(env["url"]) as conn:
        appointment = conn.execute("SELECT * FROM appointments").fetchone()
        consents = conn.execute("SELECT count(*) AS n FROM consents").fetchone()["n"]
        event = conn.execute(
            "SELECT actor_id FROM audit_events WHERE action='staff.booking_created'"
        ).fetchone()
    assert appointment["channel"] == "staff" and appointment["notes_ciphertext"] is not None
    assert consents == 0  # consent_contact defaults to false.
    assert str(event["actor_id"]) == owner["_id"]


def test_staff_booking_records_consent_when_given(env):
    admin = login(env, "admin")
    r = book(env, admin, consent_contact=True)
    assert r.status_code == 201, r.text
    with connect(env["url"]) as conn:
        sources = {r["source"] for r in conn.execute("SELECT source FROM consents")}
    assert sources == {"staff"}


def test_staff_booking_replays_its_idempotency_key(env):
    owner = login(env)
    first = book(env, owner, key="staff-key-0001")
    again = book(env, owner, key="staff-key-0001")
    assert first.status_code == again.status_code == 201
    assert first.json() == again.json()
    other = book(env, owner, key="staff-key-0001", name="Synthetic Different")
    assert other.status_code == 409, other.text
    with connect(env["url"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM appointments").fetchone()["n"] == 1


def test_staff_booking_refusals(env):
    owner, viewer = login(env), login(env, "viewer")
    assert book(env, viewer).status_code == 403
    assert_code(book(env, owner, service_id=str(uuid4())), 404, "not_found")
    assert book(env, owner).status_code == 201
    assert_code(book(env, owner, phone="+919000000712"), 409, "slot_unavailable")
    _, doctor, svc = other_clinic(env["url"])
    assert_code(book(env, owner, service_id=str(svc), doctor_id=str(doctor)), 404, "not_found")
    assert book(env, owner, starts_at="not-a-time").status_code == 422


def test_staff_booking_allows_a_service_hidden_from_the_website(env):
    with connect(env["url"]) as conn:
        conn.execute("UPDATE services SET public=false WHERE id=%s", (SERVICE_ID,))
    r = book(env, login(env))
    assert r.status_code == 201, r.text


def calendar(env, headers, starts=None, ends=None, **params):
    starts = starts or datetime.combine(SLOT.date(), time(0), IST)
    ends = ends or starts + timedelta(days=7)
    return env["client"].get(
        "/v1/staff/calendar",
        params={"from": starts.isoformat(), "to": ends.isoformat(), **params},
        headers=h(headers),
    )


def test_calendar_shows_appointments_sync_state_and_exceptions(env):
    owner, viewer = login(env), login(env, "viewer")
    reference = book(env, owner).json()["reference"]
    with connect(env["url"]) as conn:
        conn.execute(
            "INSERT INTO schedule_exceptions (id,clinic_id,doctor_id,kind,starts_at,ends_at,label)"
            " VALUES (%s,%s,%s,'leave',%s,%s,'Synthetic leave')",
            (
                uuid4(),
                CLINIC,
                DOCTOR_ID,
                SLOT + timedelta(days=1),
                SLOT + timedelta(days=1, hours=2),
            ),
        )
    r = calendar(env, owner)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["timezone"] == "Asia/Kolkata"
    assert [d["id"] for d in body["doctors"]] == [str(DOCTOR_ID)]
    (item,) = body["appointments"]
    assert item["sync_state"] == "synced" and item["channel"] == "staff"
    assert item["patient_name"] == NAME and item["reference"] == reference
    assert item["service_name"] == "Consultation" and item["status"] == "confirmed"
    assert [e["label"] for e in body["exceptions"]] == ["Synthetic leave"]
    assert actions(env["url"]).count("appointment.patient_details_viewed") == 1

    (seen,) = calendar(env, viewer).json()["appointments"]
    assert seen["patient_name"] is None and seen["reference"] == reference
    assert actions(env["url"]).count("appointment.patient_details_viewed") == 1


def test_calendar_sync_state_mapping(env):
    owner = login(env)
    book(env, owner)
    for job_status, state in (("pending", "pending"), ("dead", "failed"), ("done", "synced")):
        with connect(env["url"]) as conn:
            conn.execute(
                "UPDATE outbox SET status=%s, lease_owner=NULL, lease_expires_at=NULL"
                " WHERE kind='calendar.create'",
                (job_status,),
            )
        assert calendar(env, owner).json()["appointments"][0]["sync_state"] == state
    with connect(env["url"]) as conn:
        conn.execute("DELETE FROM outbox")
    assert calendar(env, owner).json()["appointments"][0]["sync_state"] == "not_applicable"


def test_calendar_range_limits_and_tenant_isolation(env):
    owner = login(env)
    start = datetime.combine(SLOT.date(), time(0), IST)
    assert calendar(env, owner, start, start + timedelta(days=32)).status_code == 422
    assert calendar(env, owner, start, start).status_code == 422
    assert calendar(env, owner, start, start + timedelta(days=31)).status_code == 200
    assert (
        env["client"]
        .get("/v1/staff/calendar", params={"from": "2031-03-04T00:00:00"}, headers=h(owner))
        .status_code
        == 422
    )
    book(env, owner)
    clinic, doctor, _ = other_clinic(env["url"])
    stranger = login(env, "owner", clinic)
    body = calendar(env, stranger).json()
    assert body["appointments"] == [] and [d["id"] for d in body["doctors"]] == [str(doctor)]
    assert env["client"].get("/v1/staff/calendar").status_code == 401


def test_calendar_hides_cancelled_unless_asked(env):
    owner = login(env)
    book(env, owner)
    with connect(env["url"]) as conn:
        conn.execute("UPDATE appointments SET status='cancelled'")
    assert calendar(env, owner).json()["appointments"] == []
    assert len(calendar(env, owner, include_cancelled="true").json()["appointments"]) == 1


# --- Callbacks -------------------------------------------------------------------------------


def test_callbacks_list_masks_the_phone_and_hides_details_from_viewers(env):
    owner, viewer = login(env), login(env, "viewer")
    request_id = callback(env["url"])
    r = env["client"].get("/v1/staff/callbacks", headers=h(owner))
    assert r.status_code == 200, r.text
    (item,) = r.json()
    assert item["id"] == str(request_id) and item["name"] == NAME
    assert item["phone_masked"] == "****0711" and PHONE not in r.text
    assert item["reason"] == "Synthetic reason" and item["status"] == "open"
    assert item["synthetic"] is False and item["execution_id"] is None
    assert actions(env["url"]).count("callback.details_viewed") == 1

    (seen,) = env["client"].get("/v1/staff/callbacks", headers=h(viewer)).json()
    assert seen["name"] is None and seen["phone_masked"] is None and seen["reason"] is None
    filtered = env["client"].get("/v1/staff/callbacks?status=done", headers=h(owner)).json()
    assert filtered == []
    assert (
        env["client"].get("/v1/staff/callbacks?status=bogus", headers=h(owner)).status_code == 422
    )
    clinic, _, _ = other_clinic(env["url"])
    assert (
        env["client"].get("/v1/staff/callbacks", headers=h(login(env, "owner", clinic))).json()
        == []
    )


def test_resolve_callback(env):
    admin, viewer = login(env, "admin"), login(env, "viewer")
    request_id = callback(env["url"])
    path = f"/v1/staff/callbacks/{request_id}/resolve"
    assert env["client"].post(path, json={"status": "done"}, headers=h(viewer)).status_code == 403
    r = env["client"].post(path, json={"status": "done"}, headers=h(admin))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "done" and r.json()["resolved_at"]
    again = env["client"].post(path, json={"status": "done"}, headers=h(admin))
    assert again.status_code == 200 and again.json() == r.json()
    assert_code(
        env["client"].post(path, json={"status": "dismissed"}, headers=h(admin)),
        409,
        "callback_resolved",
    )
    assert env["client"].post(path, json={"status": "open"}, headers=h(admin)).status_code == 422
    missing = f"/v1/staff/callbacks/{uuid4()}/resolve"
    assert env["client"].post(missing, json={"status": "done"}, headers=h(admin)).status_code == 404
    assert actions(env["url"]).count("callback.done") == 1
    assert str(callback_row(env["url"], request_id)["resolved_by"]) == admin["_id"]


def test_outbound_status(env):
    viewer = login(env, "viewer")
    r = env["client"].get("/v1/staff/outbound-status", headers=h(viewer))
    assert r.status_code == 200
    assert r.json() == {"enabled": False, "has_from_number": False, "has_outbound_agent": False}
    enable_outbound(env["url"])
    assert env["client"].get("/v1/staff/outbound-status", headers=h(viewer)).json() == {
        "enabled": True,
        "has_from_number": True,
        "has_outbound_agent": True,
    }


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"outbound_enabled": False}, "outbound_disabled"),
        ({"bolna_outbound_from_number": None}, "no_from_number"),
        ({"bolna_outbound_agent_id": None}, "no_outbound_agent"),
    ],
)
def test_call_now_refuses_without_outbound_configuration(env, overrides, code):
    owner = login(env)
    request_id = callback(env["url"])
    enable_outbound(env["url"], **overrides)
    assert_code(call_now(env, owner, request_id), 409, code)
    assert env["bolna"].calls == []
    assert callback_row(env["url"], request_id)["status"] == "open"


def test_call_now_never_calls_synthetic_contacts(env):
    owner = login(env)
    enable_outbound(env["url"])
    seeded = callback(env["url"], synthetic=True)
    assert_code(call_now(env, owner, seeded), 409, "synthetic_contact")
    # A real-looking request whose phone matches a seeded patient is refused as well.
    with connect(env["url"]) as conn:
        conn.execute(
            "INSERT INTO patients (id,clinic_id,phone_ciphertext,phone_key_id,phone_hash,"
            "retention_until,synthetic) VALUES (%s,%s,%s,'k1',%s,now()+interval '1 day',true)",
            (uuid4(), CLINIC, b"x" * 40, phone_hash(decode_hash_key(HASH_KEY), "+919000000799")),
        )
    matched = callback(env["url"], phone="+919000000799")
    assert_code(call_now(env, owner, matched), 409, "synthetic_contact")
    assert env["bolna"].calls == []


def test_call_now_places_one_call_through_the_outbound_agent(env):
    owner, viewer = login(env), login(env, "viewer")
    enable_outbound(env["url"])
    request_id = callback(env["url"])
    assert call_now(env, viewer, request_id).status_code == 403
    r = call_now(env, owner, request_id)
    assert r.status_code == 202, r.text
    execution_id = r.json()["execution_id"]
    assert r.json()["status"] == "calling" and execution_id
    ((name, args),) = env["bolna"].calls
    assert name == "start_outbound_call"
    agent, phone, from_number, scheduled, user_data = args
    assert (agent, phone, from_number, scheduled) == (AGENT, PHONE, FROM_NUMBER, None)
    assert user_data == {
        "call_purpose": "callback_request",
        "clinic_name": "Synthetic Demo Clinic",
        "contact_reference": f"cvc_{request_id.hex}",
    }
    row = callback_row(env["url"], request_id)
    assert row["status"] == "calling" and row["outbound_execution_id"] == execution_id
    assert row["call_outcome"] == "placed" and str(row["call_requested_by"]) == owner["_id"]
    assert row["call_contact_day"] == NOW.date()
    # A repeated click returns the same call and dials nothing.
    again = call_now(env, owner, request_id)
    assert again.status_code == 202 and again.json() == r.json()
    assert len(env["bolna"].calls) == 1
    assert "callback.call_requested" in actions(env["url"])
    # Resolved requests cannot be called.
    env["client"].post(
        f"/v1/staff/callbacks/{request_id}/resolve", json={"status": "done"}, headers=h(owner)
    )
    assert_code(call_now(env, owner, request_id), 409, "callback_resolved")


def test_call_now_timeout_is_unknown_and_never_redialled(env):
    owner = login(env)
    enable_outbound(env["url"])
    request_id = callback(env["url"])
    env["bolna"].timeout_after_call = True
    r = call_now(env, owner, request_id)
    assert r.status_code == 202 and r.json() == {"status": "calling", "execution_id": None}
    row = callback_row(env["url"], request_id)
    assert row["status"] == "calling" and row["call_outcome"] == "unknown"
    assert call_now(env, owner, request_id).json() == {"status": "calling", "execution_id": None}
    assert len(env["bolna"].calls) == 1


def test_call_now_failure_reopens_and_releases_the_budget(env):
    owner = login(env)
    enable_outbound(env["url"], daily_contact_budget=1)
    request_id = callback(env["url"])
    env["bolna"].fail_next("start_outbound_call", 1, ProviderPermanent)
    assert_code(call_now(env, owner, request_id), 502, "call_failed")
    row = callback_row(env["url"], request_id)
    assert row["status"] == "open" and row["call_contact_day"] is None
    assert row["call_outcome"] == "failed"
    # The budget slot was released: the retry goes through.
    assert call_now(env, owner, request_id).status_code == 202
    # The day's single contact is now used.
    assert_code(call_now(env, owner, callback(env["url"])), 409, "budget_exhausted")


def test_call_now_respects_quiet_hours(env):
    owner = login(env)
    enable_outbound(env["url"])
    env["clock"]["now"] = datetime.combine(NOW.date(), time(22, 30), IST).astimezone(UTC)
    assert_code(call_now(env, owner, callback(env["url"])), 409, "quiet_hours")
    assert env["bolna"].calls == []


def test_call_now_without_bolna_or_outside_the_clinic(env):
    owner = login(env)
    enable_outbound(env["url"])
    request_id = callback(env["url"])
    env["port"]["bolna"] = None
    assert_code(call_now(env, owner, request_id), 503, "bolna_unconfigured")
    assert callback_row(env["url"], request_id)["status"] == "open"
    env["port"]["bolna"] = env["bolna"]
    clinic, _, _ = other_clinic(env["url"])
    foreign = callback(env["url"], clinic=clinic)
    assert call_now(env, owner, foreign).status_code == 404
    assert call_now(env, owner, uuid4()).status_code == 404
    assert env["bolna"].calls == []


# --- Review fixes: unexpected port errors, reopen, parallel clicks ---------------------------


class ExplodingBolna(FakeBolna):
    """A port whose outbound call raises something that is not a ProviderError."""

    def start_outbound_call(self, *args, **kwargs):
        self.calls.append(("start_outbound_call", args))
        raise RuntimeError("synthetic adapter bug")


def reopen(env, headers, request_id):
    return env["client"].post(f"/v1/staff/callbacks/{request_id}/reopen", headers=h(headers))


def test_call_now_unexpected_error_is_unknown_never_null(env, caplog):
    caplog.set_level(logging.INFO)
    owner = login(env)
    enable_outbound(env["url"])
    request_id = callback(env["url"])
    env["port"]["bolna"] = ExplodingBolna()
    r = call_now(env, owner, request_id)
    assert r.status_code == 202 and r.json() == {"status": "calling", "execution_id": None}
    row = callback_row(env["url"], request_id)
    assert row["status"] == "calling" and row["call_outcome"] == "unknown"
    assert "error=RuntimeError" in caplog.text and "synthetic adapter bug" not in caplog.text
    # Never redialled by a repeated click.
    assert call_now(env, owner, request_id).status_code == 202
    assert len(env["port"]["bolna"].calls) == 1
    listed = env["client"].get("/v1/staff/callbacks", headers=h(owner)).json()
    assert listed[0]["call_outcome"] == "unknown"


def test_reopen_lets_staff_retry_a_placed_or_unknown_call(env):
    owner, viewer = login(env), login(env, "viewer")
    enable_outbound(env["url"], daily_contact_budget=2)
    request_id = callback(env["url"])
    assert call_now(env, owner, request_id).status_code == 202
    first = callback_row(env["url"], request_id)
    assert reopen(env, viewer, request_id).status_code == 403
    r = reopen(env, owner, request_id)
    assert r.status_code == 200 and r.json() == {"id": str(request_id), "status": "open"}
    row = callback_row(env["url"], request_id)
    assert row["status"] == "open" and row["call_outcome"] is None
    assert row["call_contact_day"] is None and row["outbound_execution_id"] is None
    with connect(env["url"]) as conn:
        (attempt,) = conn.execute("SELECT * FROM callback_call_attempts").fetchall()
    assert attempt["callback_request_id"] == request_id and attempt["outcome"] == "placed"
    assert attempt["execution_id"] == first["outbound_execution_id"]
    assert attempt["contact_day"] == NOW.date() and str(attempt["reopened_by"]) == owner["_id"]
    assert "callback.reopened" in actions(env["url"])
    assert len(env["bolna"].calls) == 1  # Reopening never dials.
    # Reopening an open request changes nothing.
    assert reopen(env, owner, request_id).json() == {"id": str(request_id), "status": "open"}
    # A deliberate retry dials again; both calls count against today's budget of 2.
    assert call_now(env, owner, request_id).status_code == 202
    assert len(env["bolna"].calls) == 2
    assert_code(call_now(env, owner, callback(env["url"])), 409, "budget_exhausted")
    # Resolved requests cannot be reopened; unknown ones can.
    env["client"].post(
        f"/v1/staff/callbacks/{request_id}/resolve", json={"status": "done"}, headers=h(owner)
    )
    assert_code(reopen(env, owner, request_id), 409, "callback_resolved")
    assert reopen(env, owner, uuid4()).status_code == 404


def test_reopen_refuses_a_call_still_being_placed_until_it_is_stale(env):
    owner = login(env)
    enable_outbound(env["url"])
    request_id = callback(env["url"])
    # The state a "Call now" leaves between its commit and the provider's answer.
    with connect(env["url"]) as conn:
        conn.execute(
            "UPDATE callback_requests SET status='calling', call_requested_at=%s,"
            " call_contact_day=%s WHERE id=%s",
            (env["clock"]["now"], NOW.date(), request_id),
        )
    assert_code(reopen(env, owner, request_id), 409, "call_in_progress")
    env["clock"]["now"] += timedelta(minutes=6)
    assert reopen(env, owner, request_id).status_code == 200
    with connect(env["url"]) as conn:
        outcome = conn.execute("SELECT outcome FROM callback_call_attempts").fetchone()["outcome"]
    assert outcome == "unknown"


def test_reopen_after_a_timeout_and_tenant_isolation(env):
    owner = login(env)
    enable_outbound(env["url"])
    request_id = callback(env["url"])
    env["bolna"].timeout_after_call = True
    call_now(env, owner, request_id)
    other = login(env, "owner", other_clinic(env["url"])[0])
    assert reopen(env, other, request_id).status_code == 404
    assert reopen(env, owner, request_id).status_code == 200


def test_parallel_call_now_clicks_dial_once(env):
    import threading

    owner = login(env)
    enable_outbound(env["url"])
    request_id = callback(env["url"])
    barrier = threading.Barrier(5)
    responses = []

    def click():
        barrier.wait()
        responses.append(call_now(env, owner, request_id))

    threads = [threading.Thread(target=click) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert [r.status_code for r in responses] == [202] * 5
    assert all(r.json()["status"] == "calling" for r in responses)
    assert len(env["bolna"].calls) == 1
    row = callback_row(env["url"], request_id)
    assert row["status"] == "calling" and row["call_outcome"] == "placed"
    assert actions(env["url"]).count("callback.call_requested") == 1


# --- Conversations ---------------------------------------------------------------------------


def chat_session(url, clinic=CLINIC):
    keyring = Keyring.from_setting(KEYS)
    session_id = uuid4()
    with connect(url) as conn:
        conn.execute(
            "INSERT INTO chat_sessions (id,clinic_id,token_hash,turn_count) VALUES (%s,%s,%s,1)",
            (session_id, clinic, hashlib.sha256(uuid4().bytes).digest()),
        )
        session = {"id": session_id, "clinic_id": clinic}
        chat.append(conn, keyring, session, "assistant", {"text": "Hello from the assistant"})
        chat.append(conn, keyring, session, "user", {"text": "SYNTHETIC-CHAT-MARKER hello"})
        chat.append(
            conn,
            keyring,
            session,
            "assistant",
            {"text": None, "tool_calls": [{"id": "c1", "name": "check_slots", "arguments": {}}]},
        )
        chat.append(
            conn,
            keyring,
            session,
            "tool",
            {"tool_call_id": "c1", "result": {"slots": [{"secret": "SYNTHETIC-TOOL-RAW"}]}},
            tool_name="check_slots",
        )
    return session_id


def voice_session(url):
    session_id = uuid4()
    with connect(url) as conn:
        conn.execute(
            "INSERT INTO call_sessions (id,clinic_id,execution_id,provider_status,call_type,"
            "conversation_seconds,hangup_by) VALUES (%s,%s,'exec-synthetic-1','completed',"
            "'inbound',42.5,'user')",
            (session_id, CLINIC),
        )
    return session_id


def test_conversations_list_and_chat_transcript(env):
    owner, viewer = login(env), login(env, "viewer")
    chat_id, voice_id = chat_session(env["url"]), voice_session(env["url"])
    r = env["client"].get("/v1/staff/conversations", headers=h(viewer))
    assert r.status_code == 200, r.text
    assert {(c["id"], c["channel"]) for c in r.json()} == {
        (str(chat_id), "chat"),
        (str(voice_id), "voice"),
    }
    only = env["client"].get("/v1/staff/conversations?channel=chat", headers=h(viewer)).json()
    assert [c["id"] for c in only] == [str(chat_id)]

    detail = env["client"].get(f"/v1/staff/conversations/{chat_id}", headers=h(owner))
    assert detail.status_code == 200, detail.text
    messages = detail.json()["messages"]
    assert [m["role"] for m in messages] == ["assistant", "user", "assistant", "tool"]
    assert messages[1]["text"] == "SYNTHETIC-CHAT-MARKER hello"
    assert messages[2]["tool_name"] == "check_slots"
    assert messages[3]["text"] == "check_slots: ok"
    assert "SYNTHETIC-TOOL-RAW" not in detail.text
    assert actions(env["url"]).count("conversation.transcript_viewed") == 1

    hidden = env["client"].get(f"/v1/staff/conversations/{chat_id}", headers=h(viewer)).json()
    assert [m["text"] for m in hidden["messages"]] == [None, None, None, None]
    assert actions(env["url"]).count("conversation.transcript_viewed") == 1


def test_voice_conversation_detail_and_isolation(env):
    owner = login(env)
    voice_id = voice_session(env["url"])
    r = env["client"].get(f"/v1/staff/conversations/{voice_id}", headers=h(owner))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["channel"] == "voice" and body["provider_status"] == "completed"
    assert body["duration_seconds"] == 42.5 and body["hangup_by"] == "user"
    assert body["messages"] == [] and body["appointments"] == []
    clinic, _, _ = other_clinic(env["url"])
    foreign_chat = chat_session(env["url"], clinic)
    assert (
        env["client"].get(f"/v1/staff/conversations/{foreign_chat}", headers=h(owner)).status_code
        == 404
    )
    stranger = login(env, "owner", clinic)
    assert (
        env["client"].get(f"/v1/staff/conversations/{voice_id}", headers=h(stranger)).status_code
        == 404
    )


# --- Stats -----------------------------------------------------------------------------------


def test_stats(env):
    owner = login(env)
    book(env, owner)  # Tomorrow.
    book(
        env, owner, starts_at=datetime.combine(NOW.date(), time(15, 0), IST), phone="+919000000713"
    )
    callback(env["url"])
    r = env["client"].get("/v1/staff/stats", headers=h(login(env, "viewer")))
    assert r.status_code == 200, r.text
    assert r.json() == {
        "today": 1,
        "week": 2,
        "needs_review": 0,
        "open_callbacks": 1,
        "by_channel": {"voice": 0, "web": 0, "chat": 0, "staff": 2},
    }


# --- Admin public profile --------------------------------------------------------------------


def test_admin_edits_the_public_profile(env):
    admin, owner = login(env, "admin"), login(env)
    changes = {
        "public_tagline": "Synthetic smiles",
        "public_address": "1 Synthetic Road",
        "public_map_url": "https://maps.example.test/synthetic",
        "public_phone_display": "+91 90000 01000",
        "public_email": "hello@example.test",
        "public_hours_note": "Closed on Sundays",
        "public_about": "A synthetic clinic.",
        "public_voice_number": "+919000001000",
        "public_chat_enabled": True,
    }
    r = env["client"].patch("/v1/admin/clinic", json=changes, headers=h(admin))
    assert r.status_code == 200, r.text
    body = r.json()
    assert {k: body[k] for k in changes} == changes
    assert body["public_slug"] == "demo-clinic" and body["public_enabled"] is False
    assert "clinic.public_profile_updated" in actions(env["url"])
    assert "clinic.chat_enabled" in actions(env["url"])
    assert env["client"].get("/v1/admin/clinic", headers=h(admin)).json()["public_about"] == (
        "A synthetic clinic."
    )

    number = {"bolna_outbound_from_number": FROM_NUMBER}
    assert env["client"].patch("/v1/admin/clinic", json=number, headers=h(admin)).status_code == 403
    r = env["client"].patch("/v1/admin/clinic", json=number, headers=h(owner))
    assert r.status_code == 200 and r.json()["bolna_outbound_from_number"] == FROM_NUMBER
    assert "clinic.outbound_number_updated" in actions(env["url"])

    for bad in (
        {"public_map_url": "http://insecure.example.test"},
        {"public_email": "not-an-email"},
        {"public_voice_number": "12345"},
        {"public_tagline": "x" * 161},
        {"public_slug": "other-slug"},
        {"public_enabled": True},
        {"public_about": "bad\x00text"},
    ):
        response = env["client"].patch("/v1/admin/clinic", json=bad, headers=h(owner))
        assert response.status_code == 422, bad
    viewer = login(env, "viewer")
    assert (
        env["client"]
        .patch("/v1/admin/clinic", json={"public_tagline": "x"}, headers=h(viewer))
        .status_code
        == 403
    )


def test_call_now_logs_no_contact_details(env, caplog):
    owner = login(env)
    enable_outbound(env["url"])
    caplog.set_level(logging.DEBUG, logger="clinic_voice")
    assert call_now(env, owner, callback(env["url"])).status_code == 202
    text = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("clinic_voice"))
    assert "callback call placed" in text
    for secret in (PHONE, PHONE[-4:], NAME, "Synthetic reason", FROM_NUMBER):
        assert secret not in text
