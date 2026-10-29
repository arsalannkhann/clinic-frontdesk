"""Patients, consent, verified booking lookup, cancel/reschedule, staff appointment routes
and retention (T7).

Synthetic data only: "Synthetic Patient" names, +9100000xxxxx numbers, generated keys and
credentials. Calendar behaviour comes from FakeCalendar; nothing reaches the network.
"""

import base64
import logging
import time as pytime
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, time, timedelta
from threading import Barrier
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from clinic_voice import appointments, cli, credentials, service, staff
from clinic_voice.api import app
from clinic_voice.bolna import bookings
from clinic_voice.cli import DEMO_CLINIC_ID as CLINIC
from clinic_voice.cli import DOCTOR_ID, SERVICE_ID
from clinic_voice.config import get_settings
from clinic_voice.crypto import CryptoError, Keyring, phone_hash
from clinic_voice.db import connect
from clinic_voice.domain import normalize_reference
from clinic_voice.outbox import Ports, run_once
from clinic_voice.patients import field_aad
from clinic_voice.providers.calendar import FakeCalendar, event_id_for
from clinic_voice.schemas import ReservationRequest

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2031, 3, 3, 8, 30, tzinfo=IST)
DAY = NOW.date() + timedelta(days=2)
SLOT = datetime.combine(DAY, time(9, 0), IST)
KEYS = "k1:" + base64.b64encode(b"\x02" * 32).decode()
HASH_KEY_BYTES = b"\x03" * 32
HASH_KEY = base64.b64encode(HASH_KEY_BYTES).decode()
CALLER = "+910000000123"
OTHER_CALLER = "+910000000456"
NAME = "Synthetic Patient Alpha"
SIBLING = "Synthetic Patient Beta"
AGENT = "11111111-1111-4111-8111-111111111111"
OTHER_CLINIC = UUID("00000000-0000-4000-8000-0000000000c1")
PASSWORD = "synthetic-password-01"
CAL = "booking@example.test"
VERIFY = "/v1/bolna/tools/verify-booking"
CANCEL = "/v1/bolna/tools/cancel-booking"
RESCHEDULE = "/v1/bolna/tools/reschedule-booking"
RESERVE = "/v1/bolna/tools/reserve-slot"


@pytest.fixture
def env(database, monkeypatch):
    monkeypatch.setattr(service, "utc_now", lambda: NOW.astimezone(UTC))
    monkeypatch.setattr(staff, "SCRYPT_N", 2**10)
    monkeypatch.setenv("DATABASE_URL", database)
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEYS", KEYS)
    monkeypatch.setenv("PHONE_HASH_KEY", HASH_KEY)
    get_settings.cache_clear()
    with connect(database) as conn:
        conn.execute("INSERT INTO clinics (id,name) VALUES (%s,'Other Synthetic')", (OTHER_CLINIC,))
        _, token = credentials.issue(conn, CLINIC, "t7")
        _, other = credentials.issue(conn, OTHER_CLINIC, "t7-other")
        conn.execute("UPDATE doctors SET booking_calendar_id=%s WHERE id=%s", (CAL, DOCTOR_ID))
    with TestClient(app) as client:
        yield {"client": client, "url": database, "token": token, "other": other}
    get_settings.cache_clear()


def execution():
    return str(uuid4())


def body(execution_id, number=CALLER, **extra):
    return {"execution_id": execution_id, "from_number": number, "agent_id": AGENT, **extra}


def post(env, path, payload, token=None):
    return env["client"].post(
        path, json=payload, headers={"Authorization": f"Bearer {token or env['token']}"}
    )


def book(env, name=NAME, number=CALLER, starts_at=SLOT, execution_id=None, **extra):
    execution_id = execution_id or execution()
    r = post(
        env,
        RESERVE,
        body(
            execution_id,
            number,
            doctor="Demo Doctor",
            service="Consultation",
            starts_at=starts_at.isoformat(),
            patient_name=name,
            **extra,
        ),
    )
    assert r.status_code == 202, r.text
    return {**r.json(), "execution_id": execution_id}


def verify(env, execution_id, reference, number=CALLER, token=None):
    return post(env, VERIFY, body(execution_id, number, booking_reference=reference), token)


def cancel(env, execution_id, reference, confirmed="true"):
    return post(env, CANCEL, body(execution_id, booking_reference=reference, confirmed=confirmed))


def reschedule(env, execution_id, reference, starts_at, confirmed="true"):
    return post(
        env,
        RESCHEDULE,
        body(
            execution_id,
            booking_reference=reference,
            starts_at=starts_at.isoformat(),
            confirmed=confirmed,
        ),
    )


def rows(url, sql, params=()):
    with connect(url) as conn:
        return conn.execute(sql, params).fetchall()


def appointment(url, appointment_id):
    return rows(url, "SELECT * FROM appointments WHERE id=%s", (appointment_id,))[0]


def snapshot(url):
    """Everything a change could touch, for "nothing changed" assertions."""
    return (
        rows(url, "SELECT id, status, version, updated_at FROM appointments ORDER BY id"),
        rows(url, "SELECT id, kind, appointment_version FROM outbox ORDER BY id"),
        rows(url, "SELECT * FROM appointment_changes ORDER BY id"),
    )


