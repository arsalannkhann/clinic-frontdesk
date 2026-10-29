"""Bolna trusted gateway, call sessions, agent compiler and Bolna API adapter.

Synthetic data only: example.test hosts, +9100000xxxxx numbers, generated credentials.
Bolna HTTP goes through httpx.MockTransport; nothing reaches the network.
"""

import base64
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from clinic_voice import cli, credentials, service
from clinic_voice.api import app
from clinic_voice.bolna import agent_config, gateway
from clinic_voice.cli import DEMO_CLINIC_ID as CLINIC
from clinic_voice.cli import DOCTOR_ID, SERVICE_ID
from clinic_voice.config import get_settings
from clinic_voice.crypto import CryptoError, Keyring, phone_hash
from clinic_voice.db import connect
from clinic_voice.providers.bolna import BolnaClient, Execution, FakeBolna, PhoneNumber
from clinic_voice.providers.calendar import (
    ProviderError,
    ProviderPermanent,
    ProviderRetryable,
    ProviderTimeout,
)
from clinic_voice.schemas import ReservationRequest

FIXTURES = Path(__file__).parent / "fixtures" / "bolna"
IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2031, 3, 3, 8, 30, tzinfo=IST)
DAY = NOW.date() + timedelta(days=2)
SLOT = datetime.combine(DAY, time(9, 0), IST)
KEYS = "k1:" + base64.b64encode(b"\x02" * 32).decode()
HASH_KEY_BYTES = b"\x03" * 32
HASH_KEY = base64.b64encode(HASH_KEY_BYTES).decode()
CALLER = "+910000000123"
EXECUTION = "5f0c6f7e-1d1a-4c55-9a43-0b6f1b2a0001"
OTHER_EXECUTION = "5f0c6f7e-1d1a-4c55-9a43-0b6f1b2a0002"
AGENT = "11111111-1111-4111-8111-111111111111"
OTHER_CLINIC = UUID("00000000-0000-4000-8000-0000000000b1")
OTHER_DOCTOR = UUID("00000000-0000-4000-8000-0000000000b2")
OTHER_SERVICE = UUID("00000000-0000-4000-8000-0000000000b3")
SYNTHETIC_TOKEN = "cvt_" + "0" * 32 + "." + "A" * 43
MARKER = "SYNTHETIC-BODY-MARKER"


@pytest.fixture
def env(database, monkeypatch):
    monkeypatch.setattr(service, "utc_now", lambda: NOW.astimezone(UTC))
    monkeypatch.setenv("DATABASE_URL", database)
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEYS", KEYS)
    monkeypatch.setenv("PHONE_HASH_KEY", HASH_KEY)
    get_settings.cache_clear()
    with connect(database) as conn:
        conn.execute(
            "INSERT INTO clinics (id,name) VALUES (%s,'Other Synthetic Clinic')", (OTHER_CLINIC,)
        )
        conn.execute(
            "INSERT INTO doctors (id,clinic_id,name) VALUES (%s,%s,'Other Doctor')",
            (OTHER_DOCTOR, OTHER_CLINIC),
        )
        conn.execute(
            """INSERT INTO services (id,clinic_id,doctor_id,name,duration_minutes)
            VALUES (%s,%s,%s,'Consultation',15)""",
            (OTHER_SERVICE, OTHER_CLINIC, OTHER_DOCTOR),
        )
        for day in range(7):
            conn.execute(
                "INSERT INTO working_hours VALUES (%s,%s,%s,'09:00','17:00')",
                (OTHER_CLINIC, OTHER_DOCTOR, day),
            )
        _, token = credentials.issue(conn, CLINIC, "bolna-test")
        _, other = credentials.issue(conn, OTHER_CLINIC, "bolna-test-other")
    with TestClient(app) as client:
        yield {"client": client, "url": database, "token": token, "other": other}
    get_settings.cache_clear()


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def call(execution=EXECUTION, number=CALLER, agent=AGENT, **extra):
    return {"execution_id": execution, "from_number": number, "agent_id": agent, **extra}


def reserve_body(execution=EXECUTION, starts_at=SLOT, **extra):
    return call(
        execution,
        doctor="Demo Doctor",
        service="Consultation",
        starts_at=starts_at.isoformat(),
        **extra,
    )


def post(env, path, body, token=None):
    return env["client"].post(path, json=body, headers=auth(token or env["token"]))


def sessions(url):
    with connect(url) as conn:
        return conn.execute("SELECT * FROM call_sessions ORDER BY created_at").fetchall()


# Authentication and tenant isolation


@pytest.mark.parametrize(
    "path",
    [
        "/v1/bolna/context",
        "/v1/bolna/tools/check-slots",
        "/v1/bolna/tools/reserve-slot",
        "/v1/bolna/tools/appointment-status",
    ],
)
def test_credential_required(env, path):
    for headers in ({}, auth("cvt_" + "f" * 32 + "." + "B" * 43), {"X-Token": env["token"]}):
        r = env["client"].post(path, json=reserve_body(), headers=headers)
        assert r.status_code == 401
        assert r.json() == {"detail": "Invalid credentials"}
    assert sessions(env["url"]) == []


