from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from clinic_voice.api import app, tenant
from clinic_voice.config import get_settings
from clinic_voice.credentials import parse

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
    app.dependency_overrides[tenant] = lambda: CLINIC
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
