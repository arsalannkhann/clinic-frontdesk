import io
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from clinic_voice import credentials, staff
from clinic_voice.api import app
from clinic_voice.cli import DEMO_CLINIC_ID as CLINIC
from clinic_voice.config import get_settings
from clinic_voice.db import connect

PASSWORD = "synthetic-password-01"


@pytest.fixture(autouse=True)
def cheap_hashing(monkeypatch):
    # Production cost is deliberately slow; tests only need the same code path.
    monkeypatch.setattr(staff, "SCRYPT_N", 2**10)


@pytest.fixture
def client(database, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database)
    get_settings.cache_clear()
    with TestClient(app) as value:
        yield value
    get_settings.cache_clear()


def make_user(database, role="owner", clinic=CLINIC, email=None):
    email = email or f"{role}-{uuid4().hex[:8]}@example.test"
    with connect(database) as conn:
        user = staff.create_user(conn, clinic, email, role, PASSWORD)
    return user | {"email": email}


def login(client, email, password=PASSWORD):
    return client.post("/v1/staff/login", json={"email": email, "password": password})


def auth(token):
    return {"Authorization": "Bearer " + token}


def session(client, user):
    r = login(client, user["email"])
    assert r.status_code == 200, r.text
    return auth(r.json()["token"])


def other_clinic(database):
    clinic = uuid4()
    with connect(database) as conn:
        conn.execute("INSERT INTO clinics (id,name) VALUES (%s,'Synthetic Other')", (clinic,))
    return clinic


# Pure checks


def test_password_hash_roundtrip():
    stored = staff.hash_password(PASSWORD)
    assert PASSWORD not in stored
    assert staff.verify_password(PASSWORD, stored)
    assert not staff.verify_password(PASSWORD + "x", stored)
    assert not staff.verify_password(PASSWORD, "garbage")
    assert not staff.verify_password(PASSWORD, "bcrypt$1$2$3$x$y")


def test_needs_rehash_tracks_cost(monkeypatch):
    stored = staff.hash_password(PASSWORD)
    assert not staff.needs_rehash(stored)
    monkeypatch.setattr(staff, "SCRYPT_N", 2**11)
    assert staff.needs_rehash(stored)


def test_token_kinds_do_not_cross():
    tool = "cvt_" + "0" * 32 + "." + "A" * 43
    assert staff.parse_session(tool) is None
    assert credentials.parse("cvs_" + "0" * 32 + "." + "A" * 43) is None


