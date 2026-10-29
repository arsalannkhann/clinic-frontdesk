from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from conftest import activate_lines
from fastapi.testclient import TestClient

from clinic_voice import credentials
from clinic_voice.api import app
from clinic_voice.cli import DEMO_CLINIC_ID as CLINIC
from clinic_voice.cli import DOCTOR_ID, SERVICE_ID
from clinic_voice.config import get_settings
from clinic_voice.db import connect


@pytest.fixture
def client(database, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database)
    get_settings.cache_clear()
    with TestClient(app) as value:
        yield value
    get_settings.cache_clear()


def issue(database, clinic=CLINIC, line=True, **kwargs):
    with connect(database) as conn:
        if line:
            activate_lines(conn, clinic)
        return credentials.issue(conn, clinic, "synthetic", **kwargs)


def check(client, token, doctor=DOCTOR_ID, service=SERVICE_ID):
    day = datetime.now(ZoneInfo("Asia/Kolkata")).date() + timedelta(days=1)
    return client.post(
        "/v1/tools/check-slots",
        headers={"Authorization": "Bearer " + token},
        json={"doctor_id": str(doctor), "service_id": str(service), "day": day.isoformat()},
    )


def other_clinic(database):
    clinic, doctor, service = uuid4(), uuid4(), uuid4()
    with connect(database) as conn:
        conn.execute("INSERT INTO clinics (id,name) VALUES (%s,'Synthetic Other')", (clinic,))
        conn.execute("INSERT INTO doctors VALUES (%s,%s,'Other Doctor')", (doctor, clinic))
        conn.execute(
            "INSERT INTO services VALUES (%s,%s,%s,'Other Visit',15)", (service, clinic, doctor)
        )
        conn.execute(
            "INSERT INTO working_hours SELECT %s,%s,d,'09:00','17:00' FROM generate_series(0,6) d",
            (clinic, doctor),
        )
    return clinic, doctor, service


def test_only_digest_is_stored(database):
    credential_id, token = issue(database)
    secret = token.split(".", 1)[1]
    with connect(database) as conn:
        row = conn.execute(
            "SELECT * FROM tenant_credentials WHERE id=%s", (credential_id,)
        ).fetchone()
    assert bytes(row["secret_hash"]) == credentials.digest(secret)
    assert secret not in repr(dict(row))


def test_issued_credential_resolves_its_own_tenant(database, client):
    _, token = issue(database)
    r = check(client, token)
    assert r.status_code == 200
    assert len(r.json()["slots"]) == 3


def test_tenant_a_cannot_reference_tenant_b_objects(database, client):
    _, token_a = issue(database)
    clinic_b, doctor_b, service_b = other_clinic(database)
    _, token_b = issue(database, clinic_b)
    assert check(client, token_a, doctor_b, service_b).status_code == 404
    assert check(client, token_b, DOCTOR_ID, SERVICE_ID).status_code == 404
    assert check(client, token_b, doctor_b, service_b).status_code == 200


def test_wrong_secret_for_real_credential_fails(database, client):
    _, token = issue(database)
    forged = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert check(client, forged).status_code == 401


def test_revoked_credential_fails(database, client):
    credential_id, token = issue(database)
    assert check(client, token).status_code == 200
    with connect(database) as conn:
        assert credentials.revoke(conn, credential_id)
        assert not credentials.revoke(conn, credential_id)
    r = check(client, token)
    assert r.status_code == 401
    assert r.json() == {"detail": "Invalid credentials"}


def test_expired_credential_fails(database, client):
    credential_id, token = issue(database)
    with connect(database) as conn:
        # Use the database clock; host and container clocks can differ.
        conn.execute(
            "UPDATE tenant_credentials SET expires_at=now() - interval '1 second' WHERE id=%s",
            (credential_id,),
        )
    assert check(client, token).status_code == 401


def test_rotation_grace_then_old_expires(database, client):
    old_id, old_token = issue(database)
    with connect(database) as conn:
        new_id, new_token = credentials.rotate(conn, old_id, timedelta(hours=1))
        assert (
            conn.execute(
                "SELECT rotated_from FROM tenant_credentials WHERE id=%s", (new_id,)
            ).fetchone()["rotated_from"]
            == old_id
        )
    assert check(client, old_token).status_code == 200
    assert check(client, new_token).status_code == 200
    with connect(database) as conn:
        # Simulate the grace period elapsing.
        conn.execute(
            "UPDATE tenant_credentials SET expires_at=now() - interval '1 second' WHERE id=%s",
            (old_id,),
        )
        with pytest.raises(LookupError):
            credentials.rotate(conn, old_id, timedelta(hours=1))
    assert check(client, old_token).status_code == 401
    assert check(client, new_token).status_code == 200


