from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from clinic_voice.api import app
from clinic_voice.config import Settings, get_settings

CLINIC = UUID("00000000-0000-4000-8000-000000000001")
TOKEN = "synthetic-test-token-" + "x" * 32


@pytest.fixture
def client():
    settings = Settings(
        database_url=SecretStr("postgresql://unused/unused"),
        tool_token=SecretStr(TOKEN),
        clinic_id=CLINIC,
        _env_file=None,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    # tenant uses settings directly, so override cached provider as well in test env.
    import os

    old = dict(os.environ)
    os.environ.update(
        DATABASE_URL="postgresql://unused/unused", TOOL_TOKEN=TOKEN, CLINIC_ID=str(CLINIC)
    )
    get_settings.cache_clear()
    with TestClient(app) as value:
        yield value
    os.environ.clear()
    os.environ.update(old)
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def test_liveness(client):
    assert client.get("/health/live").json() == {"status": "ok"}


def test_missing_auth_fails_before_database(client):
    assert client.post("/v1/tools/check-slots", json={}).status_code == 401


def test_wrong_auth_fails(client):
    assert (
        client.post(
            "/v1/tools/check-slots", headers={"Authorization": "Bearer wrong"}, json={}
        ).status_code
        == 401
    )


def test_validation_does_not_echo_sensitive_input(client):
    r = client.post(
        "/v1/tools/reserve-slot",
        headers={"Authorization": "Bearer " + TOKEN, "Idempotency-Key": "test-key-0001"},
        json={"starts_at": "sensitive-invalid-value"},
    )
    assert r.status_code == 422
    assert "sensitive-invalid-value" not in r.text


def test_naive_datetime_rejected(client):
    r = client.post(
        "/v1/tools/reserve-slot",
        headers={"Authorization": "Bearer " + TOKEN, "Idempotency-Key": "test-key-0001"},
        json={
            "doctor_id": str(CLINIC),
            "service_id": str(CLINIC),
            "starts_at": "2030-01-01T09:00:00",
        },
    )
    assert r.status_code == 422


def test_placeholder_token_rejected():
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql://unused/unused",
            tool_token="replace-with-at-least-32-random-characters",
            clinic_id=CLINIC,
            _env_file=None,
        )


def test_tenant_in_body_rejected(client):
    r = client.post(
        "/v1/tools/check-slots",
        headers={"Authorization": "Bearer " + TOKEN},
        json={
            "clinic_id": str(CLINIC),
            "doctor_id": str(CLINIC),
            "service_id": str(CLINIC),
            "day": "2030-01-01",
        },
    )
    assert r.status_code == 422