def test_login_validation_does_not_echo_password(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused/unused")
    get_settings.cache_clear()
    with TestClient(app) as client:
        r = client.post("/v1/staff/login", json={"email": "not-an-email", "password": PASSWORD})
        assert r.status_code == 422
        assert PASSWORD not in r.text
        tool = "cvt_" + "0" * 32 + "." + "A" * 43
        assert client.get("/v1/staff/me", headers=auth(tool)).status_code == 401
    get_settings.cache_clear()


# PostgreSQL behavior


def test_login_and_me(database, client):
    user = make_user(database, "admin", email="Admin.Case@Example.test")
    r = login(client, "  admin.case@example.TEST ")
    assert r.status_code == 200
    token = r.json()["token"]
    me = client.get("/v1/staff/me", headers=auth(token))
    assert me.json() == {"id": str(user["id"]), "email": "admin.case@example.test", "role": "admin"}
    with connect(database) as conn:
        stored = conn.execute("SELECT secret_hash FROM staff_sessions").fetchone()
    assert bytes(stored["secret_hash"]) == credentials.digest(token.split(".", 1)[1])


def test_login_failures_are_indistinguishable(database, client):
    disabled = make_user(database, "viewer")
    with connect(database) as conn:
        conn.execute("UPDATE staff_users SET disabled_at=now() WHERE id=%s", (disabled["id"],))
    owner = make_user(database)
    responses = [
        login(client, owner["email"], "wrong-password-000"),
        login(client, "nobody@example.test"),
        login(client, disabled["email"]),
    ]
    assert {r.status_code for r in responses} == {401}
    assert len({r.text for r in responses}) == 1


def test_lockout_and_expiry(database, client):
    user = make_user(database)
    for _ in range(staff.MAX_FAILED_LOGINS):
        assert login(client, user["email"], "wrong-password-000").status_code == 401
    assert login(client, user["email"]).status_code == 401  # Locked even with the right password.
    with connect(database) as conn:
        conn.execute(
            "UPDATE staff_users SET locked_until=now() - interval '1 second' WHERE id=%s",
            (user["id"],),
        )
    # After the lock expires one failure restarts the count rather than relocking.
    assert login(client, user["email"], "wrong-password-000").status_code == 401
    assert login(client, user["email"]).status_code == 200
    with connect(database) as conn:
        row = conn.execute(
            "SELECT failed_logins, locked_until FROM staff_users WHERE id=%s", (user["id"],)
        ).fetchone()
    assert row == {"failed_logins": 0, "locked_until": None}


def test_logout_and_session_expiry(database, client):
    user = make_user(database)
    first, second = session(client, user), session(client, user)
    assert client.post("/v1/staff/logout", headers=first).status_code == 204
    assert client.get("/v1/staff/me", headers=first).status_code == 401
    assert client.get("/v1/staff/me", headers=second).status_code == 200
    with connect(database) as conn:
        conn.execute("UPDATE staff_sessions SET expires_at=now() - interval '1 second'")
    assert client.get("/v1/staff/me", headers=second).status_code == 401


def test_role_matrix(database, client):
    viewer = session(client, make_user(database, "viewer"))
    admin = session(client, make_user(database, "admin"))
    owner = session(client, make_user(database, "owner"))
    new = {"email": "new@example.test", "role": "viewer", "temporary_password": PASSWORD}
    assert client.get("/v1/staff/users", headers=viewer).status_code == 403
    assert client.get("/v1/staff/users", headers=admin).status_code == 200
    assert client.post("/v1/staff/users", headers=admin, json=new).status_code == 403
    created = client.post("/v1/staff/users", headers=owner, json=new)
    assert created.status_code == 201
    assert client.post("/v1/staff/users", headers=owner, json=new).status_code == 409
    assert login(client, "new@example.test").status_code == 200
    patch = {"role": "admin"}
    target = created.json()["id"]
    assert client.patch(f"/v1/staff/users/{target}", headers=admin, json=patch).status_code == 403


def test_tenant_isolation(database, client):
    owner_a = session(client, make_user(database))
    clinic_b = other_clinic(database)
    user_b = make_user(database, "viewer", clinic=clinic_b)
    listing = client.get("/v1/staff/users", headers=owner_a).json()
    assert str(user_b["id"]) not in {row["id"] for row in listing}
    r = client.patch(f"/v1/staff/users/{user_b['id']}", headers=owner_a, json={"disabled": True})
    assert r.status_code == 404
    with connect(database) as conn:
        assert (
            conn.execute(
                "SELECT disabled_at FROM staff_users WHERE id=%s", (user_b["id"],)
            ).fetchone()["disabled_at"]
            is None
        )


def test_body_cannot_select_tenant(database, client):
    owner = session(client, make_user(database))
    body = {
        "email": "x@example.test",
        "role": "viewer",
        "temporary_password": PASSWORD,
        "clinic_id": str(other_clinic(database)),
    }
    assert client.post("/v1/staff/users", headers=owner, json=body).status_code == 422


def test_tool_and_staff_tokens_are_not_interchangeable(database, client):
    owner = session(client, make_user(database))
    with connect(database) as conn:
        _, tool_token = credentials.issue(conn, CLINIC, "synthetic")
    assert client.get("/v1/staff/me", headers=auth(tool_token)).status_code == 401
    r = client.post("/v1/tools/check-slots", headers=owner, json={})
    assert r.status_code == 401


def test_password_change_revokes_other_sessions(database, client):
    user = make_user(database)
    current, other = session(client, user), session(client, user)
    wrong = {"current_password": "wrong-password-000", "new_password": "another-password-02"}
    assert client.post("/v1/staff/me/password", headers=current, json=wrong).status_code == 403
    change = {"current_password": PASSWORD, "new_password": "another-password-02"}
    assert client.post("/v1/staff/me/password", headers=current, json=change).status_code == 204
    assert client.get("/v1/staff/me", headers=current).status_code == 200
    assert client.get("/v1/staff/me", headers=other).status_code == 401
    assert login(client, user["email"]).status_code == 401
    assert login(client, user["email"], "another-password-02").status_code == 200


def test_disable_and_demote_take_effect_immediately(database, client):
    owner = session(client, make_user(database))
    admin_user = make_user(database, "admin")
    admin = session(client, admin_user)
    url = f"/v1/staff/users/{admin_user['id']}"
    assert client.patch(url, headers=owner, json={"role": "viewer"}).status_code == 200
    assert client.get("/v1/staff/users", headers=admin).status_code == 403
    r = client.patch(url, headers=owner, json={"disabled": True})
    assert r.json()["disabled"] is True
    assert client.get("/v1/staff/me", headers=admin).status_code == 401
    assert login(client, admin_user["email"]).status_code == 401
    with connect(database) as conn:
        open_sessions = conn.execute(
            "SELECT count(*) AS n FROM staff_sessions WHERE user_id=%s AND revoked_at IS NULL",
            (admin_user["id"],),
        ).fetchone()["n"]
    assert open_sessions == 0


def test_last_owner_is_protected(database, client):
    user = make_user(database)
    owner = session(client, user)
    url = f"/v1/staff/users/{user['id']}"
    assert client.patch(url, headers=owner, json={"role": "admin"}).status_code == 409
    assert client.patch(url, headers=owner, json={"disabled": True}).status_code == 409
    assert client.get("/v1/staff/me", headers=owner).json()["role"] == "owner"


def test_concurrent_mutual_demotion_leaves_one_owner(database, client):
    a, b = make_user(database), make_user(database)
    with connect(database) as conn:
        contexts = {}
        for user in (a, b):
            session_token = staff.login(conn, user["email"], PASSWORD)["token"]
            contexts[user["id"]] = staff.authenticate_session(conn, session_token)
    barrier = Barrier(2)

    def demote(pair):
        actor, target = pair
        barrier.wait()
        try:
            with connect(database) as conn:
                staff.update_user(conn, contexts[actor["id"]], target["id"], role="admin")
            return 200
        except Exception as error:  # HTTPException from the losing transaction
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(demote, [(a, b), (b, a)]))
    assert outcomes[0] == 200 and outcomes[1] in (403, 409)
    with connect(database) as conn:
        owners = conn.execute(
            "SELECT count(*) AS n FROM staff_users WHERE clinic_id=%s AND role='owner'", (CLINIC,)
        ).fetchone()["n"]
    assert owners == 1