def test_repeat_rotation_never_extends_grace(database):
    old_id, _ = issue(database)
    with connect(database) as conn:
        credentials.rotate(conn, old_id, timedelta(minutes=5))
        first = conn.execute(
            "SELECT expires_at FROM tenant_credentials WHERE id=%s", (old_id,)
        ).fetchone()["expires_at"]
        credentials.rotate(conn, old_id, timedelta(days=7))
        second = conn.execute(
            "SELECT expires_at FROM tenant_credentials WHERE id=%s", (old_id,)
        ).fetchone()["expires_at"]
    assert second == first


def test_cli_rejects_unknown_clinic(database, monkeypatch):
    from clinic_voice.cli import main

    monkeypatch.setenv("DATABASE_URL", database)
    get_settings.cache_clear()
    monkeypatch.setattr(
        "sys.argv",
        ["clinic-voice", "credential", "issue", "--clinic", str(uuid4()), "--label", "x"],
    )
    try:
        with pytest.raises(SystemExit, match="Clinic not found"):
            main()
    finally:
        get_settings.cache_clear()


def test_rotation_without_grace_is_immediate(database, client):
    old_id, old_token = issue(database)
    with connect(database) as conn:
        _, new_token = credentials.rotate(conn, old_id, timedelta(0))
    assert check(client, old_token).status_code == 401
    assert check(client, new_token).status_code == 200


def test_revoked_credential_cannot_be_rotated(database):
    credential_id, _ = issue(database)
    with connect(database) as conn:
        credentials.revoke(conn, credential_id)
        with pytest.raises(LookupError):
            credentials.rotate(conn, credential_id, timedelta(hours=1))


def test_last_used_is_recorded_coarsely(database, client):
    credential_id, token = issue(database)
    check(client, token)
    with connect(database) as conn:
        first = conn.execute(
            "SELECT last_used_at FROM tenant_credentials WHERE id=%s", (credential_id,)
        ).fetchone()["last_used_at"]
    check(client, token)
    with connect(database) as conn:
        second = conn.execute(
            "SELECT last_used_at FROM tenant_credentials WHERE id=%s", (credential_id,)
        ).fetchone()["last_used_at"]
    assert first is not None and first == second


def test_unknown_credential_id_fails(database, client):
    _, token = issue(database)
    unknown = "cvt_" + uuid4().hex + "." + token.split(".", 1)[1]
    assert check(client, unknown).status_code == 401


def test_cli_issue_list_revoke(database, monkeypatch, capsys):
    from clinic_voice.cli import main

    monkeypatch.setenv("DATABASE_URL", database)
    get_settings.cache_clear()
    try:
        monkeypatch.setattr(
            "sys.argv",
            ["clinic-voice", "credential", "issue", "--clinic", str(CLINIC), "--label", "cli"],
        )
        main()
        issued = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
        monkeypatch.setattr(
            "sys.argv", ["clinic-voice", "credential", "list", "--clinic", str(CLINIC)]
        )
        main()
        listing = capsys.readouterr().out
        assert issued["credential_id"] in listing
        assert issued["token"].split(".", 1)[1] not in listing
        monkeypatch.setattr(
            "sys.argv", ["clinic-voice", "credential", "revoke", issued["credential_id"]]
        )
        main()
        with connect(database) as conn:
            assert credentials.authenticate(conn, issued["token"]) is None
    finally:
        get_settings.cache_clear()


# Scopes and line gating of the generic tool API (T12)


def reserve(client, token, key="t12-reserve-0001"):
    day = datetime.now(ZoneInfo("Asia/Kolkata")).date() + timedelta(days=1)
    starts = datetime.combine(day, datetime.min.time().replace(hour=10), ZoneInfo("Asia/Kolkata"))
    return client.post(
        "/v1/tools/reserve-slot",
        headers={"Authorization": "Bearer " + token, "Idempotency-Key": key},
        json={
            "doctor_id": str(DOCTOR_ID),
            "service_id": str(SERVICE_ID),
            "starts_at": starts.isoformat(),
        },
    )