def staff_headers(env, role="owner", clinic=CLINIC):
    email = f"{role}-{uuid4().hex[:8]}@example.test"
    with connect(env["url"]) as conn:
        staff.create_user(conn, clinic, email, role, PASSWORD)
    r = env["client"].post("/v1/staff/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200
    return {"Authorization": "Bearer " + r.json()["token"]}


# Patients and consent


def test_reservation_creates_encrypted_patient_and_consents(env):
    made = book(
        env,
        preferred_language="HI",
        consent_booking_confirmation="true",
        consent_reminder_call="false",
        consent_reminder_message="",
    )
    reference = made["booking_reference"]
    assert normalize_reference(reference) == reference and len(reference) == 8
    assert " ".join(reference) in made["spoken"]
    assert "not yet confirmed" in made["spoken"]
    [patient] = rows(env["url"], "SELECT * FROM patients")
    keyring = Keyring.from_setting(KEYS)
    pid = patient["id"]
    name = keyring.decrypt(
        patient["name_key_id"], patient["name_ciphertext"], field_aad(CLINIC, pid, "name")
    )
    phone = keyring.decrypt(
        patient["phone_key_id"], patient["phone_ciphertext"], field_aad(CLINIC, pid, "phone")
    )
    assert name.decode() == NAME and phone.decode() == CALLER
    with pytest.raises(CryptoError):  # Bound to field and row.
        keyring.decrypt(
            patient["name_key_id"], patient["name_ciphertext"], field_aad(CLINIC, pid, "phone")
        )
    assert bytes(patient["phone_hash"]) == phone_hash(HASH_KEY_BYTES, CALLER)
    assert patient["preferred_language"] == "hi"
    assert patient["retention_until"] == SLOT + timedelta(days=365)
    text = rows(env["url"], "SELECT p::text AS t FROM patients p")[0]["t"]
    assert NAME not in text and "0000000123" not in text
    consents = rows(env["url"], "SELECT purpose, source, revoked_at FROM consents")
    assert consents == [
        {"purpose": "booking_confirmation", "source": "voice_call", "revoked_at": None}
    ]
    row = appointment(env["url"], made["appointment_id"])
    assert row["patient_id"] == pid and row["booking_reference"] == reference


def test_reservation_replay_returns_same_reference_and_patient(env):
    first = book(env)
    again = book(env, name="Different Synthetic", execution_id=first["execution_id"])
    assert again["booking_reference"] == first["booking_reference"]
    assert again["appointment_id"] == first["appointment_id"]
    assert len(rows(env["url"], "SELECT id FROM patients")) == 1


def test_attach_policy(env):
    first = book(env)
    # Same phone, same name (folded), another call: the same patient.
    same = book(env, name="  synthetic   PATIENT alpha ", starts_at=SLOT + timedelta(hours=1))
    # Same phone, another name: a separate person on a shared phone.
    sibling = book(env, name=SIBLING, starts_at=SLOT + timedelta(hours=2))
    # No name: never merged by phone alone.
    anonymous = book(env, name="", starts_at=SLOT + timedelta(hours=3))
    ids = {
        key: appointment(env["url"], made["appointment_id"])["patient_id"]
        for key, made in {
            "first": first,
            "same": same,
            "sibling": sibling,
            "anonymous": anonymous,
        }.items()
    }
    assert ids["first"] == ids["same"]
    assert len({ids["first"], ids["sibling"], ids["anonymous"]}) == 3
    [patient] = rows(
        env["url"], "SELECT retention_until FROM patients WHERE id=%s", (ids["first"],)
    )
    assert patient["retention_until"] == SLOT + timedelta(hours=1, days=365)


def active_consents(env):
    return rows(env["url"], "SELECT purpose FROM consents WHERE revoked_at IS NULL")


def test_consent_grants_are_additive_for_unverified_callers(env):
    book(env, consent_reminder_call="true")
    book(env, starts_at=SLOT + timedelta(hours=1), consent_reminder_call="yes")
    assert active_consents(env) == [{"purpose": "reminder_call"}]
    # Same phone + name on a later call, not verified: a grant adds, a decline is ignored.
    book(
        env,
        starts_at=SLOT + timedelta(hours=2),
        consent_reminder_call="false",
        consent_reminder_message="true",
    )
    assert sorted(r["purpose"] for r in active_consents(env)) == [
        "reminder_call",
        "reminder_message",
    ]
    assert rows(env["url"], "SELECT id FROM consents WHERE revoked_at IS NOT NULL") == []
    assert len(rows(env["url"], "SELECT id FROM patients")) == 1


def test_unverified_caller_cannot_change_language(env):
    book(env, preferred_language="hi")
    book(env, starts_at=SLOT + timedelta(hours=1), preferred_language="en")
    [patient] = rows(env["url"], "SELECT preferred_language FROM patients")
    assert patient["preferred_language"] == "hi"


def test_unverified_caller_fills_missing_language(env):
    book(env)
    book(env, starts_at=SLOT + timedelta(hours=1), preferred_language="en")
    [patient] = rows(env["url"], "SELECT preferred_language FROM patients")
    assert patient["preferred_language"] == "en"


def test_verified_session_decline_revokes_and_changes_language(env):
    first = book(env, consent_reminder_call="true", preferred_language="hi")
    call = execution()
    assert verify(env, call, first["booking_reference"]).status_code == 200
    book(
        env,
        execution_id=call,
        starts_at=SLOT + timedelta(hours=1),
        consent_reminder_call="no",
        preferred_language="en",
    )
    assert active_consents(env) == []
    [patient] = rows(env["url"], "SELECT preferred_language FROM patients")
    assert patient["preferred_language"] == "en"


@pytest.mark.parametrize("value", ["maybe", "TRUE please", "1"])
def test_unclear_consent_is_refused_before_booking(env, value):
    r = post(
        env,
        RESERVE,
        body(
            execution(),
            doctor="Demo Doctor",
            service="Consultation",
            starts_at=SLOT.isoformat(),
            consent_reminder_call=value,
        ),
    )
    assert r.status_code == 422
    assert "yes or no about reminder calls" in r.json()["detail"]
    assert value not in r.json()["detail"]
    assert rows(env["url"], "SELECT id FROM appointments") == []
    assert rows(env["url"], "SELECT id FROM patients") == []


def test_reschedule_does_not_use_a_session_place(env):
    earlier = book(env)  # Booked on an earlier call.
    call = execution()
    for hour in (1, 2):
        book(env, execution_id=call, starts_at=SLOT + timedelta(hours=hour))
    verify(env, call, earlier["booking_reference"])
    assert (
        reschedule(env, call, earlier["booking_reference"], SLOT + timedelta(hours=3)).status_code
        == 200
    )
    # Two own bookings plus a moved one from another call: the third own booking fits.
    book(env, execution_id=call, starts_at=SLOT + timedelta(hours=4))
    # Moving one of this call's bookings keeps its place, so a fourth is still refused.
    own = rows(
        env["url"],
        "SELECT booking_reference FROM appointments a JOIN call_sessions s ON s.id=a.call_session_id"
        " WHERE s.execution_id=%s AND a.rescheduled_from IS NULL ORDER BY a.starts_at LIMIT 1",
        (call,),
    )[0]["booking_reference"]
    verify(env, call, own)
    assert reschedule(env, call, own, SLOT + timedelta(hours=5)).status_code == 200
    r = post(
        env,
        RESERVE,
        body(
            call,
            doctor="Demo Doctor",
            service="Consultation",
            starts_at=(SLOT + timedelta(hours=6)).isoformat(),
        ),
    )
    assert r.status_code == 409 and r.json() == {"detail": service.SESSION_LIMIT_MESSAGE}


def test_attach_after_concurrent_purge_creates_new_patient(env):
    first = book(env)
    patient_id = appointment(env["url"], first["appointment_id"])["patient_id"]
    purge = connect(env["url"])
    try:
        # A purge in flight: it holds the patient row and deletes it, not yet committed.
        purge.execute("SELECT id FROM patients WHERE id=%s FOR UPDATE", (patient_id,))
        purge.execute("UPDATE appointments SET patient_id=NULL WHERE patient_id=%s", (patient_id,))
        purge.execute("DELETE FROM patients WHERE id=%s", (patient_id,))
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(book, env, starts_at=SLOT + timedelta(hours=1))
            pytime.sleep(0.5)
            assert not pending.done()  # Waiting on the matched patient's row lock.
            purge.commit()
            made = pending.result(timeout=10)
    finally:
        purge.close()
    new_patient = appointment(env["url"], made["appointment_id"])["patient_id"]
    assert new_patient is not None and new_patient != patient_id
    assert [r["id"] for r in rows(env["url"], "SELECT id FROM patients")] == [new_patient]


def test_no_patient_without_number_or_name(env):
    made = book(env, name="", number="")
    assert appointment(env["url"], made["appointment_id"])["patient_id"] is None
    assert rows(env["url"], "SELECT id FROM patients") == []


def test_phone_is_not_a_tool_parameter(env):
    r = post(
        env,
        RESERVE,
        body(
            execution(),
            doctor="Demo Doctor",
            service="Consultation",
            starts_at=SLOT.isoformat(),
            patient_phone=OTHER_CALLER,
        ),
    )
    assert r.status_code == 422


# Verification


def test_verify_success_bound_to_session(env):
    made = book(env)
    call = execution()
    r = verify(
        env, call, made["booking_reference"].lower()[:4] + "-" + made["booking_reference"][4:]
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["booking_reference"] == made["booking_reference"]
    assert out["doctor"] == "Demo Doctor" and out["status"] == "pending_sync"
    assert "not yet confirmed" in out["spoken"]
    assert NAME not in r.text and "0000000123" not in r.text
    # Replay: same result, one verification record.
    again = verify(env, call, made["booking_reference"])
    assert again.json() == out
    [record] = rows(env["url"], "SELECT * FROM call_session_verifications")
    assert record["appointment_id"] == UUID(made["appointment_id"])
    # appointment-status now answers for it in this call only.
    status = body(call, appointment_id=made["appointment_id"])
    assert post(env, "/v1/bolna/tools/appointment-status", status).status_code == 200
    status["execution_id"] = execution()
    assert post(env, "/v1/bolna/tools/appointment-status", status).status_code == 404


def test_verify_failures_are_uniform(env):
    made = book(env)
    other_phone = book(env, number=OTHER_CALLER, starts_at=SLOT + timedelta(hours=1))
    unknown = "ZZZZZZZZ" if made["booking_reference"] != "ZZZZZZZZ" else "YYYYYYYY"
    attempts = [
        verify(env, execution(), unknown),  # well-formed, no such booking
        verify(env, execution(), "not a reference!"),  # malformed
        verify(env, execution(), other_phone["booking_reference"]),  # another phone's booking
        verify(env, execution(), made["booking_reference"], number=""),  # no caller number
        verify(env, execution(), made["booking_reference"], number=OTHER_CALLER),
        verify(env, execution(), made["booking_reference"], token=env["other"]),  # other clinic
    ]
    outcomes = {(r.status_code, r.text) for r in attempts}
    assert outcomes == {(404, '{"detail":"' + bookings.VERIFY_FAILED + '"}')}
    assert rows(env["url"], "SELECT * FROM call_session_verifications") == []


def test_wrong_references_count_to_handoff(env):
    made = book(env)
    call = execution()
    assert verify(env, call, "AAAAAAAA").status_code == 404
    assert verify(env, call, "AAAAAAAA").status_code == 404  # A retry is not counted twice.
    assert verify(env, call, "BBBBBBBB").status_code == 404
    third = verify(env, call, "CCCCCCCC")
    assert third.status_code == 403 and third.json() == {"detail": bookings.HANDOFF}
    [session] = rows(
        env["url"], "SELECT verification_failures FROM call_sessions WHERE execution_id=%s", (call,)
    )
    assert session["verification_failures"] == 3
    # Even the right reference is refused now, and so are changes.
    assert verify(env, call, made["booking_reference"]).json() == {"detail": bookings.HANDOFF}
    assert cancel(env, call, made["booking_reference"]).status_code == 403
    r = reschedule(env, call, made["booking_reference"], SLOT + timedelta(hours=1))
    assert r.json() == {"detail": bookings.HANDOFF}
    # Another call starts fresh.
    assert verify(env, execution(), made["booking_reference"]).status_code == 200


def test_verified_session_then_handoff_refuses_changes(env):
    made = book(env)
    call = execution()
    assert verify(env, call, made["booking_reference"]).status_code == 200
    for wrong in ("AAAAAAAA", "BBBBBBBB", "CCCCCCCC"):
        verify(env, call, wrong)
    before = snapshot(env["url"])
    assert cancel(env, call, made["booking_reference"]).json() == {"detail": bookings.HANDOFF}
    assert snapshot(env["url"]) == before


def test_shared_phone_cannot_enumerate(env):
    alpha = book(env)
    beta = book(env, name=SIBLING, starts_at=SLOT + timedelta(hours=1))
    call = execution()
    r = verify(env, call, alpha["booking_reference"])
    assert r.status_code == 200
    # The response names exactly the spoken booking; nothing about the other one.
    assert beta["booking_reference"] not in r.text and beta["appointment_id"] not in r.text
    # Beta is not verified on this call, so it can be neither read nor changed.
    status = body(call, appointment_id=beta["appointment_id"])
    assert post(env, "/v1/bolna/tools/appointment-status", status).status_code == 404
    assert cancel(env, call, beta["booking_reference"]).json() == {"detail": bookings.NOT_VERIFIED}
    # There is no lookup by phone: no tool accepts a bare call context.
    for path in (VERIFY, CANCEL, RESCHEDULE):
        assert post(env, path, body(call)).status_code == 422
    paths = app.openapi()["paths"]
    assert not any("phone" in path or "search" in path for path in paths)


def test_verification_cannot_be_reused_by_another_execution(env):
    made = book(env)
    first = execution()
    assert verify(env, first, made["booking_reference"]).status_code == 200
    before = snapshot(env["url"])
    second = execution()  # Same caller, another call.
    r = cancel(env, second, made["booking_reference"])
    assert r.status_code == 403 and r.json() == {"detail": bookings.NOT_VERIFIED}
    r = reschedule(env, second, made["booking_reference"], SLOT + timedelta(hours=1))
    assert r.status_code == 403
    # The same execution id under another clinic's credential is another session.
    r = post(
        env,
        CANCEL,
        body(first, booking_reference=made["booking_reference"], confirmed="true"),
        token=env["other"],
    )
    assert r.status_code == 403
    assert snapshot(env["url"]) == before


# Cancel


@pytest.mark.parametrize("confirmed", [None, "false", "", "yes", "TRUE please", False])
def test_cancel_without_confirmation_changes_nothing(env, confirmed):
    made = book(env)
    call = execution()
    verify(env, call, made["booking_reference"])
    before = snapshot(env["url"])
    payload = body(call, booking_reference=made["booking_reference"])
    if confirmed is not None:
        payload["confirmed"] = confirmed
    r = post(env, CANCEL, payload)
    assert r.status_code == 200
    assert r.json()["changed"] is False and r.json()["status"] == "pending_sync"
    assert "Please confirm" in r.json()["spoken"] and "cancelled" not in r.json()["spoken"]
    assert snapshot(env["url"]) == before


def test_cancel_flow_and_replay(env):
    made = book(env)
    call = execution()
    verify(env, call, made["booking_reference"])
    r = cancel(env, call, made["booking_reference"], confirmed="True")
    assert r.status_code == 200
    out = r.json()
    assert out["changed"] is True and out["status"] == "cancelled"
    assert out["spoken"] == service.spoken_status("cancelled")
    row = appointment(env["url"], made["appointment_id"])
    assert row["status"] == "cancelled" and row["version"] == 2
    jobs = rows(
        env["url"],
        "SELECT kind, appointment_version FROM outbox WHERE appointment_id=%s ORDER BY kind",
        (made["appointment_id"],),
    )
    assert jobs == [
        {"kind": "calendar.cancel", "appointment_version": 1},
        {"kind": "calendar.create", "appointment_version": 1},
    ]
    [change] = rows(env["url"], "SELECT * FROM appointment_changes")
    assert change["action"] == "cancel" and change["source"] == "voice_call"
    assert change["idempotency_key"].startswith(f"bolna:{call}:cancel-booking:")
    # Replay: same outcome, nothing new.
    before = snapshot(env["url"])
    assert cancel(env, call, made["booking_reference"]).json() == out
    assert snapshot(env["url"]) == before
    # A reschedule of the cancelled booking changes nothing.
    r = reschedule(env, call, made["booking_reference"], SLOT + timedelta(hours=1))
    assert r.status_code == 409 and r.json() == {"detail": bookings.NOT_ACTIVE}
    assert snapshot(env["url"]) == before


def test_cancel_refuses_started_appointment(env, monkeypatch):
    made = book(env)
    call = execution()
    verify(env, call, made["booking_reference"])
    monkeypatch.setattr(service, "utc_now", lambda: SLOT.astimezone(UTC))
    r = cancel(env, call, made["booking_reference"])
    assert r.status_code == 409 and r.json() == {"detail": bookings.STARTED}
    assert appointment(env["url"], made["appointment_id"])["status"] == "pending_sync"


# Reschedule


def test_reschedule_flow_and_replay(env):
    made = book(env)
    call = execution()
    verify(env, call, made["booking_reference"])
    new_start = SLOT + timedelta(hours=2)
    preview = reschedule(env, call, made["booking_reference"], new_start, confirmed="false")
    assert preview.json()["changed"] is False and "Please confirm" in preview.json()["spoken"]
    r = reschedule(env, call, made["booking_reference"], new_start)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["changed"] is True and out["status"] == "pending_sync"
    assert out["previous_booking_reference"] == made["booking_reference"]
    assert out["booking_reference"] != made["booking_reference"]
    assert "moved" in out["spoken"] and "not yet confirmed" in out["spoken"]
    assert datetime.fromisoformat(out["starts_at"]) == new_start
    old = appointment(env["url"], made["appointment_id"])
    [new] = rows(env["url"], "SELECT * FROM appointments WHERE rescheduled_from=%s", (old["id"],))
    assert old["status"] == "cancelled" and old["version"] == 2
    assert new["version"] == 1 and new["status"] == "pending_sync"
    assert new["patient_id"] == old["patient_id"] is not None
    assert new["booking_reference"] == out["booking_reference"]
    jobs = rows(
        env["url"],
        "SELECT appointment_id, kind, appointment_version FROM outbox"
        " ORDER BY appointment_id = %s DESC, kind",
        (old["id"],),
    )
    assert jobs == [
        {"appointment_id": old["id"], "kind": "calendar.cancel", "appointment_version": 1},
        {"appointment_id": old["id"], "kind": "calendar.create", "appointment_version": 1},
        {"appointment_id": new["id"], "kind": "calendar.create", "appointment_version": 1},
    ]
    [patient] = rows(env["url"], "SELECT retention_until FROM patients")
    assert patient["retention_until"] == new_start + timedelta(days=365)
    # The new booking is verified on this call (status and further changes work here).
    status = body(call, appointment_id=str(new["id"]))
    assert post(env, "/v1/bolna/tools/appointment-status", status).status_code == 200
    before = snapshot(env["url"])
    assert reschedule(env, call, made["booking_reference"], new_start).json() == out
    assert snapshot(env["url"]) == before
    # A different target for the already moved booking is refused.
    r = reschedule(env, call, made["booking_reference"], SLOT + timedelta(hours=3))
    assert r.json() == {"detail": bookings.NOT_ACTIVE}


def test_reschedule_may_overlap_its_own_old_hold(env):
    with connect(env["url"]) as conn:
        conn.execute("UPDATE services SET buffer_after_minutes=15 WHERE id=%s", (SERVICE_ID,))
    made = book(env)  # Holds [9:00, 9:30).
    call = execution()
    verify(env, call, made["booking_reference"])
    r = reschedule(env, call, made["booking_reference"], SLOT + timedelta(minutes=15))
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("target", ["taken", "closed", "past_horizon"])
def test_failed_reschedule_preserves_original(env, target):
    made = book(env)
    call = execution()
    verify(env, call, made["booking_reference"])
    taken = SLOT + timedelta(hours=1)
    service.reserve(
        env["url"],
        CLINIC,
        ReservationRequest(doctor_id=DOCTOR_ID, service_id=SERVICE_ID, starts_at=taken),
        "synthetic-blocker",
    )
    new_start = {
        "taken": taken,
        "closed": datetime.combine(DAY, time(20, 0), IST),
        "past_horizon": SLOT + timedelta(days=400),
    }[target]
    before = snapshot(env["url"])
    r = reschedule(env, call, made["booking_reference"], new_start)
    assert r.status_code in (409, 422)
    if target != "past_horizon":
        assert r.json() == {"detail": bookings.UNAVAILABLE}
    assert snapshot(env["url"]) == before
    row = appointment(env["url"], made["appointment_id"])
    assert row["status"] == "pending_sync" and row["version"] == 1


def test_exclusion_violation_during_reschedule_preserves_original(env, monkeypatch):
    """Even if the slot check were bypassed, PostgreSQL's exclusion constraint rejects the
    overlap and the whole transaction (including the old appointment's cancel) rolls back."""
    made = book(env)
    call = execution()
    verify(env, call, made["booking_reference"])
    taken = SLOT + timedelta(hours=1)
    service.reserve(
        env["url"],
        CLINIC,
        ReservationRequest(doctor_id=DOCTOR_ID, service_id=SERVICE_ID, starts_at=taken),
        "synthetic-blocker",
    )
    original = service.slots

    def no_busy(conn, clinic_id, doctor_id, service_id, day, extra_busy=(), context=None):
        found = original(conn, clinic_id, doctor_id, service_id, day, extra_busy, context)
        return sorted({*found, taken.astimezone(found[0].tzinfo)})

    monkeypatch.setattr(service, "slots", no_busy)
    before = snapshot(env["url"])
    r = reschedule(env, call, made["booking_reference"], taken)
    assert r.status_code == 409 and r.json() == {"detail": bookings.UNAVAILABLE}
    assert snapshot(env["url"]) == before


def run_parallel(*calls):
    barrier = Barrier(len(calls))

    def run(fn):
        barrier.wait()
        return fn()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(run, calls))


def test_cancel_and_reschedule_race_one_outcome(env):
    target = datetime.combine(DAY, time(16, 0), IST)
    for _ in range(3):
        made = book(
            env,
            starts_at=SLOT + timedelta(hours=len(rows(env["url"], "SELECT id FROM appointments"))),
        )
        call = execution()
        verify(env, call, made["booking_reference"])
        results = run_parallel(
            lambda c=call, ref=made["booking_reference"]: cancel(env, c, ref),
            lambda c=call, ref=made["booking_reference"]: reschedule(env, c, ref, target),
        )
        codes = sorted(r.status_code for r in results)
        assert codes == [200, 409], [r.text for r in results]
        changes = rows(
            env["url"],
            "SELECT action FROM appointment_changes WHERE appointment_id=%s",
            (made["appointment_id"],),
        )
        assert len(changes) == 1
        successors = rows(
            env["url"],
            "SELECT status FROM appointments WHERE rescheduled_from=%s",
            (made["appointment_id"],),
        )
        if changes[0]["action"] == "reschedule":
            assert [s["status"] for s in successors] == ["pending_sync"]
            # Free the target again for the next round.
            with connect(env["url"]) as conn:
                conn.execute(
                    "UPDATE appointments SET status='cancelled' WHERE rescheduled_from=%s",
                    (made["appointment_id"],),
                )
        else:
            assert successors == []
        assert appointment(env["url"], made["appointment_id"])["status"] == "cancelled"


def test_concurrent_reschedules_one_wins(env):
    made = book(env)
    call = execution()
    verify(env, call, made["booking_reference"])
    results = run_parallel(
        lambda: reschedule(env, call, made["booking_reference"], SLOT + timedelta(hours=1)),
        lambda: reschedule(env, call, made["booking_reference"], SLOT + timedelta(hours=2)),
    )
    assert sorted(r.status_code for r in results) == [200, 409]
    assert (
        len(rows(env["url"], "SELECT id FROM appointments WHERE rescheduled_from IS NOT NULL")) == 1
    )
    active = rows(env["url"], "SELECT id FROM appointments WHERE status='pending_sync'")
    assert len(active) == 1


def test_identical_concurrent_reschedules_replay(env):
    made = book(env)
    call = execution()
    verify(env, call, made["booking_reference"])
    target = SLOT + timedelta(hours=1)
    results = run_parallel(*[lambda: reschedule(env, call, made["booking_reference"], target)] * 3)
    assert {r.status_code for r in results} == {200}
    assert len({r.json()["booking_reference"] for r in results}) == 1


# Worker interplay


def test_worker_projects_reschedule_and_ignores_stale_versions(env):
    fake = FakeCalendar()
    made = book(env)
    old_id = UUID(made["appointment_id"])
    assert run_once(env["url"], "w1", Ports(fake)) == 1
    assert appointment(env["url"], old_id)["status"] == "confirmed"
    old_event = event_id_for(old_id, 1)
    call = execution()
    assert "confirmed" in verify(env, call, made["booking_reference"]).json()["spoken"]
    out = reschedule(env, call, made["booking_reference"], SLOT + timedelta(hours=1)).json()
    assert "not yet confirmed" in out["spoken"]  # Never spoken as confirmed before the worker.
    while run_once(env["url"], "w1", Ports(fake)):
        pass
    [new] = rows(env["url"], "SELECT * FROM appointments WHERE rescheduled_from=%s", (old_id,))
    assert fake.get_event(CAL, old_event).status == "cancelled"
    assert fake.get_event(CAL, event_id_for(new["id"], 1)).status == "confirmed"
    assert new["status"] == "confirmed"
    assert appointment(env["url"], old_id)["status"] == "cancelled"
    assert {r["status"] for r in rows(env["url"], "SELECT status FROM outbox")} == {"done"}


def test_worker_after_unprojected_cancel_inserts_nothing(env):
    fake = FakeCalendar()
    made = book(env)
    call = execution()
    verify(env, call, made["booking_reference"])
    cancel(env, call, made["booking_reference"])
    while run_once(env["url"], "w1", Ports(fake)):
        pass
    # The stale create job (version 1 of a cancelled appointment) completed without writing.
    assert [c for c in fake.calls if c[0] == "insert_event"] == []
    assert {r["status"] for r in rows(env["url"], "SELECT status FROM outbox")} == {"done"}


# Staff routes


def test_staff_list_roles_and_filters(env):
    made = book(env)
    book(env, name=SIBLING, starts_at=SLOT + timedelta(hours=1))
    owner = staff_headers(env, "owner")
    viewer = staff_headers(env, "viewer")
    client = env["client"]
    r = client.get("/v1/admin/appointments", headers=owner)
    assert r.status_code == 200
    listed = r.json()
    assert [a["patient_name"] for a in listed] == [NAME, SIBLING]
    assert {a["phone_last2"] for a in listed} == {"23"}
    assert listed[0]["booking_reference"] == made["booking_reference"]
    assert "phone" not in str(set(listed[0]) - {"phone_last2"})
    assert CALLER not in r.text and "0000000123" not in r.text
    audits = rows(
        env["url"],
        "SELECT action, target_id FROM audit_events WHERE left(action, 6) <> 'staff.'",
    )
    # One row per appointment whose patient details were shown, targeting that appointment.
    assert sorted(str(a["target_id"]) for a in audits) == sorted(a["id"] for a in listed)
    assert {a["action"] for a in audits} == {"appointment.patient_details_viewed"}
    seen = client.get("/v1/admin/appointments", headers=viewer).json()
    assert [a["patient_name"] for a in seen] == [None, None]
    assert {a["phone_last2"] for a in seen} == {None}
    assert NAME not in str(seen)
    r = client.get(
        "/v1/admin/appointments",
        params={"from": (SLOT + timedelta(minutes=30)).isoformat(), "status": "pending_sync"},
        headers=viewer,
    )
    assert [a["patient_name"] for a in r.json()] == [None] and len(r.json()) == 1
    assert len(client.get("/v1/admin/appointments?limit=1", headers=viewer).json()) == 1
    assert client.get("/v1/admin/appointments?status=bogus", headers=viewer).status_code == 422
    other = staff_headers(env, "owner", OTHER_CLINIC)
    assert client.get("/v1/admin/appointments", headers=other).json() == []


def test_staff_cancel(env):
    made = book(env)
    client = env["client"]
    owner, viewer = staff_headers(env, "owner"), staff_headers(env, "viewer")
    path = f"/v1/admin/appointments/{made['appointment_id']}/cancel"
    assert client.post(path, headers=viewer).status_code == 403
    r = client.post(path, headers=owner)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert r.json()["patient_name"] is None
    assert client.post(path, headers=owner).status_code == 409
    jobs = rows(env["url"], "SELECT kind FROM outbox WHERE kind='calendar.cancel'")
    assert len(jobs) == 1
    [change] = rows(env["url"], "SELECT source, staff_user_id FROM appointment_changes")
    assert change["source"] == "staff" and change["staff_user_id"] is not None
    audits = rows(
        env["url"], "SELECT action, target_id FROM audit_events WHERE left(action, 6) <> 'staff.'"
    )
    assert audits == [
        {"action": "appointment.cancelled", "target_id": UUID(made["appointment_id"])}
    ]


def test_staff_routes_cross_tenant_404(env):
    made = book(env)
    other = staff_headers(env, "owner", OTHER_CLINIC)
    client = env["client"]
    for path, payload in (
        (f"/v1/admin/appointments/{made['appointment_id']}/cancel", None),
        (f"/v1/admin/appointments/{made['appointment_id']}/resolve", {"action": "cancel"}),
        (f"/v1/admin/appointments/{uuid4()}/cancel", None),
    ):
        r = client.post(path, json=payload, headers=other)
        assert r.status_code == 404 and r.json() == {"detail": "Not found"}
    assert appointment(env["url"], made["appointment_id"])["status"] == "pending_sync"


def make_needs_review(env, appointment_id, error="timeout"):
    with connect(env["url"]) as conn:
        conn.execute(
            "UPDATE outbox SET status='dead', last_error=%s WHERE appointment_id=%s",
            (error, appointment_id),
        )
        conn.execute("UPDATE appointments SET status='needs_review' WHERE id=%s", (appointment_id,))


def test_resolve_requeue_and_cancel(env):
    owner = staff_headers(env, "owner")
    client = env["client"]
    first = book(env)
    path = f"/v1/admin/appointments/{first['appointment_id']}/resolve"
    assert client.post(path, json={"action": "requeue"}, headers=owner).status_code == 409
    make_needs_review(env, first["appointment_id"])
    assert client.post(path, json={"action": "other"}, headers=owner).status_code == 422
    r = client.post(path, json={"action": "requeue"}, headers=owner)
    assert r.status_code == 200 and r.json()["status"] == "pending_sync"
    [job] = rows(
        env["url"], "SELECT status FROM outbox WHERE appointment_id=%s", (first["appointment_id"],)
    )
    assert job["status"] == "pending"
    second = book(env, starts_at=SLOT + timedelta(hours=1))
    make_needs_review(env, second["appointment_id"], error="unknown_kind")
    path = f"/v1/admin/appointments/{second['appointment_id']}/resolve"
    assert client.post(path, json={"action": "requeue"}, headers=owner).status_code == 409
    r = client.post(path, json={"action": "cancel"}, headers=owner)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    actions = [
        a["action"]
        for a in rows(
            env["url"],
            "SELECT action FROM audit_events WHERE left(action, 6) <> 'staff.' ORDER BY created_at",
        )
    ]
    assert actions == [
        "outbox.requeued",
        "appointment.resolved_requeue",
        "appointment.resolved_cancel",
    ]


def test_concurrent_resolve_and_requeue_do_not_deadlock(env):
    owner = staff_headers(env, "owner")
    client = env["client"]
    for hour in range(4):
        made = book(env, starts_at=SLOT + timedelta(hours=hour))
        make_needs_review(env, made["appointment_id"])
        [job] = rows(
            env["url"], "SELECT id FROM outbox WHERE appointment_id=%s", (made["appointment_id"],)
        )
        results = run_parallel(
            lambda a=made["appointment_id"]: client.post(
                f"/v1/admin/appointments/{a}/resolve", json={"action": "requeue"}, headers=owner
            ),
            lambda j=job["id"]: client.post(f"/v1/admin/outbox/{j}/requeue", headers=owner),
        )
        assert sorted(r.status_code for r in results) == [200, 409], [r.text for r in results]
        assert appointment(env["url"], made["appointment_id"])["status"] == "pending_sync"


# No patient data where it does not belong


def test_no_name_or_phone_outside_patient_ciphertext(env, caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeCalendar()
    made = book(env, consent_reminder_message="true")
    run_once(env["url"], "w1", Ports(fake))
    call = execution()
    responses = [
        verify(env, call, made["booking_reference"]),
        verify(env, execution(), "WRONGREF", number=OTHER_CALLER),
        reschedule(env, call, made["booking_reference"], SLOT + timedelta(hours=1)),
        reschedule(env, call, made["booking_reference"], SLOT + timedelta(hours=2)),
        cancel(env, call, "ZZZZ"),
    ]
    while run_once(env["url"], "w1", Ports(fake)):
        pass
    owner = staff_headers(env, "owner")
    listing = env["client"].get("/v1/admin/appointments", headers=owner)
    new_id = listing.json()[-1]["id"]
    responses.append(env["client"].post(f"/v1/admin/appointments/{new_id}/cancel", headers=owner))
    responses.append(env["client"].post(f"/v1/admin/appointments/{new_id}/cancel", headers=owner))
    secrets = (NAME, "Alpha", CALLER, CALLER[1:], "0000000123", OTHER_CALLER[1:])
    stored = " ".join(
        str(row)
        for table in (
            "audit_events",
            "outbox",
            "appointments",
            "appointment_changes",
            "consents",
            "call_session_verifications",
            "call_session_verification_failures",
        )
        for row in rows(env["url"], f"SELECT t::text AS r FROM {table} t")  # noqa: S608
    )
    events = " ".join(f"{e.summary} {e.description}" for e in fake._events.values())
    texts = [stored, events, caplog.text] + [r.text for r in responses]
    for text in texts:
        for secret in secrets:
            assert secret not in text
    assert fake._events  # The calendar was written, opaquely.


# Retention


def test_retention_purge(env, capsys):
    expired = book(env)
    kept = book(env, name=SIBLING, starts_at=SLOT + timedelta(hours=1))
    call = execution()
    verify(env, call, expired["booking_reference"])
    cancel(env, call, expired["booking_reference"])
    with connect(env["url"]) as conn:
        expired_patient = appointment(env["url"], expired["appointment_id"])["patient_id"]
        kept_patient = appointment(env["url"], kept["appointment_id"])["patient_id"]
        conn.execute("UPDATE patients SET retention_until = now() - interval '1 day'")
        conn.execute("UPDATE call_sessions SET started_at = now() - interval '91 days'")
    counts_before = snapshot(env["url"])
    cli.main(["retention", "purge", "--dry-run"])
    out = capsys.readouterr().out
    assert "patients=1" in out and "call_sessions=3" in out and "dry_run=true" in out
    assert snapshot(env["url"]) == counts_before
    assert len(rows(env["url"], "SELECT id FROM patients")) == 2
    cli.main(["retention", "purge"])
    assert "dry_run=false" in capsys.readouterr().out
    # The patient with an active future appointment stays, whatever its retention date.
    assert [r["id"] for r in rows(env["url"], "SELECT id FROM patients")] == [kept_patient]
    assert expired_patient is not None
    assert rows(env["url"], "SELECT id FROM call_sessions") == []
    assert rows(env["url"], "SELECT * FROM call_session_verifications") == []
    assert appointment(env["url"], expired["appointment_id"])["patient_id"] is None
    assert {
        r["call_session_id"] for r in rows(env["url"], "SELECT call_session_id FROM appointments")
    } == {None}
    [change] = rows(env["url"], "SELECT call_session_id FROM appointment_changes")
    assert change["call_session_id"] is None
    assert appointment(env["url"], kept["appointment_id"])["status"] == "pending_sync"


# T6 review follow-ups


def test_authentication_precedes_duplicate_key_check(env):
    first, second = execution(), execution()
    raw = f'{{"execution_id": "{first}", "execution_id": "{second}"}}'
    r = env["client"].post(VERIFY, content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code == 401
    r = env["client"].post(
        VERIFY,
        content=raw,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {env['token']}"},
    )
    assert r.status_code == 422


def test_appointment_change_helpers_reject_inactive(env):
    made = book(env)
    with connect(env["url"]) as conn:
        row = appointments.lock_appointment(conn, CLINIC, made["appointment_id"])
        appointments.cancel_locked(conn, row)
        with pytest.raises(RuntimeError):
            appointments.cancel_locked(conn, row)  # Stale version: refused.