def test_audit_trail_minimizes_data(database, client):
    owner_user = make_user(database)
    owner = session(client, owner_user)
    login(client, owner_user["email"], "wrong-password-000")
    client.post("/v1/staff/logout", headers=owner)
    with connect(database) as conn:
        rows = conn.execute(
            "SELECT * FROM audit_events WHERE clinic_id=%s ORDER BY created_at", (CLINIC,)
        ).fetchall()
    actions = [row["action"] for row in rows]
    assert actions == ["staff.user_created", "staff.login", "staff.login_failed", "staff.logout"]
    assert owner_user["email"] not in repr(rows) and PASSWORD not in repr(rows)


def test_rehash_on_login(database, client, monkeypatch):
    user = make_user(database)
    monkeypatch.setattr(staff, "SCRYPT_N", 2**11)
    assert login(client, user["email"]).status_code == 200
    with connect(database) as conn:
        stored = conn.execute(
            "SELECT password_hash FROM staff_users WHERE id=%s", (user["id"],)
        ).fetchone()["password_hash"]
    assert stored.split("$")[1] == str(2**11)
    assert login(client, user["email"]).status_code == 200


def test_cli_create_owner_and_reset(database, client, monkeypatch, capsys):
    from clinic_voice.cli import main

    monkeypatch.setenv("DATABASE_URL", database)
    get_settings.cache_clear()
    email = "cli-owner@example.test"
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    monkeypatch.setattr(
        "sys.argv",
        ["clinic-voice", "staff", "create-owner", "--clinic", str(CLINIC), "--email", email],
    )
    main()
    assert "created owner" in capsys.readouterr().out
    before = session(client, {"email": email})
    for _ in range(staff.MAX_FAILED_LOGINS):
        login(client, email, "wrong-password-000")
    monkeypatch.setattr("sys.stdin", io.StringIO("recovered-password-03\n"))
    monkeypatch.setattr("sys.argv", ["clinic-voice", "staff", "set-password", "--email", email])
    main()
    assert client.get("/v1/staff/me", headers=before).status_code == 401
    assert login(client, email, "recovered-password-03").status_code == 200
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    with pytest.raises(SystemExit, match="12 to 256"):
        main()
    get_settings.cache_clear()