def test_bolna_scope_is_refused_by_the_tool_api_with_a_generic_401(database, client):
    credential_id, token = issue(database, scope="bolna")
    assert token.startswith("cvb_")
    paths = {
        "/v1/tools/check-slots": {},
        "/v1/tools/appointment-status": {"appointment_id": str(uuid4())},
    }
    for path, body in paths.items():
        r = client.post(path, headers={"Authorization": "Bearer " + token}, json=body)
        assert r.status_code == 401, path
        assert r.json() == {"detail": "Invalid credentials"}
    r = reserve(client, token)
    assert r.status_code == 401
    assert r.json() == {"detail": "Invalid credentials"}
    # Same id and secret under the tools prefix: the stored scope still refuses it.
    assert check(client, "cvt_" + token[4:]).status_code == 401
    # The gateway accepts it (and a tools credential, for hand-configured clinics).
    _, tools_token = issue(database)
    for value in (token, tools_token):
        r = client.post("/v1/bolna/context", headers={"Authorization": "Bearer " + value})
        assert r.status_code == 200, r.text
    with connect(database) as conn:
        assert credentials.authenticate(conn, token, "bolna") == CLINIC
        assert credentials.authenticate(conn, token) is None
        count = conn.execute(
            "SELECT count(*) AS n FROM appointments WHERE clinic_id=%s", (CLINIC,)
        ).fetchone()["n"]
    assert count == 0


def test_tool_api_is_line_gated(database, client):
    """A tools credential on a clinic that is not active: reads need verified or active,
    reserve needs active (or verified with test_calls_enabled). Refused before any write."""
    _, token = issue(database, line=False)  # No provisioning row: not active.
    assert check(client, token).status_code == 403
    refused = reserve(client, token)
    assert refused.status_code == 403
    assert "isn't accepting bookings" in refused.json()["detail"]
    with connect(database) as conn:
        conn.execute(
            "INSERT INTO provisioning (clinic_id,state,resume_state,agent_id,phone_number_id)"
            " VALUES (%s,'verified','verified',%s,'synthetic-number')",
            (CLINIC, str(uuid4())),
        )
    assert check(client, token).status_code == 200  # Reads open once verified.
    assert reserve(client, token).status_code == 403  # Writes need active or test calls.
    r = client.post(
        "/v1/tools/appointment-status",
        headers={"Authorization": "Bearer " + token},
        json={"appointment_id": str(uuid4())},
    )
    assert r.status_code == 404
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) AS n FROM appointments").fetchone()["n"] == 0
        conn.execute("UPDATE clinics SET test_calls_enabled=true WHERE id=%s", (CLINIC,))
    booked = reserve(client, token)
    assert booked.status_code == 202, booked.text
    with connect(database) as conn:
        conn.execute("UPDATE provisioning SET state='manual_review' WHERE clinic_id=%s", (CLINIC,))
    # A replay is refused too once the line closed (the early check runs first).
    assert reserve(client, token).status_code == 403
    assert check(client, token).status_code == 403


