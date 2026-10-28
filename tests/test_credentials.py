from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
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


def issue(database, clinic=CLINIC, **kwargs):
    with connect(database) as conn:
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
    _, token = issue(database, expires_at=datetime.now(ZoneInfo("UTC")) - timedelta(seconds=1))
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