def test_revoked_credential_rejected(env):
    with connect(env["url"]) as conn:
        credentials.revoke(conn, credentials.parse(env["token"])[0])
    assert (
        post(
            env, "/v1/bolna/tools/check-slots", call(doctor="x", service="y", day=str(DAY))
        ).status_code
        == 401
    )


def test_other_clinic_cannot_see_session_or_appointment(env):
    created = post(env, "/v1/bolna/tools/reserve-slot", reserve_body())
    assert created.status_code == 202
    appointment_id = created.json()["appointment_id"]
    # Same execution id under another clinic's credential: a separate session, no access.
    r = post(
        env,
        "/v1/bolna/tools/appointment-status",
        call(appointment_id=appointment_id),
        token=env["other"],
    )
    assert r.status_code == 404
    rows = sessions(env["url"])
    assert {row["clinic_id"] for row in rows} == {CLINIC, OTHER_CLINIC}
    assert len({row["id"] for row in rows}) == 2
    # The other clinic's catalog does not include this clinic's doctor.
    r = post(
        env,
        "/v1/bolna/tools/check-slots",
        call(doctor="Demo Doctor", service="Consultation", day=str(DAY)),
        token=env["other"],
    )
    assert r.status_code == 404


def test_body_cannot_choose_tenant(env):
    r = post(
        env,
        "/v1/bolna/tools/check-slots",
        call(
            doctor="Demo Doctor", service="Consultation", day=str(DAY), clinic_id=str(OTHER_CLINIC)
        ),
    )
    assert r.status_code == 422


# Call sessions


def test_session_upsert_and_fill(env):
    body = call(
        number="", agent="%(agent_id)s", doctor="Demo Doctor", service="Consultation", day=str(DAY)
    )
    assert post(env, "/v1/bolna/tools/check-slots", body).status_code == 200
    [row] = sessions(env["url"])
    assert row["caller_number_hash"] is None and row["agent_id"] is None
    assert row["status"] == "active" and row["provider"] == "bolna"
    assert row["execution_id"] == EXECUTION
    # A later call of the same execution fills the missing values once.
    body.update(from_number=CALLER, agent_id=AGENT)
    assert post(env, "/v1/bolna/tools/check-slots", body).status_code == 200
    body.update(from_number="+910000000999")
    assert post(env, "/v1/bolna/tools/check-slots", body).status_code == 200
    [row] = sessions(env["url"])
    assert row["agent_id"] == AGENT
    assert bytes(row["caller_number_hash"]) == phone_hash(HASH_KEY_BYTES, CALLER)


def test_concurrent_session_upsert_creates_one_row(env):
    barrier = Barrier(4)
    keyring = Keyring.from_setting(KEYS)

    def run(_):
        barrier.wait()
        with connect(env["url"]) as conn:
            return gateway.upsert_session(
                conn, CLINIC, gateway_context(), (keyring, HASH_KEY_BYTES)
            )

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = set(pool.map(run, range(4)))
    assert len(ids) == 1
    assert len(sessions(env["url"])) == 1


def gateway_context():
    from clinic_voice.schemas import BolnaCallContext

    return BolnaCallContext(**call())


def test_caller_number_encrypted_and_hashed(env):
    assert post(env, "/v1/bolna/tools/reserve-slot", reserve_body()).status_code == 202
    with connect(env["url"]) as conn:
        text = conn.execute("SELECT s::text AS t FROM call_sessions s").fetchone()["t"]
        appointment = conn.execute("SELECT a::text AS t FROM appointments a").fetchone()["t"]
    for value in (text, appointment):
        assert CALLER not in value and CALLER[1:] not in value and "0000000123" not in value
    [row] = sessions(env["url"])
    keyring = Keyring.from_setting(KEYS)
    aad = gateway.session_aad(CLINIC, row["id"])
    blob = row["caller_number_ciphertext"]
    assert keyring.decrypt(row["caller_number_key_id"], blob, aad).decode() == CALLER
    with pytest.raises(CryptoError):  # Bound to its row: another session's AAD fails.
        keyring.decrypt(row["caller_number_key_id"], blob, gateway.session_aad(CLINIC, uuid4()))
    assert bytes(row["caller_number_hash"]) == phone_hash(HASH_KEY_BYTES, CALLER)


@pytest.mark.parametrize("missing", ["PHONE_HASH_KEY", "TOKEN_ENCRYPTION_KEYS"])
def test_storage_keys_required(env, monkeypatch, missing):
    monkeypatch.setenv(missing, "")
    get_settings.cache_clear()
    r = post(
        env,
        "/v1/bolna/tools/check-slots",
        call(doctor="Demo Doctor", service="Consultation", day=str(DAY)),
    )
    assert r.status_code == 503
    assert r.json() == {"detail": "Call session storage not configured"}
    assert sessions(env["url"]) == []


def test_invalid_hash_key_fails_clearly(env, monkeypatch):
    monkeypatch.setenv("PHONE_HASH_KEY", base64.b64encode(b"short").decode())
    get_settings.cache_clear()
    r = post(
        env,
        "/v1/bolna/tools/check-slots",
        call(doctor="Demo Doctor", service="Consultation", day=str(DAY)),
    )
    assert r.status_code == 503


