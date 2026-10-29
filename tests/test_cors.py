"""Dashboard CORS (T11): only configured origins, only staff/admin paths, no credentials."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from clinic_voice.api import app
from clinic_voice.config import Settings, get_settings
from clinic_voice.deps import staff_session

ALLOWED = "https://dash.clinic.example"
OTHER = "https://evil.example"


@pytest.fixture
def make_client(monkeypatch):
    # An unreachable database: every request below is answered before touching it.
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused/unused")
    clients = []

    def make(origins=None, raise_server_exceptions=True):
        if origins is None:
            monkeypatch.delenv("DASHBOARD_ORIGINS", raising=False)
        else:
            monkeypatch.setenv("DASHBOARD_ORIGINS", origins)
        get_settings.cache_clear()
        client = TestClient(app, raise_server_exceptions=raise_server_exceptions)
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def preflight(client, origin, path="/v1/staff/login", method="POST", headers="content-type"):
    return client.options(
        path,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": headers,
        },
    )


def cors_headers(response):
    return {k: v for k, v in response.headers.items() if k.lower().startswith("access-control-")}


def test_allowed_origin_gets_preflight_headers(make_client):
    client = make_client(f"{ALLOWED}, http://localhost:5173")
    r = preflight(client, ALLOWED, "/v1/admin/clinic", "PATCH", "authorization,content-type")
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == ALLOWED
    assert "PATCH" in r.headers["access-control-allow-methods"]
    allowed_headers = r.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed_headers and "content-type" in allowed_headers
    assert "access-control-allow-credentials" not in r.headers
    local = preflight(client, "http://localhost:5173")
    assert local.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_allowed_origin_gets_headers_on_actual_response(make_client):
    client = make_client(ALLOWED)
    # A 401 must still be readable by the dashboard so it can return to login.
    r = client.get("/v1/staff/me", headers={"Origin": ALLOWED})
    assert r.status_code == 401
    assert r.headers["access-control-allow-origin"] == ALLOWED
    assert "access-control-allow-credentials" not in r.headers
    assert "origin" in r.headers.get("vary", "").lower()


def test_disallowed_origin_gets_no_headers(make_client):
    client = make_client(ALLOWED)
    r = preflight(client, OTHER)
    assert r.status_code == 400
    assert cors_headers(r).get("access-control-allow-origin") is None
    r = client.get("/v1/staff/me", headers={"Origin": OTHER})
    assert r.status_code == 401
    assert "access-control-allow-origin" not in r.headers
    # Origins match exactly: no suffix, scheme or port variants.
    for near in ("https://dash.clinic.example.evil.example", "http://dash.clinic.example"):
        assert "access-control-allow-origin" not in preflight(client, near).headers


def test_default_sends_no_cors_headers(make_client):
    client = make_client()
    assert get_settings().dashboard_origin_list() == []
    r = preflight(client, ALLOWED)
    assert r.status_code == 405  # No CORS layer: OPTIONS reaches the router.
    assert cors_headers(r) == {}
    r = client.get("/v1/staff/me", headers={"Origin": ALLOWED})
    assert cors_headers(r) == {}
    assert cors_headers(make_client("").get("/v1/staff/me", headers={"Origin": ALLOWED})) == {}


def test_only_dashboard_paths_get_cors(make_client):
    client = make_client(ALLOWED)
    for path in ("/v1/tools/check-slots", "/v1/bolna/context", "/health/live"):
        assert cors_headers(preflight(client, ALLOWED, path)) == {}
    assert cors_headers(client.get("/health/live", headers={"Origin": ALLOWED})) == {}


def test_origin_setting_is_normalized():
    settings = Settings(
        database_url="postgresql://unused/unused",
        dashboard_origins="HTTPS://Dash.Example:443/, https://dash.example, http://127.0.0.1:5173",
    )
    assert settings.dashboard_origin_list() == ["https://dash.example", "http://127.0.0.1:5173"]


@pytest.mark.parametrize(
    "value",
    [
        "*",
        "https://*.example",
        "http://dash.example",  # Plain http only for loopback.
        "https://dash.example/app",
        "https://dash.example?x=1",
        "https://user@dash.example",
        "dash.example",
        "javascript:alert(1)",
    ],
)
def test_invalid_origins_are_rejected(value):
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql://unused/unused", dashboard_origins=value)


def _boom():
    raise RuntimeError("unexpected failure with secret detail")


def test_unhandled_error_keeps_cors_headers_for_allowed_origin(make_client):
    client = make_client(ALLOWED, raise_server_exceptions=False)
    app.dependency_overrides[staff_session] = _boom
    r = client.get("/v1/staff/me", headers={"Origin": ALLOWED})
    assert r.status_code == 500
    assert r.json() == {"detail": "Internal server error"}
    assert "secret" not in r.text
    assert r.headers["access-control-allow-origin"] == ALLOWED
    assert r.headers["cache-control"] == "no-store"
    # A disallowed origin gets the same generic 500 without CORS headers.
    r = client.get("/v1/staff/me", headers={"Origin": OTHER})
    assert r.status_code == 500
    assert "access-control-allow-origin" not in r.headers


def test_unhandled_error_is_still_raised_to_the_server(make_client):
    client = make_client(ALLOWED)  # raise_server_exceptions=True: the error propagates.
    app.dependency_overrides[staff_session] = _boom
    with pytest.raises(RuntimeError):
        client.get("/v1/staff/me", headers={"Origin": ALLOWED})


def test_staff_and_admin_responses_are_no_store(make_client):
    client = make_client()  # Independent of CORS configuration.
    for path in ("/v1/staff/me", "/v1/admin/clinic", "/v1/admin/appointments"):
        r = client.get(path)
        assert r.status_code == 401
        assert r.headers["cache-control"] == "no-store"
    app.dependency_overrides[staff_session] = lambda: {
        "id": "00000000-0000-4000-8000-000000000009",
        "clinic_id": "00000000-0000-4000-8000-000000000001",
        "email": "owner@example.test",
        "role": "owner",
    }
    r = client.get("/v1/staff/me")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    # Other routes are untouched.
    assert "cache-control" not in client.get("/health/live").headers


def test_path_prefix_fails_closed_for_cors_but_keeps_no_store(make_client):
    client = make_client(ALLOWED)
    prefixed = TestClient(app, root_path="/api")
    prefixed.__enter__()
    try:
        r = prefixed.get("/api/v1/staff/me", headers={"Origin": ALLOWED})
        assert "access-control-allow-origin" not in r.headers
        assert r.headers["cache-control"] == "no-store"
    finally:
        prefixed.__exit__(None, None, None)
    assert client.get("/v1/staff/me", headers={"Origin": ALLOWED}).headers[
        "access-control-allow-origin"
    ] == (ALLOWED)
