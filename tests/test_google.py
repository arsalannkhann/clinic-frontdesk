"""Google OAuth connection, the Google Calendar adapter and worker integration.

All Google traffic goes to FakeGoogle through httpx.MockTransport: no network, no real
credentials. Client ids, secrets and tokens are synthetic.
"""

import base64
import json
import logging
import os
from datetime import UTC, datetime, time, timedelta
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from clinic_voice import calendar_accounts, service, staff
from clinic_voice.api import StripQuery, app
from clinic_voice.cli import DEMO_CLINIC_ID as CLINIC
from clinic_voice.cli import DOCTOR_ID, SERVICE_ID
from clinic_voice.config import Settings, get_settings
from clinic_voice.crypto import Keyring
from clinic_voice.db import connect
from clinic_voice.outbox import Ports, check_ports, run_once
from clinic_voice.providers import google_calendar as google
from clinic_voice.providers.calendar import (
    CalendarEvent,
    EventAlreadyExists,
    FakeCalendar,
    ProviderPermanent,
    ProviderRetryable,
    ProviderTimeout,
    event_id_for,
)
from clinic_voice.routes_calendar import google_http
from clinic_voice.schemas import ReservationRequest

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2031, 3, 3, 8, 30, tzinfo=IST)
DAY = NOW.date() + timedelta(days=2)
PASSWORD = "synthetic-password-01"
CLIENT_ID = "synthetic-client.apps.googleusercontent.test"
CLIENT_SECRET = "synthetic-client-secret-0001"
REDIRECT = "https://clinic.example.test/v1/oauth/google/callback"
KEYS = "k1:" + base64.b64encode(b"\x01" * 32).decode()
CODE = "synthetic-auth-code-0001"
REFRESH = "synthetic-refresh-token-0001"
CAL = "booking@example.test"
BUSY = "busy@example.test"
SECRET_MARKER = "SYNTHETIC-BODY-MARKER"


class FakeGoogle:
    """Minimal stand-in for Google's token, revoke, events and freeBusy endpoints.

    Grant-wide revocation (grant_wide=True, the default, as Google documents it): revoking
    any token of a fake user invalidates every refresh and access token of that user issued
    so far. Refresh tokens not seen before (e.g. stored by make_account) belong to `user`.
    """

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.token_queue: list = []
        self.api_queue: list = []
        self.events: dict[tuple[str, str], dict] = {}
        self.busy: dict[str, list[dict]] = {}
        self.fb_errors: dict[str, list[dict]] = {}
        self.issued: set[str] = set()
        self.count = 0
        self.scope = " ".join(google.SCOPES)
        self.refresh_token: str | None = REFRESH
        self.revoke_status = 200
        self.revoke_body = None
        self.owner: dict[str, str] = {}  # access token -> refresh token (or "code")
        self.unreachable: set[str] = set()
        self.grant_wide = True
        self.user = "user-1"
        self.user_of: dict[str, str] = {}  # refresh token -> fake user
        self.generation: dict[str, int] = {}  # user -> revocation generation
        self.token_generation: dict[str, int] = {}  # refresh token -> generation at issue
        self.dead_tokens: set[str] = set()

    def refresh_valid(self, token):
        if token == "code" or token in self.dead_tokens:
            return token == "code"
        user = self.user_of.get(token, self.user)
        return self.token_generation.get(token, 0) >= self.generation.get(user, 0)

    def _revoke(self, token):
        refresh = self.owner.get(token, token)
        if not self.grant_wide:
            self.dead_tokens.add(refresh)
            return
        user = self.user_of.get(refresh, self.user)
        self.generation[user] = self.generation.get(user, 0) + 1

    def transport(self):
        return httpx.MockTransport(self)

    def client(self):
        return httpx.Client(transport=self.transport())

    def by(self, method, suffix):
        return [r for r in self.requests if r.method == method and r.url.path.endswith(suffix)]

    def tokens_requested(self):
        return [r for r in self.requests if r.url.host == "oauth2.googleapis.com"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if request.url.path == "/revoke":
                if self.revoke_status == 200:
                    self._revoke(form["token"])
                if self.revoke_body is not None:
                    return httpx.Response(self.revoke_status, json=self.revoke_body)
                return httpx.Response(self.revoke_status, text=SECRET_MARKER)
            return self._token(form)
        return self._api(request)

    def _token(self, form):
        if self.token_queue:
            item = self.token_queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if form["grant_type"] == "refresh_token" and not self.refresh_valid(form["refresh_token"]):
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.count += 1
        access = f"synthetic-access-{self.count}"
        self.issued.add(access)
        body = {"access_token": access, "expires_in": 3600, "token_type": "Bearer"}
        if form["grant_type"] == "authorization_code":
            body["scope"] = self.scope
            self.owner[access] = self.refresh_token or "code"
            if self.refresh_token:
                body["refresh_token"] = self.refresh_token
                self.user_of[self.refresh_token] = self.user
                self.token_generation[self.refresh_token] = self.generation.get(self.user, 0)
        else:
            self.owner[access] = form["refresh_token"]
        return httpx.Response(200, json=body)

    def _api(self, request):
        if self.api_queue:
            item = self.api_queue.pop(0)
            if isinstance(item, Exception):
                raise item
            if item is not None:
                return item
        access = request.headers.get("authorization", "")[7:]
        if access not in self.issued or not self.refresh_valid(self.owner.get(access, "code")):
            return httpx.Response(401, json={"error": {"code": 401}})
        parts = request.url.path.split("/")  # ['', 'calendar', 'v3', ...]
        if parts[3] == "freeBusy":
            return self._free_busy(json.loads(request.content))
        calendar_id = unquote(parts[4])
        if calendar_id in self.unreachable:
            return httpx.Response(404)
        if request.method == "GET" and len(parts) == 6:  # events.list reachability check
            return httpx.Response(200, json={"items": []})
        if request.method == "POST":
            body = json.loads(request.content)
            key = (calendar_id, body["id"])
            if key in self.events:
                return httpx.Response(409, json={"error": {"errors": [{"reason": "duplicate"}]}})
            self.events[key] = {**body, "status": "confirmed"}
            return httpx.Response(200, json=self.events[key])
        key = (calendar_id, parts[6])
        event = self.events.get(key)
        if request.method == "GET":
            return httpx.Response(200, json=event) if event else httpx.Response(404)
        if event is None:
            return httpx.Response(404)
        if event["status"] == "cancelled":
            return httpx.Response(410)
        self.events[key] = {"id": event["id"], "status": "cancelled"}
        return httpx.Response(204)

    def _free_busy(self, body):
        calendars = {}
        for item in body["items"]:
            cid = item["id"]
            if cid in self.fb_errors:
                calendars[cid] = {"errors": self.fb_errors[cid], "busy": []}
            else:
                calendars[cid] = {"busy": self.busy.get(cid, [])}
        return httpx.Response(200, json={"kind": "calendar#freeBusy", "calendars": calendars})


# Fixtures and helpers


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setattr(staff, "SCRYPT_N", 2**10)
    monkeypatch.setattr(service, "utc_now", lambda: NOW)


@pytest.fixture
def fake():
    return FakeGoogle()


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEYS", KEYS)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", REDIRECT)


