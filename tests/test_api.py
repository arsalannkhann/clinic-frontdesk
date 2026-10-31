import subprocess
import sys
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from clinic_voice.api import app, tool_caller
from clinic_voice.config import ENV_FILE, Settings, get_settings
from clinic_voice.credentials import parse
from clinic_voice.deps import ToolCaller

CLINIC = UUID("00000000-0000-4000-8000-000000000001")
WELL_FORMED = "cvt_" + "0" * 32 + "." + "A" * 43


@pytest.fixture
def client(monkeypatch):
    # An unreachable database proves the paths below never need one.
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused/unused")
    get_settings.cache_clear()
    with TestClient(app) as value:
        yield value
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def authorized(client):
    app.dependency_overrides[tool_caller] = lambda: ToolCaller(CLINIC, UUID(int=0))
    return client


def test_liveness(client):
    assert client.get("/health/live").json() == {"status": "ok"}


def test_missing_auth_fails_before_database(client):
    r = client.post("/v1/tools/check-slots", json={})
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "header",
    ["Bearer wrong", "Basic " + WELL_FORMED, "Bearer " + WELL_FORMED + "x", WELL_FORMED],
)
def test_malformed_auth_fails_before_database(client, header):
    r = client.post("/v1/tools/check-slots", headers={"Authorization": header}, json={})
    assert r.status_code == 401
    assert r.json() == {"detail": "Invalid credentials"}


def test_token_parsing():
    assert parse(WELL_FORMED) == (UUID(int=0), "A" * 43)
    assert parse(WELL_FORMED.upper()) is None
    assert parse(WELL_FORMED + "\n") is None
    assert parse("") is None


def test_validation_does_not_echo_sensitive_input(authorized):
    r = authorized.post(
        "/v1/tools/reserve-slot",
        headers={"Idempotency-Key": "test-key-0001"},
        json={"starts_at": "sensitive-invalid-value"},
    )
    assert r.status_code == 422
    assert "sensitive-invalid-value" not in r.text


def test_naive_datetime_rejected(authorized):
    r = authorized.post(
        "/v1/tools/reserve-slot",
        headers={"Idempotency-Key": "test-key-0001"},
        json={
            "doctor_id": str(CLINIC),
            "service_id": str(CLINIC),
            "starts_at": "2030-01-01T09:00:00",
        },
    )
    assert r.status_code == 422


def test_tenant_in_body_rejected(authorized):
    r = authorized.post(
        "/v1/tools/check-slots",
        json={
            "clinic_id": str(CLINIC),
            "doctor_id": str(CLINIC),
            "service_id": str(CLINIC),
            "day": "2030-01-01",
        },
    )
    assert r.status_code == 422


def test_tests_never_read_dotenv(tmp_path, monkeypatch):
    """The suite must not see a developer's .env (real secrets, conftest.py). Production
    still reads .env from the working directory."""
    (tmp_path / ".env").write_text(
        "DATABASE_URL=postgresql://from-dotenv/x\nBOLNA_API_KEY=synthetic-dotenv-key\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOLNA_API_KEY", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://from-env/x")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.bolna_api_key is None
        assert settings.database_url.get_secret_value() == "postgresql://from-env/x"
    finally:
        get_settings.cache_clear()
    # The declared production default, not conftest's override: config.ENV_FILE, and a fresh
    # interpreter (no conftest) shows Settings is configured with it.
    assert ENV_FILE == ".env"
    code = (
        "from clinic_voice.config import ENV_FILE, Settings\n"
        "assert Settings.model_config['env_file'] == ENV_FILE == '.env'\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=tmp_path)
    explicit = Settings(_env_file=ENV_FILE)
    assert explicit.bolna_api_key.get_secret_value() == "synthetic-dotenv-key"
