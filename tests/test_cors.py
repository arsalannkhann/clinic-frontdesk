"""Dashboard CORS (T11): only configured origins, only staff/admin paths, no credentials."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from clinic_voice.api import app
from clinic_voice.config import Settings, get_settings

ALLOWED = "https://dash.clinic.example"
OTHER = "https://evil.example"


@pytest.fixture
def make_client(monkeypatch):
    # An unreachable database: every request below is answered before touching it.
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused/unused")
    clients = []

    def make(origins=None):
        if origins is None:
            monkeypatch.delenv("DASHBOARD_ORIGINS", raising=False)
        else:
            monkeypatch.setenv("DASHBOARD_ORIGINS", origins)
        get_settings.cache_clear()
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)
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