def make_client(database, monkeypatch, fake):
    monkeypatch.setenv("DATABASE_URL", database)
    get_settings.cache_clear()

    def http():
        with fake.client() as client:
            yield client

    app.dependency_overrides[google_http] = http
    return TestClient(app)


@pytest.fixture
def client(database, monkeypatch, fake, configured):
    with make_client(database, monkeypatch, fake) as value:
        yield value
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def unconfigured_client(database, monkeypatch, fake):
    for name in ("TOKEN_ENCRYPTION_KEYS", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "")
    with make_client(database, monkeypatch, fake) as value:
        yield value
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def new_user(database, role="owner", clinic=CLINIC):
    email = f"{role}-{uuid4().hex[:8]}@example.test"
    with connect(database) as conn:
        user = staff.create_user(conn, clinic, email, role, PASSWORD)
    return user["id"], email


def headers(client, database, role="owner", clinic=CLINIC):
    _, email = new_user(database, role, clinic)
    r = client.post("/v1/staff/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def other_clinic(database):
    clinic = uuid4()
    with connect(database) as conn:
        conn.execute("INSERT INTO clinics (id,name) VALUES (%s,'Synthetic Other')", (clinic,))
    return clinic


def start(client, auth):
    r = client.post("/v1/admin/calendar/google/start", headers=auth)
    assert r.status_code == 200, r.text
    url = r.json()["authorization_url"]
    return url, {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def callback(client, **params):
    return client.get("/v1/oauth/google/callback", params=params)


def accounts(database, clinic=None):
    with connect(database) as conn:
        if clinic is None:
            return conn.execute("SELECT * FROM calendar_accounts").fetchall()
        return conn.execute(
            "SELECT * FROM calendar_accounts WHERE clinic_id=%s", (clinic,)
        ).fetchall()


def settings_for(database):
    return Settings(
        database_url=database,
        token_encryption_keys=KEYS,
        google_client_id=CLIENT_ID,
        google_client_secret=CLIENT_SECRET,
        google_redirect_uri=REDIRECT,
    )


def make_account(database, clinic=CLINIC, token=REFRESH, status="active"):
    user_id, _ = new_user(database, "owner", clinic)
    account_id = uuid4()
    key_id, blob = Keyring.from_setting(KEYS).encrypt(
        token.encode(), calendar_accounts.account_aad(clinic, account_id)
    )
    with connect(database) as conn:
        conn.execute(
            "INSERT INTO calendar_accounts (id,clinic_id,provider,refresh_token_ciphertext,"
            "key_id,scopes,status,connected_by,revoked_at) VALUES"
            " (%s,%s,'google',%s,%s,%s,%s,%s,%s)",
            (
                account_id,
                clinic,
                None if status == "revoked" else blob,
                None if status == "revoked" else key_id,
                list(google.SCOPES),
                status,
                user_id,
                datetime.now(UTC) if status == "revoked" else None,
            ),
        )
    return account_id


def app_logs(caplog):
    """Captured log text except the test client's own request lines (httpx2, used by
    Starlette's TestClient, logs the URL it sends, which in a callback test holds the code)."""
    return "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("httpx2"))


def audit_rows(database):
    with connect(database) as conn:
        return conn.execute("SELECT actor_id, action, target_id FROM audit_events").fetchall()


# Settings


def test_redirect_uri_must_be_https_unless_loopback(database):
    base = {"database_url": database}
    assert Settings(**base, google_redirect_uri="http://localhost:8000/cb").google_redirect_uri
    assert Settings(**base, google_redirect_uri="http://127.0.0.1:8000/cb").google_redirect_uri
    with pytest.raises(ValueError):
        Settings(**base, google_redirect_uri="http://clinic.example.test/cb")
    with pytest.raises(ValueError):
        Settings(**base, google_redirect_uri="ftp://clinic.example.test/cb")
    assert not Settings(**base).google_configured()
    assert settings_for(database).google_configured()


# Start


def test_start_builds_pkce_authorization_url(client, database):
    url, query = start(client, headers(client, database, "admin"))
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == REDIRECT
    assert query["response_type"] == "code"
    assert query["scope"].split() == list(google.SCOPES)
    assert query["access_type"] == "offline"
    assert query["prompt"] == "consent"
    assert query["include_granted_scopes"] == "false"
    assert query["code_challenge_method"] == "S256"
    assert len(query["state"]) >= 43
    digest = calendar_accounts.digest(query["state"])
    with connect(database) as conn:
        row = conn.execute(
            "SELECT *, expires_at - created_at AS ttl FROM oauth_states WHERE state_digest=%s",
            (digest,),
        ).fetchone()
    assert row["ttl"] == timedelta(minutes=10)
    assert query["state"].encode() not in bytes(row["code_verifier_ciphertext"])
    verifier = (
        Keyring.from_setting(KEYS)
        .decrypt(
            row["key_id"],
            row["code_verifier_ciphertext"],
            calendar_accounts.state_aad(digest, row["clinic_id"], row["user_id"]),
        )
        .decode()
    )
    assert 43 <= len(verifier) <= 128 and verifier.isalnum()
    assert query["code_challenge"] == calendar_accounts.challenge(verifier)
    expected = base64.urlsafe_b64encode(
        __import__("hashlib").sha256(verifier.encode()).digest()
    ).rstrip(b"=")
    assert query["code_challenge"] == expected.decode()


@pytest.mark.parametrize("role,status", [("owner", 200), ("admin", 200), ("viewer", 403)])
def test_start_role_matrix(client, database, role, status):
    r = client.post("/v1/admin/calendar/google/start", headers=headers(client, database, role))
    assert r.status_code == status


def test_start_requires_session(client):
    assert client.post("/v1/admin/calendar/google/start").status_code == 401


def test_not_configured_fails_closed(unconfigured_client, database):
    auth = headers(unconfigured_client, database)
    r = unconfigured_client.post("/v1/admin/calendar/google/start", headers=auth)
    assert r.status_code == 503
    assert r.json()["detail"] == "Calendar integration not configured"
    r = callback(unconfigured_client, state="x" * 43, code=CODE)
    assert r.status_code == 503


def test_bad_keyring_fails_closed(client, database, monkeypatch):
    auth = headers(client, database)
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEYS", "k1:" + base64.b64encode(b"short").decode())
    get_settings.cache_clear()
    r = client.post("/v1/admin/calendar/google/start", headers=auth)
    assert r.status_code == 503


# Callback


def test_callback_stores_encrypted_token(client, database, fake, caplog):
    caplog.set_level(logging.DEBUG)
    user_auth = headers(client, database, "admin")
    _, query = start(client, user_auth)
    r = callback(client, state=query["state"], code=CODE)
    assert r.status_code == 200, r.text
    assert "Calendar connected" in r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    [account] = accounts(database)
    assert account["clinic_id"] == CLINIC and account["status"] == "active"
    assert account["provider"] == "google" and sorted(account["scopes"]) == sorted(google.SCOPES)
    blob = bytes(account["refresh_token_ciphertext"])
    assert REFRESH.encode() not in blob
    plain = Keyring.from_setting(KEYS).decrypt(
        account["key_id"], blob, calendar_accounts.account_aad(CLINIC, account["id"])
    )
    assert plain.decode() == REFRESH
    [token_request] = fake.tokens_requested()
    form = {k: v[0] for k, v in parse_qs(token_request.content.decode()).items()}
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == CODE and form["redirect_uri"] == REDIRECT
    assert form["client_id"] == CLIENT_ID and form["client_secret"] == CLIENT_SECRET
    assert query["code_challenge"] == calendar_accounts.challenge(form["code_verifier"])
    me = client.get("/v1/staff/me", headers=user_auth).json()["id"]
    assert {
        "actor_id": account["connected_by"],
        "action": "calendar.connected",
        "target_id": account["id"],
    } in audit_rows(database)
    assert str(account["connected_by"]) == me
    for secret in (CODE, REFRESH, CLIENT_SECRET, "synthetic-access", query["state"]):
        assert secret not in app_logs(caplog)
        assert secret not in r.text
    # Accounts listing carries no secrets.
    listing = client.get("/v1/admin/calendar/accounts", headers=headers(client, database, "viewer"))
    assert listing.status_code == 200
    [item] = listing.json()
    assert set(item) == {
        "id",
        "provider",
        "status",
        "scopes",
        "connected_by",
        "created_at",
        "revoked_at",
    }


def test_callback_state_is_single_use(client, database):
    _, query = start(client, headers(client, database))
    assert callback(client, state=query["state"], code=CODE).status_code == 200
    r = callback(client, state=query["state"], code=CODE)
    assert r.status_code == 400
    assert len(accounts(database)) == 1


def test_callback_rejects_expired_and_unknown_state(client, database, fake):
    _, query = start(client, headers(client, database))
    with connect(database) as conn:
        conn.execute("UPDATE oauth_states SET expires_at = now() - interval '1 second'")
    assert callback(client, state=query["state"], code=CODE).status_code == 400
    assert callback(client, state="unknown-" + "x" * 40, code=CODE).status_code == 400
    assert callback(client, code=CODE).status_code == 400
    assert accounts(database) == []
    assert fake.tokens_requested() == []


def test_callback_cross_tenant(client, database):
    other = other_clinic(database)
    other_auth = headers(client, database, "owner", other)
    _, query = start(client, other_auth)
    assert callback(client, state=query["state"], code=CODE).status_code == 200
    assert accounts(database, CLINIC) == []
    [account] = accounts(database, other)
    own_auth = headers(client, database)
    assert client.get("/v1/admin/calendar/accounts", headers=own_auth).json() == []
    r = client.delete(f"/v1/admin/calendar/google/{account['id']}", headers=own_auth)
    assert r.status_code == 404
    assert accounts(database, other)[0]["status"] == "active"


def test_callback_requires_refresh_token(client, database, fake):
    fake.refresh_token = None
    _, query = start(client, headers(client, database))
    r = callback(client, state=query["state"], code=CODE)
    assert r.status_code == 400
    assert accounts(database) == []
    # Discarded, not revoked: Google revocation is grant-wide and would also kill an
    # account this Google user already connected.
    assert fake.by("POST", "/revoke") == []
    assert "synthetic-access" not in r.text


def test_callback_requires_scopes(client, database, fake):
    fake.scope = google.SCOPES[0]
    _, query = start(client, headers(client, database))
    r = callback(client, state=query["state"], code=CODE)
    assert r.status_code == 400
    assert accounts(database) == []
    assert fake.by("POST", "/revoke") == []


def test_callback_provider_error_param(client, database, fake):
    _, query = start(client, headers(client, database))
    r = callback(client, state=query["state"], error="access_denied")
    assert r.status_code == 400
    assert fake.tokens_requested() == []
    assert callback(client, state=query["state"], code=CODE).status_code == 400  # consumed


def test_callback_token_endpoint_failures(client, database, fake, caplog):
    caplog.set_level(logging.DEBUG)
    fake.token_queue.append(
        httpx.Response(400, json={"error": "invalid_grant", "x": SECRET_MARKER})
    )
    _, query = start(client, headers(client, database))
    assert callback(client, state=query["state"], code=CODE).status_code == 400
    fake.token_queue.append(httpx.Response(503, text=SECRET_MARKER))
    _, query = start(client, headers(client, database))
    assert callback(client, state=query["state"], code=CODE).status_code == 502
    assert accounts(database) == []
    assert SECRET_MARKER not in app_logs(caplog) and CODE not in app_logs(caplog)


def test_callback_rejects_disabled_user(client, database):
    user_id, email = new_user(database, "admin")
    r = client.post("/v1/staff/login", json={"email": email, "password": PASSWORD})
    _, query = start(client, {"Authorization": "Bearer " + r.json()["token"]})
    with connect(database) as conn:
        conn.execute("UPDATE staff_users SET disabled_at=now() WHERE id=%s", (user_id,))
    assert callback(client, state=query["state"], code=CODE).status_code == 400
    assert accounts(database) == []


def test_access_log_query_is_stripped():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", f"/v1/oauth/google/callback?state=s&code={CODE}", "1.1", 200),
        None,
    )
    StripQuery().filter(record)
    assert CODE not in record.getMessage()
    assert "/v1/oauth/google/callback" in record.getMessage()


# Revocation and doctor mapping


def revoke_jobs(database):
    with connect(database) as conn:
        return conn.execute(
            "SELECT status, last_error, payload FROM outbox WHERE kind='calendar.revoke_grant'"
        ).fetchall()


def test_revoke_account(client, database, fake):
    account_id = make_account(database)
    assert (
        client.delete(
            f"/v1/admin/calendar/google/{account_id}", headers=headers(client, database, "viewer")
        ).status_code
        == 403
    )
    auth = headers(client, database, "admin")
    r = client.delete(f"/v1/admin/calendar/google/{account_id}", headers=auth)
    assert r.status_code == 204
    [account] = accounts(database)
    assert account["status"] == "revoked" and account["revoked_at"] is not None
    # The ciphertext stays until Google confirms; the route makes no provider call.
    assert account["refresh_token_ciphertext"] is not None
    assert fake.requests == []
    [job] = revoke_jobs(database)
    assert job["status"] == "pending" and job["payload"] == {"account_id": str(account_id)}
    assert [r["action"] for r in audit_rows(database)].count("calendar.revoked") == 1
    # Idempotent; no second job or audit row.
    assert client.delete(f"/v1/admin/calendar/google/{account_id}", headers=auth).status_code == 204
    assert len(revoke_jobs(database)) == 1
    assert client.delete(f"/v1/admin/calendar/google/{uuid4()}", headers=auth).status_code == 404
    run_once(database, "worker-g", google_ports(database, fake))
    [revoke] = fake.by("POST", "/revoke")
    assert parse_qs(revoke.content.decode())["token"] == [REFRESH]
    [account] = accounts(database)
    assert account["refresh_token_ciphertext"] is None and account["key_id"] is None
    assert revoke_jobs(database)[0]["status"] == "done"
    with pytest.raises(ProviderPermanent, match="^auth_revoked$"):
        adapter(fake).get_event(CAL, EVENT.event_id)  # The grant is gone at (fake) Google.


def test_revoke_grant_retries_until_google_confirms(client, database, fake, caplog):
    caplog.set_level(logging.DEBUG)
    fake.revoke_status = 500
    account_id = make_account(database)
    r = client.delete(f"/v1/admin/calendar/google/{account_id}", headers=headers(client, database))
    assert r.status_code == 204
    run_once(database, "worker-g", google_ports(database, fake))
    [job] = revoke_jobs(database)
    assert job["status"] == "pending" and job["last_error"] == "retryable"
    assert accounts(database)[0]["refresh_token_ciphertext"] is not None
    assert SECRET_MARKER not in app_logs(caplog) and REFRESH not in app_logs(caplog)
    # Exhausted attempts dead-letter with the ciphertext kept (staff requeue later).
    with connect(database) as conn:
        conn.execute(
            "UPDATE outbox SET available_at=now() - interval '1 second', max_attempts=2"
            " WHERE kind='calendar.revoke_grant'"
        )
    run_once(database, "worker-g", google_ports(database, fake))
    assert revoke_jobs(database)[0]["status"] == "dead"
    assert accounts(database)[0]["refresh_token_ciphertext"] is not None
    # The owner removed the grant in Google; Google now reports invalid_token: success.
    fake.revoke_status = 400
    fake.revoke_body = {"error": "invalid_token"}
    with connect(database) as conn:
        conn.execute(
            "UPDATE outbox SET status='pending', max_attempts=attempts + 1,"
            " available_at=now() WHERE kind='calendar.revoke_grant'"
        )
    run_once(database, "worker-g", google_ports(database, fake))
    assert revoke_jobs(database)[0]["status"] == "done"
    assert accounts(database)[0]["refresh_token_ciphertext"] is None


def test_revoke_grant_needs_google_worker(client, database):
    account_id = make_account(database)
    client.delete(f"/v1/admin/calendar/google/{account_id}", headers=headers(client, database))
    run_once(database, "worker-f", Ports(calendar=FakeCalendar()))
    [job] = revoke_jobs(database)
    assert job["status"] == "dead" and job["last_error"] == "no_revoker"
    assert accounts(database)[0]["refresh_token_ciphertext"] is not None


def test_doctor_calendar_account_mapping(client, database):
    auth = headers(client, database, "admin")
    account_id = make_account(database)
    path = f"/v1/admin/doctors/{DOCTOR_ID}"
    r = client.patch(path, headers=auth, json={"calendar_account_id": str(account_id)})
    assert r.status_code == 200 and r.json()["calendar_account_id"] == str(account_id)
    foreign = make_account(database, other_clinic(database))
    assert (
        client.patch(path, headers=auth, json={"calendar_account_id": str(foreign)}).status_code
        == 404
    )
    assert (
        client.patch(path, headers=auth, json={"calendar_account_id": str(uuid4())}).status_code
        == 404
    )
    revoked = make_account(database, status="revoked")
    assert (
        client.patch(path, headers=auth, json={"calendar_account_id": str(revoked)}).status_code
        == 409
    )
    r = client.patch(path, headers=auth, json={"calendar_account_id": None})
    assert r.status_code == 200 and r.json()["calendar_account_id"] is None


# Adapter


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def adapter(fake, clock=None, timeout=10):
    return google.GoogleCalendar(
        REFRESH, CLIENT_ID, CLIENT_SECRET, fake.client(), timeout, clock or Clock()
    )


START = datetime(2031, 3, 5, 10, 0, tzinfo=IST)
EVENT = CalendarEvent("cv" + "a" * 32 + "v1", START, START + timedelta(minutes=15), "Clinic ref")


def test_token_refresh_and_caching(fake):
    clock = Clock()
    port = adapter(fake, clock)
    assert port.get_event(CAL, EVENT.event_id) is None
    assert port.get_event(CAL, EVENT.event_id) is None
    [token] = fake.tokens_requested()
    form = {k: v[0] for k, v in parse_qs(token.content.decode()).items()}
    assert form == {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": REFRESH,
    }
    clock.now += 3600 - 61
    port.get_event(CAL, EVENT.event_id)
    assert len(fake.tokens_requested()) == 1
    clock.now += 2  # Inside the 60 s margin: refresh.
    port.get_event(CAL, EVENT.event_id)
    assert len(fake.tokens_requested()) == 2
    assert "synthetic" not in repr(port)


def test_401_refreshes_once_and_retries(fake):
    port = adapter(fake)
    port.get_event(CAL, EVENT.event_id)
    fake.issued.clear()  # Google invalidated the cached token.
    assert port.get_event(CAL, EVENT.event_id) is None
    assert len(fake.tokens_requested()) == 2
    fake.api_queue = [httpx.Response(401), httpx.Response(401)]
    with pytest.raises(ProviderPermanent) as error:
        port.get_event(CAL, EVENT.event_id)
    assert error.value.code == "unauthorized"
    assert len(fake.tokens_requested()) == 3  # One refresh per 401, never a loop.


def test_refresh_failures(fake):
    fake.token_queue.append(httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(ProviderPermanent, match="^auth_revoked$"):
        adapter(fake).get_event(CAL, EVENT.event_id)
    fake.token_queue.append(httpx.Response(503))
    with pytest.raises(ProviderRetryable):
        adapter(fake).get_event(CAL, EVENT.event_id)


def test_insert_body_shape(fake):
    stored = adapter(fake).insert_event(CAL, EVENT)
    assert stored.status == "confirmed" and stored.start == EVENT.start
    [request] = fake.by("POST", "/events")
    path = request.url.raw_path.split(b"?")[0]
    assert path == b"/calendar/v3/calendars/booking%40example.test/events"
    assert request.url.params["sendUpdates"] == "none"
    assert request.headers["authorization"] == "Bearer synthetic-access-1"
    body = json.loads(request.content)
    assert body == {
        "id": EVENT.event_id,
        "start": {"dateTime": "2031-03-05T10:00:00+05:30"},
        "end": {"dateTime": "2031-03-05T10:15:00+05:30"},
        "summary": "Clinic ref",
        "description": "",
        "transparency": "opaque",
        "extendedProperties": {"private": {"appointment_ref": EVENT.event_id}},
    }


def test_insert_conflict_and_get_mapping(fake):
    port = adapter(fake)
    port.insert_event(CAL, EVENT)
    with pytest.raises(EventAlreadyExists):
        port.insert_event(CAL, EVENT)
    got = port.get_event(CAL, EVENT.event_id)
    assert got.status == "confirmed" and got.start == EVENT.start and got.end == EVENT.end
    port.cancel_event(CAL, EVENT.event_id)
    assert port.get_event(CAL, EVENT.event_id).status == "cancelled"  # 200 status cancelled
    fake.api_queue.append(httpx.Response(410))
    assert port.get_event(CAL, EVENT.event_id).status == "cancelled"
    assert port.get_event(CAL, "cv" + "b" * 32 + "v1") is None  # 404


def test_delete_is_idempotent(fake):
    port = adapter(fake)
    port.cancel_event(CAL, EVENT.event_id)  # 404
    port.insert_event(CAL, EVENT)
    port.cancel_event(CAL, EVENT.event_id)  # 204
    port.cancel_event(CAL, EVENT.event_id)  # 410
    assert [r.method for r in fake.by("DELETE", EVENT.event_id)] == ["DELETE"] * 3
    fake.api_queue.append(httpx.Response(500))
    with pytest.raises(ProviderRetryable):
        port.cancel_event(CAL, EVENT.event_id)


def test_free_busy_merges_and_maps_errors(fake):
    port = adapter(fake)
    window = (START, START + timedelta(hours=4))
    fake.busy[CAL] = [
        {"start": "2031-03-05T04:00:00Z", "end": "2031-03-05T05:00:00Z"},  # 09:30-10:30 IST
    ]
    fake.busy[BUSY] = [
        {"start": "2031-03-05T10:15:00+05:30", "end": "2031-03-05T11:00:00+05:30"},
        {"start": "2031-03-05T13:30:00+05:30", "end": "2031-03-05T15:00:00+05:30"},
    ]
    busy = port.free_busy([CAL, BUSY], *window)
    assert busy == [
        (START, datetime(2031, 3, 5, 11, 0, tzinfo=IST)),
        (datetime(2031, 3, 5, 13, 30, tzinfo=IST), window[1]),
    ]
    body = json.loads(fake.by("POST", "/freeBusy")[0].content)
    assert body["items"] == [{"id": CAL}, {"id": BUSY}]
    assert body["timeMin"] == "2031-03-05T10:00:00+05:30"
    fake.fb_errors[BUSY] = [{"domain": "global", "reason": "notFound"}]
    with pytest.raises(ProviderPermanent, match="^calendar_not_found$"):
        port.free_busy([CAL, BUSY], *window)
    fake.fb_errors[BUSY] = [{"domain": "global", "reason": "internalError"}]
    with pytest.raises(ProviderRetryable):
        port.free_busy([CAL, BUSY], *window)
    fake.api_queue.append(httpx.Response(200, json={"calendars": {CAL: {"busy": []}}}))
    with pytest.raises(ProviderRetryable, match="freebusy_missing"):
        port.free_busy([CAL, BUSY], *window)


def test_rate_limits_and_server_errors(fake):
    port = adapter(fake)
    fake.api_queue.append(httpx.Response(429, headers={"Retry-After": "7"}))
    with pytest.raises(ProviderRetryable) as error:
        port.get_event(CAL, EVENT.event_id)
    assert error.value.retry_after == 7
    later = (datetime.now(UTC) + timedelta(seconds=120)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    fake.api_queue.append(httpx.Response(503, headers={"Retry-After": later}))
    with pytest.raises(ProviderRetryable) as error:
        port.insert_event(CAL, EVENT)
    assert 100 < error.value.retry_after <= 120
    fake.api_queue.append(
        httpx.Response(403, json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}})
    )
    with pytest.raises(ProviderRetryable):
        port.insert_event(CAL, EVENT)
    fake.api_queue.append(
        httpx.Response(403, json={"error": {"errors": [{"reason": "forbidden"}]}})
    )
    with pytest.raises(ProviderPermanent):
        port.insert_event(CAL, EVENT)
    fake.api_queue.append(httpx.Response(400))
    with pytest.raises(ProviderPermanent):
        port.insert_event(CAL, EVENT)
    fake.api_queue.append(httpx.Response(500))
    with pytest.raises(ProviderRetryable) as error:
        port.free_busy([CAL], START, START + timedelta(hours=1))
    assert error.value.retry_after is None


def test_timeouts(fake):
    port = adapter(fake)
    port.get_event(CAL, EVENT.event_id)  # Warm the token so the queue hits the API call.
    request = httpx.Request("GET", "https://www.googleapis.com/")
    fake.api_queue.append(httpx.ReadTimeout("synthetic", request=request))
    with pytest.raises(ProviderTimeout):
        port.insert_event(CAL, EVENT)
    fake.api_queue.append(httpx.ReadTimeout("synthetic", request=request))
    with pytest.raises(ProviderTimeout):
        port.cancel_event(CAL, EVENT.event_id)
    fake.api_queue.append(httpx.ReadTimeout("synthetic", request=request))
    with pytest.raises(ProviderRetryable, match="^timeout$"):
        port.get_event(CAL, EVENT.event_id)
    fake.api_queue.append(httpx.ConnectError("synthetic", request=request))
    with pytest.raises(ProviderRetryable, match="^network$"):
        port.insert_event(CAL, EVENT)
    assert fake.events == {}


def test_errors_carry_codes_only(fake):
    port = adapter(fake)
    responses = [
        httpx.Response(400, text=SECRET_MARKER),
        httpx.Response(500, text=SECRET_MARKER),
        httpx.Response(200, text=SECRET_MARKER),
    ]
    for response in responses:
        fake.api_queue.append(response)
        with pytest.raises(Exception) as error:
            port.get_event(CAL, EVENT.event_id)
        assert SECRET_MARKER not in str(error.value) and CAL not in str(error.value)
        assert error.value.__cause__ is None
        assert len(str(error.value)) <= 32


def test_check_ports_requires_numeric_timeout(fake):
    check_ports(Ports(calendar=adapter(fake)))
    with pytest.raises(ValueError):
        check_ports(Ports(calendar=adapter(fake, timeout=True)))
    with pytest.raises(ValueError):
        check_ports(Ports(calendar=adapter(fake, timeout=60)))
    with pytest.raises(ValueError):
        Ports()
    with pytest.raises(ValueError):
        Ports(calendar=FakeCalendar(), calendar_for=lambda *a: None)


# Worker


def at(hour, minute=0):
    return datetime.combine(DAY, time(hour, minute), IST)


def book(database, hour=10):
    request = ReservationRequest(doctor_id=DOCTOR_ID, service_id=SERVICE_ID, starts_at=at(hour))
    return service.reserve(database, CLINIC, request, f"key-{uuid4().hex}")["appointment_id"]


def map_doctor(database, account_id=None):
    with connect(database) as conn:
        conn.execute(
            "UPDATE doctors SET booking_calendar_id=%s, busy_calendar_ids=%s,"
            " calendar_account_id=%s WHERE id=%s",
            (CAL, [BUSY], account_id, DOCTOR_ID),
        )


def appointment(database, appointment_id):
    with connect(database) as conn:
        return conn.execute(
            "SELECT a.status, a.calendar_event_id, o.status AS job, o.last_error"
            " FROM appointments a JOIN outbox o ON o.appointment_id=a.id"
            " AND o.kind='calendar.create' WHERE a.id=%s",
            (appointment_id,),
        ).fetchone()


def google_ports(database, fake):
    return Ports(
        calendar_for=calendar_accounts.GoogleCalendarResolver(settings_for(database), fake.client())
    )


@pytest.mark.parametrize("explicit", [True, False])
def test_worker_confirms_through_google(database, fake, explicit):
    account_id = make_account(database)
    map_doctor(database, account_id if explicit else None)
    appointment_id = book(database)
    assert run_once(database, "worker-g", google_ports(database, fake)) == 1
    row = appointment(database, appointment_id)
    assert row["status"] == "confirmed" and row["job"] == "done"
    event_id = event_id_for(appointment_id, 1)
    assert row["calendar_event_id"] == event_id
    [insert] = fake.by("POST", "/events")
    assert json.loads(insert.content)["id"] == event_id
    assert len(fake.by("POST", "/freeBusy")) == 1  # Busy re-check before writing.
    assert (CAL, event_id) in fake.events


def test_worker_treats_revoked_account_as_unmapped(database, fake):
    account_id = make_account(database, status="revoked")
    map_doctor(database, account_id)
    make_account(database)  # An active account must not be used as a fallback.
    appointment_id = book(database)
    run_once(database, "worker-g", google_ports(database, fake))
    row = appointment(database, appointment_id)
    assert row["status"] == "needs_review"
    assert row["job"] == "dead" and row["last_error"] == "calendar_unmapped"
    assert fake.requests == []


def test_worker_without_unique_account_is_unmapped(database, fake):
    make_account(database)
    make_account(database)  # Two accounts and no explicit mapping: ambiguous.
    map_doctor(database)
    appointment_id = book(database)
    run_once(database, "worker-g", google_ports(database, fake))
    assert appointment(database, appointment_id)["last_error"] == "calendar_unmapped"


def test_worker_revoked_via_route(client, database, fake):
    account_id = make_account(database)
    map_doctor(database, account_id)
    auth = headers(client, database)
    assert client.delete(f"/v1/admin/calendar/google/{account_id}", headers=auth).status_code == 204
    appointment_id = book(database)
    fake.requests.clear()
    run_once(database, "worker-g", google_ports(database, fake))
    assert appointment(database, appointment_id)["last_error"] == "calendar_unmapped"
    assert fake.by("POST", "/events") == [] and fake.by("POST", "/freeBusy") == []


def test_worker_undecryptable_token_is_permanent(database, fake):
    account_id = make_account(database)
    map_doctor(database, account_id)
    appointment_id = book(database)
    other = "k2:" + base64.b64encode(os.urandom(32)).decode()
    settings = Settings(
        database_url=database,
        token_encryption_keys=other,
        google_client_id=CLIENT_ID,
        google_client_secret=CLIENT_SECRET,
        google_redirect_uri=REDIRECT,
    )
    ports = Ports(calendar_for=calendar_accounts.GoogleCalendarResolver(settings, fake.client()))
    run_once(database, "worker-g", ports)
    row = appointment(database, appointment_id)
    assert row["job"] == "dead" and row["last_error"] == "provider_permanent"


def test_worker_cli_google_requires_configuration(database, monkeypatch):
    from clinic_voice import cli

    with pytest.raises(SystemExit, match="Google Calendar is not configured"):
        cli.build_ports("google", database, Settings(database_url=database))
    ports = cli.build_ports("google", database, settings_for(database))
    check_ports(ports)


# The account that wrote an event owns its cancel


def add_cancel_job(database, appointment_id, version=1):
    with connect(database) as conn:
        conn.execute(
            "INSERT INTO outbox (id,clinic_id,appointment_id,kind,appointment_version)"
            " VALUES (%s,%s,%s,'calendar.cancel',%s)",
            (uuid4(), CLINIC, appointment_id, version),
        )


def job_state(database, appointment_id, kind):
    with connect(database) as conn:
        return conn.execute(
            "SELECT status, last_error, payload FROM outbox WHERE appointment_id=%s AND kind=%s",
            (appointment_id, kind),
        ).fetchone()


def confirmed_under(database, fake, account_id):
    map_doctor(database, account_id)
    appointment_id = book(database)
    ports = google_ports(database, fake)
    run_once(database, "worker-g", ports)
    with connect(database) as conn:
        row = conn.execute(
            "SELECT status, calendar_account_id FROM appointments WHERE id=%s", (appointment_id,)
        ).fetchone()
    assert row == {"status": "confirmed", "calendar_account_id": account_id}
    return appointment_id, ports


def owner_of(fake, request):
    return fake.owner[request.headers["authorization"].removeprefix("Bearer ")]


def test_cancel_uses_account_that_wrote_event(database, fake):
    first = make_account(database, token="synthetic-refresh-a")
    second = make_account(database, token="synthetic-refresh-b")
    appointment_id, ports = confirmed_under(database, fake, first)
    [insert] = fake.by("POST", "/events")
    assert owner_of(fake, insert) == "synthetic-refresh-a"
    create = job_state(database, appointment_id, "calendar.create")
    assert create["payload"]["calendar_account_id"] == str(first)
    map_doctor(database, second)  # Remapped after the event was written.
    add_cancel_job(database, appointment_id)
    run_once(database, "worker-g", ports)
    event_id = event_id_for(appointment_id, 1)
    [delete] = fake.by("DELETE", event_id)
    assert owner_of(fake, delete) == "synthetic-refresh-a"
    assert job_state(database, appointment_id, "calendar.cancel")["status"] == "done"
    assert fake.events[(CAL, event_id)]["status"] == "cancelled"


def test_cancel_with_revoked_writer_falls_back_to_current_mapping(database, fake):
    first = make_account(database)
    second = make_account(database, token="synthetic-refresh-b")
    appointment_id, ports = confirmed_under(database, fake, first)
    with connect(database) as conn:
        conn.execute(
            "UPDATE calendar_accounts SET status='revoked', revoked_at=now(),"
            " refresh_token_ciphertext=NULL, key_id=NULL WHERE id=%s",
            (first,),
        )
    map_doctor(database, second)
    add_cancel_job(database, appointment_id)
    run_once(database, "worker-g", ports)
    assert job_state(database, appointment_id, "calendar.cancel")["status"] == "done"
    [delete] = fake.by("DELETE", event_id_for(appointment_id, 1))
    assert owner_of(fake, delete) == "synthetic-refresh-b"


def test_cancel_with_errored_writer_is_dead(database, fake):
    first = make_account(database)
    second = make_account(database, token="synthetic-refresh-b")
    appointment_id, ports = confirmed_under(database, fake, first)
    with connect(database) as conn:
        conn.execute("UPDATE calendar_accounts SET status='error' WHERE id=%s", (first,))
    map_doctor(database, second)
    add_cancel_job(database, appointment_id)
    run_once(database, "worker-g", ports)
    job = job_state(database, appointment_id, "calendar.cancel")
    assert job["status"] == "dead" and job["last_error"] == "calendar_unmapped"
    assert fake.by("DELETE", event_id_for(appointment_id, 1)) == []


def create_with_recorded_intent(database, account_id):
    appointment_id = book(database)
    with connect(database) as conn:
        conn.execute(
            "UPDATE outbox SET payload=%s WHERE appointment_id=%s AND kind='calendar.create'",
            (Jsonb({"calendar_id": CAL, "calendar_account_id": str(account_id)}), appointment_id),
        )
    return appointment_id


def test_create_with_revoked_intent_account_falls_back(database, fake):
    first = make_account(database)
    second = make_account(database, token="synthetic-refresh-b")
    appointment_id = create_with_recorded_intent(database, first)
    with connect(database) as conn:
        conn.execute(
            "UPDATE calendar_accounts SET status='revoked', revoked_at=now() WHERE id=%s",
            (first,),
        )
    map_doctor(database, second)
    run_once(database, "worker-g", google_ports(database, fake))
    assert appointment(database, appointment_id)["status"] == "confirmed"
    with connect(database) as conn:
        recorded = conn.execute(
            "SELECT calendar_account_id FROM appointments WHERE id=%s", (appointment_id,)
        ).fetchone()["calendar_account_id"]
    assert recorded == second


def test_create_with_errored_intent_account_is_dead(database, fake):
    first = make_account(database)
    second = make_account(database, token="synthetic-refresh-b")
    appointment_id = create_with_recorded_intent(database, first)
    with connect(database) as conn:
        conn.execute("UPDATE calendar_accounts SET status='error' WHERE id=%s", (first,))
    map_doctor(database, second)
    run_once(database, "worker-g", google_ports(database, fake))
    row = appointment(database, appointment_id)
    assert row["status"] == "needs_review" and row["last_error"] == "calendar_unmapped"
    assert fake.by("POST", "/events") == []


def test_cancel_404_on_unreachable_calendar_is_not_done(database, fake):
    account_id = make_account(database)
    appointment_id, ports = confirmed_under(database, fake, account_id)
    fake.unreachable.add(CAL)
    add_cancel_job(database, appointment_id)
    run_once(database, "worker-g", ports)
    job = job_state(database, appointment_id, "calendar.cancel")
    assert job["status"] == "dead" and job["last_error"] == "provider_permanent"


def test_delete_404_requires_reachable_calendar(fake):
    port = adapter(fake)
    fake.unreachable.add(CAL)
    with pytest.raises(ProviderPermanent, match="^calendar_not_found$"):
        port.cancel_event(CAL, EVENT.event_id)
    fake.unreachable.clear()
    port.cancel_event(CAL, EVENT.event_id)  # 404 on a reachable calendar: really gone.
    checks = [r for r in fake.requests if r.method == "GET" and r.url.path.endswith("/events")]
    assert len(checks) == 2 and checks[-1].url.params["maxResults"] == "1"


@pytest.mark.parametrize("value", ["\xb2", "99999999999", "soon", " 12x", "-5"])
def test_retry_after_rejects_garbage(value):
    response = httpx.Response(429, headers={"Retry-After": value.encode("latin-1")})
    assert response.headers["retry-after"] == value  # Decoded back, e.g. a superscript two.
    assert google.retry_after(response) is None


# Rejected credentials, adapter cache, replacement


def test_rejected_credentials_mark_account_error(client, database, fake):
    account_id = make_account(database)
    map_doctor(database, account_id)
    appointment_id = book(database)
    fake.token_queue.append(httpx.Response(400, json={"error": "invalid_grant"}))
    run_once(database, "worker-g", google_ports(database, fake))
    row = appointment(database, appointment_id)
    assert row["job"] == "dead" and row["last_error"] == "provider_permanent"
    assert row["status"] == "needs_review"
    assert accounts(database)[0]["status"] == "error"
    listing = client.get("/v1/admin/calendar/accounts", headers=headers(client, database, "viewer"))
    assert [a["status"] for a in listing.json()] == ["error"]
    assert "calendar.credentials_rejected" in [r["action"] for r in audit_rows(database)]
    resolver = calendar_accounts.GoogleCalendarResolver(settings_for(database), fake.client())
    with connect(database) as conn:
        assert resolver(conn, CLINIC, DOCTOR_ID) is None


def test_resolver_cache_replaces_and_evicts(database, fake):
    account_id = make_account(database)
    map_doctor(database, account_id)
    resolver = calendar_accounts.GoogleCalendarResolver(settings_for(database), fake.client())
    with connect(database) as conn:
        port, resolved = resolver(conn, CLINIC, DOCTOR_ID)
        assert resolved == account_id
        assert resolver(conn, CLINIC, DOCTOR_ID)[0] is port
        key_id, blob = Keyring.from_setting(KEYS).encrypt(
            b"synthetic-refresh-new", calendar_accounts.account_aad(CLINIC, account_id)
        )
        conn.execute(
            "UPDATE calendar_accounts SET refresh_token_ciphertext=%s, key_id=%s WHERE id=%s",
            (blob, key_id, account_id),
        )
        conn.commit()
        replaced, _ = resolver(conn, CLINIC, DOCTOR_ID)
        assert replaced is not port and resolver.cached_accounts() == {account_id}
        conn.execute("UPDATE calendar_accounts SET status='error' WHERE id=%s", (account_id,))
        conn.commit()
        assert resolver(conn, CLINIC, DOCTOR_ID) is None
        assert resolver.cached_accounts() == set()


def test_start_conflicts_with_active_account_unless_replace(client, database, fake):
    auth = headers(client, database)
    old = make_account(database, token="synthetic-refresh-old")
    assert client.post("/v1/admin/calendar/google/start", headers=auth).status_code == 409
    r = client.post("/v1/admin/calendar/google/start?replace=true", headers=auth)
    assert r.status_code == 200
    state = parse_qs(urlsplit(r.json()["authorization_url"]).query)["state"][0]
    fake.refresh_token = "synthetic-refresh-new"  # The same fake Google user reconsents.
    page = callback(client, state=state, code=CODE)
    assert page.status_code == 200
    rows = {row["id"]: row for row in accounts(database)}
    assert rows[old]["status"] == "revoked" and rows[old]["refresh_token_ciphertext"] is None
    assert [row["status"] for row in rows.values()].count("active") == 1
    # Superseded locally only: no Google revocation (it would be grant-wide).
    assert revoke_jobs(database) == [] and fake.by("POST", "/revoke") == []
    actions = [(r["action"], r["target_id"]) for r in audit_rows(database)]
    assert ("calendar.account_replaced", old) in actions
    assert ("calendar.revoked", old) not in actions


def test_replace_with_same_google_user_keeps_new_account_working(client, database, fake):
    auth = headers(client, database)
    make_account(database, token="synthetic-refresh-old")
    r = client.post("/v1/admin/calendar/google/start?replace=true", headers=auth)
    state = parse_qs(urlsplit(r.json()["authorization_url"]).query)["state"][0]
    fake.refresh_token = "synthetic-refresh-new"
    assert callback(client, state=state, code=CODE).status_code == 200
    map_doctor(database)  # No explicit account: the clinic's only active account.
    appointment_id = book(database)
    run_once(database, "worker-g", google_ports(database, fake))
    row = appointment(database, appointment_id)
    assert row["status"] == "confirmed" and row["job"] == "done"
    [insert] = fake.by("POST", "/events")
    assert owner_of(fake, insert) == "synthetic-refresh-new"


def test_fake_revocation_is_grant_wide(fake):
    """Guards the scenario above: revoking the old token would have killed the new one."""
    fake.refresh_token = "synthetic-refresh-new"
    google.exchange_code(fake.client(), CLIENT_ID, CLIENT_SECRET, CODE, "v" * 43, REDIRECT)
    old = google.GoogleCalendar("synthetic-refresh-old", CLIENT_ID, CLIENT_SECRET, fake.client())
    new = google.GoogleCalendar("synthetic-refresh-new", CLIENT_ID, CLIENT_SECRET, fake.client())
    assert new.get_event(CAL, EVENT.event_id) is None
    google.revoke_grant(fake.client(), "synthetic-refresh-old")
    fresh = google.GoogleCalendar("synthetic-refresh-new", CLIENT_ID, CLIENT_SECRET, fake.client())
    with pytest.raises(ProviderPermanent, match="^auth_revoked$"):
        fresh.get_event(CAL, EVENT.event_id)
    with pytest.raises(ProviderPermanent):
        old.get_event(CAL, EVENT.event_id)


def test_callback_refuses_second_active_account(client, database, fake):
    auth = headers(client, database)
    _, first = start(client, auth)
    _, second = start(client, auth)  # No active account yet, so both starts are allowed.
    assert callback(client, state=first["state"], code=CODE).status_code == 200
    r = callback(client, state=second["state"], code=CODE)
    assert r.status_code == 409
    assert len(accounts(database)) == 1
    assert len(fake.tokens_requested()) == 1  # Refused before the code was exchanged.
    assert fake.by("POST", "/revoke") == []


# Logging and housekeeping


def test_access_log_filter_installed_at_import(tmp_path):
    import subprocess
    import sys

    code = (
        "import logging, clinic_voice.api as a\n"
        "count = lambda: sum(isinstance(f, a.StripQuery)"
        " for f in logging.getLogger('uvicorn.access').filters)\n"
        "assert count() == 1\n"
        "a.quiet_logs()\n"
        "assert count() == 1\n"
    )
    env = {**os.environ, "DATABASE_URL": "postgresql://unused@localhost/unused_test"}
    # From an empty directory, so no repository .env is reachable (the package is installed).
    subprocess.run([sys.executable, "-c", code], check=True, env=env, cwd=tmp_path)


def test_prune_oauth_states(database):
    other = other_clinic(database)
    for clinic, offset in ((CLINIC, "-1 minute"), (other, "-1 minute"), (CLINIC, "5 minutes")):
        user_id, _ = new_user(database, "owner", clinic)
        with connect(database) as conn:
            conn.execute(
                "INSERT INTO oauth_states (state_digest,clinic_id,user_id,"
                "code_verifier_ciphertext,key_id,expires_at)"
                " VALUES (%s,%s,%s,%s,'k1',now() + %s::interval)",
                (os.urandom(32), clinic, user_id, b"x" * 40, offset),
            )
    with connect(database) as conn:
        assert calendar_accounts.prune_oauth_states(conn) == 2
        assert conn.execute("SELECT count(*) AS n FROM oauth_states").fetchone()["n"] == 1


def test_run_loop_runs_housekeeping_when_idle(database):
    from threading import Event

    from clinic_voice.outbox import run_loop

    calls = []
    stop = Event()
    run_loop(
        database,
        "w-house",
        Ports(calendar=FakeCalendar()),
        stop,
        housekeeping=lambda conn: calls.append(conn),
        wait=lambda seconds: stop.set(),
    )
    assert len(calls) == 1


def test_run_loop_survives_housekeeping_failure(database, caplog):
    from threading import Event

    from clinic_voice.outbox import run_loop

    stop = Event()
    waits = []

    def housekeeping(conn):
        raise RuntimeError("synthetic " + SECRET_MARKER)

    def wait(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            stop.set()

    clock = iter([0.0, 1000.0, 2000.0])
    run_loop(
        database,
        "w-house",
        Ports(calendar=FakeCalendar()),
        stop,
        housekeeping=housekeeping,
        wait=wait,
        clock=lambda: next(clock),
    )
    assert len(waits) == 2  # Kept polling after both failures.
    assert "housekeeping failed error=RuntimeError" in caplog.text
    assert SECRET_MARKER not in caplog.text


# A disconnect whose Google revocation is still pending must not kill a reconnect


def connect_via_callback(client, auth, fake, token):
    _, query = start(client, auth)
    fake.refresh_token = token
    r = callback(client, state=query["state"], code=CODE)
    assert r.status_code == 200, r.text


def new_account_works(database, fake, token):
    map_doctor(database)
    appointment_id = book(database)
    run_once(database, "worker-g", google_ports(database, fake))
    row = appointment(database, appointment_id)
    assert row["status"] == "confirmed", row
    insert = fake.by("POST", "/events")[-1]
    assert owner_of(fake, insert) == token


def test_reconnect_cancels_pending_revocation(client, database, fake):
    auth = headers(client, database)
    old = make_account(database, token="synthetic-refresh-old")
    assert client.delete(f"/v1/admin/calendar/google/{old}", headers=auth).status_code == 204
    connect_via_callback(client, auth, fake, "synthetic-refresh-new")  # Same fake user.
    [job] = revoke_jobs(database)
    assert job["status"] == "done" and job["last_error"] == "revoke_superseded"
    old_row = next(a for a in accounts(database) if a["id"] == old)
    assert old_row["status"] == "revoked" and old_row["refresh_token_ciphertext"] is None
    assert ("calendar.revoke_superseded", old) in [
        (r["action"], r["target_id"]) for r in audit_rows(database)
    ]
    new_account_works(database, fake, "synthetic-refresh-new")
    assert fake.by("POST", "/revoke") == []


def test_reconnect_while_revocation_is_processing(client, database, fake):
    from clinic_voice.outbox import claim, process

    auth = headers(client, database)
    old = make_account(database, token="synthetic-refresh-old")
    assert client.delete(f"/v1/admin/calendar/google/{old}", headers=auth).status_code == 204
    ports = google_ports(database, fake)
    with connect(database) as conn:
        [job] = claim(conn, "worker-slow", kinds=["calendar.revoke_grant"])
        connect_via_callback(client, auth, fake, "synthetic-refresh-new")
        assert revoke_jobs(database)[0]["status"] == "processing"  # Fenced; not touched.
        assert process(conn, job, ports) is not False
    assert revoke_jobs(database)[0]["status"] == "done"
    assert fake.by("POST", "/revoke") == []
    new_account_works(database, fake, "synthetic-refresh-new")


def test_revocation_skipped_when_newer_account_exists(client, database, fake):
    auth = headers(client, database)
    old = make_account(database, token="synthetic-refresh-old")
    assert client.delete(f"/v1/admin/calendar/google/{old}", headers=auth).status_code == 204
    make_account(database, token="synthetic-refresh-new")  # Connected after the revoke.
    run_once(database, "worker-g", google_ports(database, fake))
    assert fake.by("POST", "/revoke") == []
    assert revoke_jobs(database)[0]["status"] == "done"
    old_row = next(a for a in accounts(database) if a["id"] == old)
    assert old_row["refresh_token_ciphertext"] is None
    new_account_works(database, fake, "synthetic-refresh-new")