def test_tool_reserve_records_its_credential(database, client):
    credential_id, token = issue(database)
    booked = reserve(client, token)
    assert booked.status_code == 202
    with connect(database) as conn:
        row = conn.execute(
            "SELECT tool_credential_id FROM appointments WHERE id=%s",
            (booked.json()["appointment_id"],),
        ).fetchone()
    assert row["tool_credential_id"] == credential_id
    r = client.post(
        "/v1/tools/appointment-status",
        headers={"Authorization": "Bearer " + token},
        json={"appointment_id": booked.json()["appointment_id"]},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "pending_sync"


def test_cli_issues_bolna_scope(database, monkeypatch, capsys):
    from clinic_voice.cli import main

    monkeypatch.setenv("DATABASE_URL", database)
    get_settings.cache_clear()
    try:
        monkeypatch.setattr(
            "sys.argv",
            [
                "clinic-voice",
                "credential",
                "issue",
                "--clinic",
                str(CLINIC),
                "--label",
                "hand-configured agent",
                "--scope",
                "bolna",
            ],
        )
        main()
        issued = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
        assert issued["token"].startswith("cvb_")
        assert "webhook_path" not in issued
        with connect(database) as conn:
            assert credentials.authenticate(conn, issued["token"], "bolna") == CLINIC
            assert credentials.authenticate(conn, issued["token"]) is None
    finally:
        get_settings.cache_clear()


def _lock_waiters(database, count, timeout=5.0):
    """Poll until at least `count` other backends wait on a lock."""
    from time import monotonic, sleep

    deadline = monotonic() + timeout
    with connect(database) as conn:
        conn.autocommit = True
        while monotonic() < deadline:
            waiting = conn.execute(
                "SELECT count(*) AS n FROM pg_stat_activity WHERE datname=current_database()"
                " AND wait_event_type='Lock' AND pid <> pg_backend_pid()"
            ).fetchone()["n"]
            if waiting >= count:
                return
            sleep(0.01)
    raise AssertionError(f"fewer than {count} backends waited for a lock")


def _verified_with_test_calls(database):
    with connect(database) as conn:
        conn.execute(
            "INSERT INTO provisioning (clinic_id,state,resume_state,agent_id,phone_number_id)"
            " VALUES (%s,'verified','verified',%s,'synthetic-number')",
            (CLINIC, str(uuid4())),
        )
        conn.execute("UPDATE clinics SET test_calls_enabled=true WHERE id=%s", (CLINIC,))


def _appointments(database):
    with connect(database) as conn:
        return conn.execute("SELECT count(*) AS n FROM appointments").fetchone()["n"]


def test_disabling_test_calls_waits_for_an_in_flight_reserve(database, client):
    """Reserve passed its in-transaction gate (clinic row FOR SHARE) and is held at the doctor
    row lock: turning test calls off waits for it, so the booking commits before the line
    closes, and the next reserve is refused."""
    from threading import Thread

    _, token = issue(database, line=False)
    _verified_with_test_calls(database)
    result = {}
    with connect(database) as holder:
        holder.execute("SELECT id FROM doctors WHERE id=%s FOR UPDATE", (DOCTOR_ID,))
        booking = Thread(target=lambda: result.setdefault("reserve", reserve(client, token)))
        booking.start()
        _lock_waiters(database, 1)  # The reserve, past its gate, waits for the doctor row.

        def disable():
            with connect(database) as conn:
                conn.execute("UPDATE clinics SET test_calls_enabled=false WHERE id=%s", (CLINIC,))
            result["disabled"] = True

        toggle = Thread(target=disable)
        toggle.start()
        _lock_waiters(database, 2)  # The toggle waits for the reserve's clinic row share lock.
        assert "disabled" not in result
        holder.rollback()
    booking.join(10)
    toggle.join(10)
    assert result["reserve"].status_code == 202
    assert result["disabled"] is True
    assert _appointments(database) == 1
    assert reserve(client, token, key="t12-reserve-0002").status_code == 403
    assert _appointments(database) == 1


def test_reserve_waits_for_a_test_calls_toggle_and_is_refused(database, client):
    """The toggle holds the clinic row (uncommitted): the reserve's in-transaction gate waits
    for it and, once it commits, refuses without writing anything."""
    from threading import Thread

    _, token = issue(database, line=False)
    _verified_with_test_calls(database)
    result = {}
    holder = connect(database)
    try:
        holder.execute("UPDATE clinics SET test_calls_enabled=false WHERE id=%s", (CLINIC,))
        # The early check reads committed state (test calls still on), so it passes; the gate
        # inside the write transaction then waits for the holder's row lock.
        booking = Thread(target=lambda: result.setdefault("reserve", reserve(client, token)))
        booking.start()
        _lock_waiters(database, 1)
        holder.commit()
    finally:
        holder.close()
    booking.join(10)
    assert result["reserve"].status_code == 403
    assert _appointments(database) == 0


def test_racing_reserves_and_toggle_off_stay_consistent(database, client):
    """Smoke race: every reserve either commits (202, an appointment row) or is refused
    (403, nothing written); after the toggle commits no reserve succeeds."""
    from threading import Barrier, Thread

    _, token = issue(database, line=False)
    _verified_with_test_calls(database)
    barrier = Barrier(4)
    codes = []

    def book(index):
        barrier.wait()
        codes.append(reserve(client, token, key=f"t12-race-{index:04d}").status_code)

    def disable():
        barrier.wait()
        with connect(database) as conn:
            conn.execute("UPDATE clinics SET test_calls_enabled=false WHERE id=%s", (CLINIC,))

    threads = [Thread(target=book, args=(i,)) for i in range(3)] + [Thread(target=disable)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert set(codes) <= {202, 403, 409}  # 409: two racers picked the same slot.
    assert _appointments(database) == codes.count(202)
    assert reserve(client, token, key="t12-race-after").status_code == 403
    assert _appointments(database) == codes.count(202)