def test_ended_session_refuses_tools(env):
    assert post(env, "/v1/bolna/tools/reserve-slot", reserve_body()).status_code == 202
    with connect(env["url"]) as conn:
        conn.execute("UPDATE call_sessions SET status='ended', ended_at=now()")
    r = post(env, "/v1/bolna/tools/reserve-slot", reserve_body(starts_at=SLOT + timedelta(hours=1)))
    assert r.status_code == 409
    with connect(env["url"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM appointments").fetchone()["n"] == 1


def test_validation_errors_do_not_echo_caller_number(env):
    r = post(
        env,
        "/v1/bolna/tools/check-slots",
        call(execution="not-a-uuid", doctor="d", service="s", day="x"),
    )
    assert r.status_code == 422
    assert CALLER not in r.text and "not-a-uuid" not in r.text


# Tools


def test_check_slots_speakable(env):
    r = post(
        env,
        "/v1/bolna/tools/check-slots",
        call(doctor="  demo   DOCTOR ", service="consultation", day=str(DAY)),
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["slots"]) == 3
    assert datetime.fromisoformat(body["slots"][0]) == SLOT
    assert body["spoken"] == "Open times on Wednesday 5 March: 9:00 AM, 9:15 AM or 9:30 AM."
    assert r.headers["Cache-Control"] == "no-store"
    assert CALLER not in r.text


def test_check_slots_unknown_names(env):
    r = post(
        env,
        "/v1/bolna/tools/check-slots",
        call(doctor="Nobody", service="Consultation", day=str(DAY)),
    )
    assert r.status_code == 404
    r = post(
        env,
        "/v1/bolna/tools/check-slots",
        call(doctor="Demo Doctor", service="Surgery", day=str(DAY)),
    )
    assert r.status_code == 404
    with connect(env["url"]) as conn:
        conn.execute("UPDATE doctors SET active=false WHERE id=%s", (DOCTOR_ID,))
    r = post(
        env,
        "/v1/bolna/tools/check-slots",
        call(doctor="Demo Doctor", service="Consultation", day=str(DAY)),
    )
    assert r.status_code == 404


def test_reserve_replays_identical_args(env):
    first = post(env, "/v1/bolna/tools/reserve-slot", reserve_body())
    # Bolna retry: same instant in another offset, a different caller-number rendering.
    again = post(
        env,
        "/v1/bolna/tools/reserve-slot",
        reserve_body(starts_at=SLOT.astimezone(UTC), from_number=""),
    )
    assert first.status_code == again.status_code == 202
    assert first.json() == again.json()
    body = first.json()
    assert body["status"] == "pending_sync"
    assert "not yet confirmed" in body["spoken"]
    assert set(body) == {"appointment_id", "status", "starts_at", "spoken"}
    args = {
        "doctor_id": str(DOCTOR_ID),
        "service_id": str(SERVICE_ID),
        "starts_at": SLOT.astimezone(UTC).isoformat(),
    }
    key = gateway.derive_key(UUID(EXECUTION), "reserve-slot", args)
    assert key.startswith(f"bolna:{EXECUTION}:reserve-slot:") and len(key.rsplit(":", 1)[1]) == 24
    with connect(env["url"]) as conn:
        rows = conn.execute("SELECT idempotency_key, call_session_id FROM appointments").fetchall()
        assert conn.execute("SELECT count(*) AS n FROM outbox").fetchone()["n"] == 1
    assert [r["idempotency_key"] for r in rows] == [key]
    assert rows[0]["call_session_id"] == sessions(env["url"])[0]["id"]


def test_reserve_different_args_distinct_key(env):
    a = post(env, "/v1/bolna/tools/reserve-slot", reserve_body()).json()
    b = post(
        env, "/v1/bolna/tools/reserve-slot", reserve_body(starts_at=SLOT + timedelta(hours=1))
    ).json()
    assert a["appointment_id"] != b["appointment_id"]
    with connect(env["url"]) as conn:
        keys = {
            r["idempotency_key"] for r in conn.execute("SELECT idempotency_key FROM appointments")
        }
    assert len(keys) == 2
    # The same args in another call are another action, not a replay: the slot is taken.
    r = post(env, "/v1/bolna/tools/reserve-slot", reserve_body(execution=OTHER_EXECUTION))
    assert r.status_code == 409


def test_derive_key_canonical():
    args = {"b": "2", "a": "1"}
    assert gateway.derive_key(UUID(EXECUTION), "t", args) == gateway.derive_key(
        UUID(EXECUTION), "t", {"a": "1", "b": "2"}
    )
    assert gateway.derive_key(UUID(EXECUTION), "t", args) != gateway.derive_key(
        UUID(OTHER_EXECUTION), "t", args
    )
    assert gateway.derive_key(UUID(EXECUTION), "t", args) != gateway.derive_key(
        UUID(EXECUTION), "u", args
    )


def test_concurrent_identical_reserves_one_appointment(env):
    barrier = Barrier(4)
    client = env["client"]

    def run(_):
        barrier.wait()
        r = client.post(
            "/v1/bolna/tools/reserve-slot", json=reserve_body(), headers=auth(env["token"])
        )
        return r.status_code, r.json().get("appointment_id")

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(run, range(4)))
    assert {status for status, _ in outcomes} == {202}
    assert len({appointment for _, appointment in outcomes}) == 1
    with connect(env["url"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM appointments").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM call_sessions").fetchone()["n"] == 1


def test_generic_route_rejects_gateway_key_prefix(env):
    r = env["client"].post(
        "/v1/tools/reserve-slot",
        json={
            "doctor_id": str(DOCTOR_ID),
            "service_id": str(SERVICE_ID),
            "starts_at": SLOT.isoformat(),
        },
        headers={**auth(env["token"]), "Idempotency-Key": f"bolna:{EXECUTION}:reserve-slot:abc"},
    )
    assert r.status_code == 422


def test_appointment_status_bound_to_session(env):
    appointment_id = post(env, "/v1/bolna/tools/reserve-slot", reserve_body()).json()[
        "appointment_id"
    ]
    path = "/v1/bolna/tools/appointment-status"
    r = post(env, path, call(appointment_id=appointment_id))
    assert r.status_code == 200
    assert r.json()["status"] == "pending_sync"
    assert "confirmed" not in r.json()["spoken"].replace("not yet confirmed", "")
    assert r.json()["spoken"] == service.spoken_status("pending_sync")
    # Another execution of the same clinic cannot read it.
    assert (
        post(env, path, call(execution=OTHER_EXECUTION, appointment_id=appointment_id)).status_code
        == 404
    )
    # Appointments made outside any call session are invisible to the gateway.
    plain = service.reserve(
        env["url"],
        CLINIC,
        ReservationRequest(
            doctor_id=DOCTOR_ID, service_id=SERVICE_ID, starts_at=SLOT + timedelta(hours=2)
        ),
        "plain-key-0001",
    )
    assert post(env, path, call(appointment_id=str(plain["appointment_id"]))).status_code == 404
    assert post(env, path, call(appointment_id=str(uuid4()))).status_code == 404
    # Only a persisted confirmed status is spoken as confirmed.
    with connect(env["url"]) as conn:
        conn.execute("UPDATE appointments SET status='confirmed' WHERE id=%s", (appointment_id,))
    r = post(env, path, call(appointment_id=appointment_id))
    assert r.json()["spoken"] == "Your appointment is confirmed."
    assert set(r.json()) == {"appointment_id", "status", "spoken"}


def test_call_session_fk_is_clinic_scoped(env):
    appointment_id = post(env, "/v1/bolna/tools/reserve-slot", reserve_body()).json()[
        "appointment_id"
    ]
    post(
        env,
        "/v1/bolna/tools/check-slots",
        call(doctor="Other Doctor", service="Consultation", day=str(DAY)),
        token=env["other"],
    )
    other_session = [r for r in sessions(env["url"]) if r["clinic_id"] == OTHER_CLINIC][0]["id"]
    from psycopg.errors import ForeignKeyViolation

    with pytest.raises(ForeignKeyViolation), connect(env["url"]) as conn:
        conn.execute(
            "UPDATE appointments SET call_session_id=%s WHERE id=%s",
            (other_session, appointment_id),
        )


# Context


def test_context_has_no_phi(env):
    appointment_id = post(env, "/v1/bolna/tools/reserve-slot", reserve_body()).json()[
        "appointment_id"
    ]
    with connect(env["url"]) as conn:
        conn.execute(
            "INSERT INTO services (id,clinic_id,doctor_id,name,duration_minutes,active) VALUES (%s,%s,%s,'Retired',15,false)",
            (uuid4(), CLINIC, DOCTOR_ID),
        )
    r = env["client"].post("/v1/bolna/context", headers=auth(env["token"]))
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "clinic_name": "Synthetic Demo Clinic",
        "timezone": "Asia/Kolkata",
        "today": NOW.date().isoformat(),
        "doctors": [{"name": "Demo Doctor", "services": ["Consultation"]}],
        "supported_languages": ["en", "hi"],
        "faq_text": gateway.PLACEHOLDER_FAQ,
        "emergency_text": gateway.PLACEHOLDER_EMERGENCY,
        "approved_text": False,
    }
    for secret in (CALLER, appointment_id, EXECUTION, str(DOCTOR_ID)):
        assert secret not in r.text
    # Used as a tool, the body's call context records the session too.
    r = env["client"].post(
        "/v1/bolna/context", json=call(execution=OTHER_EXECUTION), headers=auth(env["token"])
    )
    assert r.status_code == 200 and CALLER not in r.text
    assert len(sessions(env["url"])) == 2
    other = env["client"].post("/v1/bolna/context", headers=auth(env["other"])).json()
    assert other["clinic_name"] == "Other Synthetic Clinic"


# Agent configuration compiler


CLINIC_CONFIG = {"name": "Synthetic Demo Clinic", "timezone": "Asia/Kolkata"}
GATEWAY = "https://voice.example.test"


def compiled(token=SYNTHETIC_TOKEN, **kwargs):
    return agent_config.compile_agent(
        CLINIC_CONFIG, gateway_base_url=GATEWAY, tool_token=token, **kwargs
    )


def test_compiler_matches_contract_fixture():
    expected = json.loads((FIXTURES / "agent_synthetic_clinic.json").read_text())
    assert agent_config.redact(compiled()) == expected
    assert compiled(agent_config.REDACTED) == expected


def test_compiler_token_only_in_memory_payload():
    payload = compiled()
    text = json.dumps(payload)
    params = payload["agent_config"]["tasks"][0]["tools_config"]["api_tools"]["tools_params"]
    assert text.count(SYNTHETIC_TOKEN) == len(params)
    assert all(p["api_token"] == f"Bearer {SYNTHETIC_TOKEN}" for p in params.values())
    redacted = json.dumps(agent_config.redact(payload))
    assert SYNTHETIC_TOKEN not in redacted
    assert SYNTHETIC_TOKEN in json.dumps(payload)  # redact() copies; the original is intact.
    for fixture in FIXTURES.glob("*.json"):
        assert "cvt_" not in fixture.read_text()


def test_compiler_structure_follows_docs():
    payload = compiled()
    task = payload["agent_config"]["tasks"][0]
    tools = task["tools_config"]["api_tools"]
    names = [tool["name"] for tool in tools["tools"]]
    assert set(names) == set(tools["tools_params"])
    for tool in tools["tools"]:
        assert tool["key"] == "custom_task"
        properties = tool["parameters"]["properties"]
        assert {"execution_id", "from_number", "agent_id"} <= set(properties)
        assert all(p["type"] == "string" for p in properties.values())
        params = tools["tools_params"][tool["name"]]
        param = json.loads(params["param"])
        assert param == {name: f"%({name})s" for name in properties}
        # Platform-rendered context fields come last in the template (injection hardening).
        assert list(param)[-3:] == ["execution_id", "from_number", "agent_id"]
        assert params["method"] == "POST" and params["url"].startswith(GATEWAY + "/v1/bolna/")
    routes = set(app.openapi()["paths"])
    for params in tools["tools_params"].values():
        assert params["url"].removeprefix(GATEWAY) in routes
    multilingual = task["tools_config"]["multilingual_config"]
    assert multilingual["enabled"] is True
    assert len(multilingual["languages"]) >= 2
    assert multilingual["active_language"] in multilingual["languages"]
    for code, entry in multilingual["languages"].items():
        assert entry["synthesizer"]["provider_config"]["language"] == code
        assert entry["transcriber"] == {"language": code}
    prompt = payload["agent_prompts"]["task_1"]["system_prompt"]
    for variable in ("{execution_id}", "{from_number}", "{agent_id}"):
        assert variable in prompt
    assert "# Receptionist system prompt template" not in prompt


def test_compiler_rejects_unsafe_inputs():
    with pytest.raises(ValueError):
        compiled("not-a-token")
    for url in (
        "http://voice.example.test",
        "https://voice.example.test/?x=1",
        "voice.example.test",
    ):
        with pytest.raises(ValueError):
            agent_config.compile_agent(
                CLINIC_CONFIG, gateway_base_url=url, tool_token=agent_config.REDACTED
            )
    with pytest.raises(ValueError):
        agent_config.AgentProfile(transcriber_model="saarika:v2.5")
    with pytest.raises(ValueError):
        agent_config.AgentProfile(active_language="ta")
    voices = {
        "en": agent_config.VoiceSettings(),
        "hi": agent_config.VoiceSettings(model="bulbul:v2"),
    }
    with pytest.raises(ValueError):
        agent_config.AgentProfile(voices=voices)


def test_compiler_neutralizes_template_characters():
    payload = agent_config.compile_agent(
        {"name": "Dr {from_number} %(x)s Clinic", "timezone": "Asia/Kolkata"},
        gateway_base_url=GATEWAY,
        tool_token=agent_config.REDACTED,
    )
    prompt = payload["agent_prompts"]["task_1"]["system_prompt"]
    assert "Dr from_number (x)s Clinic" in prompt
    assert payload["agent_config"]["agent_name"] == "Dr from_number (x)s Clinic receptionist"


def test_cli_compile_agent_prints_redacted(env, capsys, monkeypatch):
    monkeypatch.setenv("BOLNA_GATEWAY_BASE_URL", GATEWAY)
    get_settings.cache_clear()
    cli.main(["bolna", "compile-agent", "--clinic", str(CLINIC)])
    out = json.loads(capsys.readouterr().out)
    assert out == json.loads((FIXTURES / "agent_synthetic_clinic.json").read_text())
    with pytest.raises(SystemExit):
        cli.main(["bolna", "compile-agent", "--clinic", str(uuid4())])


# Bolna API adapter


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class Recorder:
    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return self.handler(request)


def client_for(handler, **kwargs):
    recorder = Recorder(handler)
    http = httpx.Client(transport=httpx.MockTransport(recorder))
    return BolnaClient(
        "synthetic-bolna-key", http, base_url="https://api.example.test", **kwargs
    ), recorder


def test_client_happy_paths():
    routes = {
        ("POST", "/v2/agent"): (201, "api_create_agent_201.json"),
        ("PUT", f"/v2/agent/{AGENT}"): (200, "api_update_agent_200.json"),
        ("GET", "/executions/33333333-3333-4333-8333-333333333333"): (
            200,
            "api_execution_200.json",
        ),
        ("GET", "/phone-numbers/all"): (200, "api_phone_numbers_200.json"),
        ("POST", "/inbound/setup"): (200, "api_inbound_setup_200.json"),
        ("POST", "/call"): (200, "api_call_200.json"),
        ("POST", "/call/44444444-4444-4444-8444-444444444444/stop"): (
            200,
            "api_stop_call_200.json",
        ),
    }

    def handler(request):
        status, name = routes[(request.method, request.url.path)]
        return httpx.Response(status, json=fixture(name))

    bolna, recorder = client_for(handler)
    payload = compiled(agent_config.REDACTED)
    assert bolna.create_agent(payload) == AGENT
    assert bolna.update_agent(AGENT, payload) is None
    execution = bolna.get_execution("33333333-3333-4333-8333-333333333333")
    assert execution == Execution(
        "33333333-3333-4333-8333-333333333333", AGENT, "completed", None, "inbound", 182.5
    )
    assert execution.terminal
    assert MARKER not in repr(execution) and "+91" not in repr(execution)
    [number] = bolna.list_phone_numbers()
    assert number == PhoneNumber("0123456789abcdef0123456789abcdef", "+910000000009", None, "plivo")
    bolna.assign_phone_number(number.id, AGENT)
    scheduled = datetime(2031, 3, 4, 10, 0, tzinfo=IST)
    assert (
        bolna.start_outbound_call(
            AGENT, "+910000000002", scheduled_at=scheduled, user_data={"k": "v"}
        )
        == "44444444-4444-4444-8444-444444444444"
    )
    bolna.stop_scheduled_call("44444444-4444-4444-8444-444444444444")
    assert all(
        r.headers["Authorization"] == "Bearer synthetic-bolna-key" for r in recorder.requests
    )
    by_path = {(r.method, r.url.path): r for r in recorder.requests}
    assert json.loads(by_path[("POST", "/inbound/setup")].content) == {
        "agent_id": AGENT,
        "phone_number_id": number.id,
    }
    call_body = json.loads(by_path[("POST", "/call")].content)
    assert call_body == {
        "agent_id": AGENT,
        "recipient_phone_number": "+910000000002",
        "scheduled_at": scheduled.isoformat(),
        "user_data": {"k": "v"},
    }
    assert json.loads(by_path[("PUT", f"/v2/agent/{AGENT}")].content) == payload
    assert "synthetic-bolna-key" not in repr(bolna)


def reads(bolna):
    return (
        lambda: bolna.list_phone_numbers(),
        lambda: bolna.get_execution(EXECUTION),
    )


def writes(bolna):
    return (
        lambda: bolna.create_agent({}),
        lambda: bolna.update_agent(AGENT, {}),
        lambda: bolna.assign_phone_number("0123456789abcdef0123456789abcdef", AGENT),
        lambda: bolna.start_outbound_call(AGENT, "+910000000002"),
        lambda: bolna.stop_scheduled_call(EXECUTION),
    )


def test_client_error_mapping(caplog):
    caplog.set_level(logging.DEBUG)
    body = fixture("api_error_400.json")
    # (status, headers, expected for reads, expected for writes); expected = (kind, code, wait)
    cases = [
        (429, {"Retry-After": "7"}, (ProviderRetryable, "http_429", 7.0), None),
        (500, {}, (ProviderRetryable, "http_500", None), (ProviderTimeout, "http_500", None)),
        (502, {}, (ProviderRetryable, "http_502", None), (ProviderTimeout, "http_502", None)),
        (
            503,
            {"Retry-After": "9"},
            (ProviderRetryable, "http_503", 9.0),
            (ProviderTimeout, "http_503", None),
        ),
        (400, {}, (ProviderPermanent, "http_400", None), (ProviderPermanent, "http_400", None)),
        (
            401,
            {},
            (ProviderPermanent, "unauthorized", None),
            (ProviderPermanent, "unauthorized", None),
        ),
        (
            403,
            {},
            (ProviderPermanent, "unauthorized", None),
            (ProviderPermanent, "unauthorized", None),
        ),
    ]
    for status, headers, on_read, on_write in cases:
        bolna, _ = client_for(
            lambda r, s=status, h=headers: httpx.Response(s, headers=h, json=body)
        )
        groups = [(reads(bolna), on_read)]
        if on_write is not None:
            groups.append((writes(bolna), on_write))
        for actions, (kind, code, wait) in groups:
            for action in actions:
                with pytest.raises(ProviderError) as error:
                    action()
                assert type(error.value) is kind, (status, code)
                assert str(error.value) == code
                assert getattr(error.value, "retry_after", None) == wait
                assert MARKER not in str(error.value) and error.value.__cause__ is None
    assert MARKER not in caplog.text and "synthetic-bolna-key" not in caplog.text


def test_client_write_429_is_unknown_unless_documented_safe():
    bolna, _ = client_for(lambda r: httpx.Response(429, json=fixture("api_error_400.json")))
    for action in writes(bolna)[:3] + writes(bolna)[4:]:
        with pytest.raises(ProviderTimeout) as error:
            action()
        assert str(error.value) == "http_429"
    # POST /call documents 429 as "number still activating; nothing queued": retryable.
    with pytest.raises(ProviderRetryable):
        writes(bolna)[3]()


def test_client_accepts_any_2xx_on_writes():
    bolna, _ = client_for(lambda r: httpx.Response(204))
    bolna.update_agent(AGENT, {})
    bolna.assign_phone_number("0123456789abcdef0123456789abcdef", AGENT)
    bolna.stop_scheduled_call(EXECUTION)
    bolna, _ = client_for(lambda r: httpx.Response(200, json=fixture("api_create_agent_201.json")))
    assert bolna.create_agent({}) == AGENT
    bolna, _ = client_for(lambda r: httpx.Response(202, json=fixture("api_call_200.json")))
    assert (
        bolna.start_outbound_call(AGENT, "+910000000002") == "44444444-4444-4444-8444-444444444444"
    )
    # An unexpected 2xx whose body cannot be parsed: the write may have happened.
    bolna, _ = client_for(lambda r: httpx.Response(202, text="<html>"))
    for action in (
        lambda: bolna.create_agent({}),
        lambda: bolna.start_outbound_call(AGENT, "+910000000002"),
    ):
        with pytest.raises(ProviderTimeout) as error:
            action()
        assert str(error.value) == "malformed"


def test_client_call_429_uses_body_retry_after():
    bolna, _ = client_for(lambda r: httpx.Response(429, json=fixture("api_call_429.json")))
    with pytest.raises(ProviderRetryable) as error:
        bolna.start_outbound_call(AGENT, "+910000000002")
    assert error.value.retry_after == 120.0
    assert MARKER not in str(error.value)


def test_client_execution_not_found_and_malformed():
    bolna, _ = client_for(lambda r: httpx.Response(404, json=fixture("api_error_400.json")))
    assert bolna.get_execution(EXECUTION) is None
    bolna, _ = client_for(lambda r: httpx.Response(200, text="<html>"))
    with pytest.raises(ProviderRetryable):
        bolna.get_execution(EXECUTION)
    with pytest.raises(ProviderRetryable):
        bolna.list_phone_numbers()


def test_client_write_timeout_is_never_success():
    def timeout(request):
        raise httpx.ReadTimeout("synthetic", request=request)

    bolna, _ = client_for(timeout)
    for action in (
        lambda: bolna.create_agent({}),
        lambda: bolna.update_agent(AGENT, {}),
        lambda: bolna.assign_phone_number("0123456789abcdef0123456789abcdef", AGENT),
        lambda: bolna.start_outbound_call(AGENT, "+910000000002"),
        lambda: bolna.stop_scheduled_call(EXECUTION),
    ):
        with pytest.raises(ProviderTimeout):
            action()
    # Reads that time out changed nothing: retryable, not unknown.
    with pytest.raises(ProviderRetryable) as error:
        bolna.get_execution(EXECUTION)
    assert str(error.value) == "timeout"


def test_client_connect_failure_is_retryable():
    def refused(request):
        raise httpx.ConnectError("synthetic", request=request)

    bolna, _ = client_for(refused)
    with pytest.raises(ProviderRetryable) as error:
        bolna.start_outbound_call(AGENT, "+910000000002")
    assert str(error.value) == "network"


def test_client_malformed_write_success_is_unknown_outcome():
    bolna, _ = client_for(lambda r: httpx.Response(201, json={"state": "created"}))
    with pytest.raises(ProviderTimeout) as error:
        bolna.create_agent({})
    assert str(error.value) == "malformed"
    bolna, _ = client_for(lambda r: httpx.Response(200, json={"status": "queued"}))
    with pytest.raises(ProviderTimeout):
        bolna.start_outbound_call(AGENT, "+910000000002")


def test_client_rejects_path_injection():
    bolna, recorder = client_for(lambda r: httpx.Response(200, json={}))
    for bad in ("../phone-numbers/all", "a/b", "", "x" * 65):
        with pytest.raises(ValueError):
            bolna.get_execution(bad)
        with pytest.raises(ValueError):
            bolna.update_agent(bad, {})
    assert recorder.requests == []
    with pytest.raises(ValueError):
        BolnaClient("", httpx.Client())
    with pytest.raises(ValueError):
        bolna.start_outbound_call(AGENT, "+910000000002", scheduled_at=datetime(2031, 3, 4, 10, 0))


def test_fake_bolna_is_deterministic():
    def run():
        fake = FakeBolna([PhoneNumber("n1", "+910000000009", None, "plivo")])
        agent = fake.create_agent({"agent_config": {}})
        fake.update_agent(agent, {"agent_config": {"v": 2}})
        fake.assign_phone_number("n1", agent)
        scheduled = fake.start_outbound_call(agent, "+910000000002", scheduled_at=NOW)
        fake.stop_scheduled_call(scheduled)
        return fake, agent, scheduled

    a, agent, scheduled = run()
    b, *_ = run()
    assert [c[0] for c in a.calls] == [c[0] for c in b.calls]
    assert a.agents == b.agents and a.executions == b.executions
    assert a.list_phone_numbers()[0].agent_id == agent
    assert a.get_execution(scheduled).status == "stopped"
    with pytest.raises(ProviderPermanent):
        a.stop_scheduled_call(scheduled)
    with pytest.raises(ProviderPermanent):
        a.update_agent("unknown", {})


# Review fixes: template injection, replay wording, per-session cap


def render(tool_name, values):
    """Render a compiled tool's param template the way a naive %(name)s substitution would:
    no JSON escaping of the substituted values."""
    params = compiled(agent_config.REDACTED)["agent_config"]["tasks"][0]["tools_config"][
        "api_tools"
    ]["tools_params"][tool_name]
    return params["param"] % values


def test_quote_injected_duplicate_key_rejected(env):
    injected = f'Demo Doctor", "execution_id": "{OTHER_EXECUTION}'
    raw = render(
        "check_slots",
        {
            "doctor": injected,
            "service": "Consultation",
            "day": str(DAY),
            "execution_id": EXECUTION,
            "from_number": CALLER,
            "agent_id": AGENT,
        },
    )
    assert raw.count('"execution_id"') == 2  # The injection really produced a duplicate key.
    for path in (
        "/v1/bolna/tools/check-slots",
        "/v1/bolna/tools/reserve-slot",
        "/v1/bolna/tools/appointment-status",
        "/v1/bolna/context",
    ):
        r = env["client"].post(
            path,
            content=raw,
            headers={**auth(env["token"]), "Content-Type": "application/json"},
        )
        assert r.status_code == 422
        assert r.json() == {"detail": "Duplicate JSON keys are not allowed"}
        assert OTHER_EXECUTION not in r.text and CALLER not in r.text and "Demo" not in r.text
    nested = '{"execution_id": "' + EXECUTION + '", "x": {"a": 1, "a": 2}}'
    r = env["client"].post(
        "/v1/bolna/context",
        content=nested,
        headers={**auth(env["token"]), "Content-Type": "application/json"},
    )
    assert r.status_code == 422
    assert sessions(env["url"]) == []
    # A well-formed rendering of the same template is accepted.
    clean = render(
        "check_slots",
        {
            "doctor": "Demo Doctor",
            "service": "Consultation",
            "day": str(DAY),
            "execution_id": EXECUTION,
            "from_number": CALLER,
            "agent_id": AGENT,
        },
    )
    r = env["client"].post(
        "/v1/bolna/tools/check-slots",
        content=clean,
        headers={**auth(env["token"]), "Content-Type": "application/json"},
    )
    assert r.status_code == 200


def test_replay_speaks_current_status(env):
    first = post(env, "/v1/bolna/tools/reserve-slot", reserve_body()).json()
    assert first["spoken"] == service.spoken_status("pending_sync")
    for status in ("cancelled", "needs_review", "confirmed"):
        with connect(env["url"]) as conn:
            conn.execute("UPDATE appointments SET status=%s", (status,))
        again = post(env, "/v1/bolna/tools/reserve-slot", reserve_body())
        assert again.status_code == 202
        assert again.json()["status"] == status
        assert again.json()["spoken"] == service.spoken_status(status)
        assert again.json()["appointment_id"] == first["appointment_id"]


def test_session_cap(env):
    path = "/v1/bolna/tools/reserve-slot"
    assert gateway.MAX_SESSION_ACTIVE == 3
    made = [
        post(env, path, reserve_body(starts_at=SLOT + timedelta(minutes=15 * i))) for i in range(3)
    ]
    assert [r.status_code for r in made] == [202, 202, 202]
    fourth = post(env, path, reserve_body(starts_at=SLOT + timedelta(hours=2)))
    assert fourth.status_code == 409
    assert fourth.json() == {"detail": service.SESSION_LIMIT_MESSAGE}
    # Replays are never capped.
    assert post(env, path, reserve_body()).json() == made[0].json()
    # Another call session has its own cap.
    other = post(
        env, path, reserve_body(execution=OTHER_EXECUTION, starts_at=SLOT + timedelta(hours=2))
    )
    assert other.status_code == 202
    # Only pending_sync/confirmed count: a cancelled one frees a place.
    with connect(env["url"]) as conn:
        conn.execute(
            "UPDATE appointments SET status='cancelled' WHERE id=%s",
            (made[1].json()["appointment_id"],),
        )
    assert post(env, path, reserve_body(starts_at=SLOT + timedelta(hours=3))).status_code == 202
    with connect(env["url"]) as conn:
        count = conn.execute("SELECT count(*) AS n FROM appointments").fetchone()["n"]
    assert count == 5


def test_session_cap_holds_under_concurrency(env, monkeypatch):
    monkeypatch.setattr(gateway, "MAX_SESSION_ACTIVE", 1)
    post(
        env,
        "/v1/bolna/tools/check-slots",
        call(doctor="Demo Doctor", service="Consultation", day=str(DAY)),
    )
    barrier = Barrier(3)
    client = env["client"]

    def run(i):
        barrier.wait()
        body = reserve_body(starts_at=SLOT + timedelta(minutes=15 * i))
        return client.post(
            "/v1/bolna/tools/reserve-slot", json=body, headers=auth(env["token"])
        ).status_code

    with ThreadPoolExecutor(max_workers=3) as pool:
        outcomes = sorted(pool.map(run, range(3)))
    assert outcomes == [202, 409, 409]
    with connect(env["url"]) as conn:
        assert conn.execute("SELECT count(*) AS n FROM appointments").fetchone()["n"] == 1
