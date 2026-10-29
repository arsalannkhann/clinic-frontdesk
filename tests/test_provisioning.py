"""Clinic provisioning state machine (T10).

Synthetic data only: generated credentials, a "+910000000999" clinic line on a FakeBolna
account and example.test URLs. FakeCalendar and FakeBolna stand in for every provider;
BolnaClient is exercised through httpx.MockTransport only. Nothing reaches the network.
"""

import base64
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from clinic_voice import cli, credentials, staff
from clinic_voice import provisioning as service
from clinic_voice.api import app
from clinic_voice.bolna import gateway
from clinic_voice.cli import DEMO_CLINIC_ID as CLINIC
from clinic_voice.config import get_settings
from clinic_voice.db import connect
from clinic_voice.outbox import Ports, run_once
from clinic_voice.providers.bolna import (
    UNREADABLE,
    BolnaClient,
    FakeBolna,
    PhoneNumber,
    summarize_agent,
)
from clinic_voice.providers.calendar import (
    FakeCalendar,
    ProviderPermanent,
    ProviderRetryable,
    ProviderTimeout,
)

PASSWORD = "synthetic-password-01"
GATEWAY = "https://voice.example.test"
LINE = "+910000000999"
NUMBER_ID = "pn-synthetic-1"
OTHER_CLINIC = UUID("00000000-0000-4000-8000-0000000000c2")
BASE = "/v1/admin/provisioning"
EMERGENCY = "If this is a medical emergency, please call local emergency services now."
HANDOFF = "I will connect you with a member of the clinic team."
KEYS = "k1:" + base64.b64encode(b"\x04" * 32).decode()
HASH_KEY = base64.b64encode(b"\x05" * 32).decode()


@pytest.fixture(autouse=True)
def cheap_hashing(monkeypatch):
    monkeypatch.setattr(staff, "SCRYPT_N", 2**10)


@pytest.fixture
def env(database, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database)
    monkeypatch.setenv("BOLNA_GATEWAY_BASE_URL", GATEWAY)
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEYS", KEYS)
    monkeypatch.setenv("PHONE_HASH_KEY", HASH_KEY)
    get_settings.cache_clear()
    with connect(database) as conn:
        conn.execute("INSERT INTO clinics (id,name) VALUES (%s,'Other Synthetic')", (OTHER_CLINIC,))
    fake = FakeBolna([PhoneNumber(NUMBER_ID, LINE, None, "plivo")])
    with TestClient(app) as client:
        yield {"client": client, "url": database, "fake": fake}
    get_settings.cache_clear()


def headers(env, role="owner", clinic=CLINIC):
    email = f"{role}-{uuid4().hex[:8]}@example.test"
    with connect(env["url"]) as conn:
        user = staff.create_user(conn, clinic, email, role, PASSWORD)
    r = env["client"].post("/v1/staff/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"], "_id": str(user["id"])}


def h(value):
    return {k: v for k, v in value.items() if not k.startswith("_")}


def rows(url, sql, params=()):
    with connect(url) as conn:
        return conn.execute(sql, params).fetchall()


def execute(url, sql, params=()):
    with connect(url) as conn:
        conn.execute(sql, params)


def connect_calendar(env, clinic=CLINIC):
    with connect(env["url"]) as conn:
        user = staff.create_user(
            conn, clinic, f"cal-{uuid4().hex[:8]}@example.test", "owner", PASSWORD
        )
        conn.execute(
            "INSERT INTO calendar_accounts (id,clinic_id,provider,refresh_token_ciphertext,key_id,"
            "status,connected_by) VALUES (%s,%s,'google',%s,'k1','active',%s)",
            (uuid4(), clinic, b"\x00" * 40, user["id"]),
        )
        conn.execute(
            "UPDATE doctors SET booking_calendar_id='booking@example.test' WHERE clinic_id=%s",
            (clinic,),
        )


def approve_scripts(env, owner):
    r = env["client"].patch(
        "/v1/admin/clinic",
        headers=h(owner),
        json={"emergency_text": EMERGENCY, "handoff_text": HANDOFF, "scripts_approved": True},
    )
    assert r.status_code == 200, r.text


def state(env, auth):
    r = env["client"].get(BASE, headers=h(auth))
    assert r.status_code == 200, r.text
    return r.json()


def advance(env, auth):
    return env["client"].post(f"{BASE}/advance", headers=h(auth))


def work(env, bolna=None):
    """Make pending provision.step jobs due, then run one worker batch."""
    execute(
        env["url"],
        "UPDATE outbox SET available_at=now() WHERE kind='provision.step' AND status='pending'",
    )
    return run_once(env["url"], "w1", Ports(calendar=FakeCalendar(), bolna=bolna or env["fake"]))


def step(env, auth, bolna=None):
    r = advance(env, auth)
    assert r.status_code == 202, r.text
    work(env, bolna)
    return state(env, auth)


def select(env, auth, number_id=NUMBER_ID):
    return env["client"].post(
        f"{BASE}/number", headers=h(auth), json={"phone_number_id": number_id}
    )


def to_state(env, auth, target):
    order = ["draft", "calendar_connected", "agent_created", "number_assigned", "verified"]
    current = state(env, auth)["state"]
    while order.index(current) < order.index(target):
        if current == "agent_created" and state(env, auth)["phone_number_id"] is None:
            assert select(env, auth).status_code == 200
        current = step(env, auth)["state"]
    assert current == target
    return state(env, auth)


def calls(fake, name):
    return [c for c in fake.calls if c[0] == name]


def jobs(env):
    return rows(
        env["url"], "SELECT * FROM outbox WHERE kind='provision.step' ORDER BY created_at, id"
    )


def agent_token(fake, agent_id):
    params = fake.agents[agent_id]["agent_config"]["tasks"][0]["tools_config"]["api_tools"][
        "tools_params"
    ]
    tokens = {entry["api_token"] for entry in params.values()}
    assert len(tokens) == 1
    return tokens.pop().removeprefix("Bearer ")


# Happy path


def test_full_provisioning_advances_one_step_per_job(env):
    owner = headers(env)
    connect_calendar(env)
    assert state(env, owner)["state"] == "draft"
    assert step(env, owner)["state"] == "calendar_connected"
    created = step(env, owner)
    assert created["state"] == "agent_created"
    agent_id = created["agent_id"]
    assert created["agent_name"] == f"clinic-{CLINIC.hex}-v1"
    assert (
        rows(env["url"], "SELECT bolna_agent_id FROM clinics WHERE id=%s", (CLINIC,))[0][
            "bolna_agent_id"
        ]
        == agent_id
    )
    # The agent carries a fresh tool credential of this clinic; only its id is recorded.
    token = agent_token(env["fake"], agent_id)
    with connect(env["url"]) as conn:
        assert credentials.authenticate(conn, token) == CLINIC
    assert credentials.parse(token)[0] == UUID(created["tool_credential_id"])
    assert env["fake"].agents[agent_id]["agent_config"]["agent_name"] == created["agent_name"]

    assert select(env, owner).status_code == 200
    assert step(env, owner)["state"] == "number_assigned"
    assert env["fake"].list_phone_numbers()[0].agent_id == agent_id
    verified = step(env, owner)
    assert verified["state"] == "verified"
    assert [e["outcome"] for e in verified["events"]][:2] == ["ok", "ok"]
    assert len(calls(env["fake"], "create_agent")) == 1
    assert not calls(env["fake"], "start_outbound_call")
    steps = [(e["from_state"], e["to_state"], e["step"]) for e in reversed(verified["events"])]
    assert steps == [
        ("draft", "calendar_connected", "calendar"),
        ("calendar_connected", "agent_created", "agent"),
        ("agent_created", "agent_created", "select_number"),
        ("agent_created", "number_assigned", "number"),
        ("number_assigned", "verified", "verify"),
    ]
    assert all(job["status"] == "done" for job in jobs(env))


def test_calendar_step_refuses_without_a_mapped_active_calendar(env):
    owner = headers(env)
    refused = step(env, owner)
    assert (refused["state"], refused["last_error"]) == ("draft", "calendar_not_connected")
    # A mapped doctor alone is not enough: the account must be active.
    connect_calendar(env)
    execute(env["url"], "UPDATE calendar_accounts SET status='error'")
    assert step(env, owner)["state"] == "draft"
    execute(env["url"], "UPDATE calendar_accounts SET status='active'")
    connected = step(env, owner)
    assert (connected["state"], connected["last_error"]) == ("calendar_connected", None)


# Agent creation: idempotency and ambiguity


def test_timeout_after_create_reconciles_to_exactly_one_agent(env):
    owner = headers(env)
    connect_calendar(env)
    to_state(env, owner, "calendar_connected")
    env["fake"].timeout_after_write.add("create_agent")
    after_timeout = step(env, owner)
    assert after_timeout["state"] == "calendar_connected"
    assert len(env["fake"].agents) == 1  # The agent exists; the client saw a timeout.
    [job] = jobs(env)[-1:]
    assert (job["status"], job["last_error"]) == ("pending", "timeout")
    pending = rows(env["url"], "SELECT * FROM provisioning WHERE clinic_id=%s", (CLINIC,))[0]
    assert pending["agent_create_pending"] is True and pending["agent_id"] is None
    first_credential = pending["attempt_credential_id"]
    assert pending["tool_credential_id"] is None  # Not acknowledged yet.

    work(env)  # Retry: finds the agent by its deterministic name and updates it.
    done = state(env, owner)
    assert done["state"] == "agent_created"
    assert done["events"][0]["outcome"] == "reconciled"
    assert len(env["fake"].agents) == 1
    assert len(calls(env["fake"], "create_agent")) == 1
    assert len(calls(env["fake"], "update_agent")) == 1
    [agent_id] = env["fake"].agents
    assert done["agent_id"] == agent_id
    # The credential of the timed-out attempt is revoked; the agent holds the new one.
    revoked = rows(
        env["url"], "SELECT revoked_at FROM tenant_credentials WHERE id=%s", (first_credential,)
    )[0]["revoked_at"]
    assert revoked is not None
    with connect(env["url"]) as conn:
        assert credentials.authenticate(conn, agent_token(env["fake"], agent_id)) == CLINIC
    assert (
        rows(env["url"], "SELECT agent_create_pending FROM provisioning")[0]["agent_create_pending"]
        is False
    )


def test_retry_with_stored_agent_id_updates_instead_of_creating(env):
    owner = headers(env)
    connect_calendar(env)
    to_state(env, owner, "calendar_connected")
    env["fake"].fail_next("update_agent", 1, ProviderTimeout)  # Not reached on the first run.
    created = step(env, owner)
    # Simulate a reset after review with the agent id kept.
    execute(env["url"], "UPDATE provisioning SET state='calendar_connected', version=version+1")
    r = advance(env, owner)
    assert r.status_code == 202
    work(env)  # update_agent times out: retried.
    work(env)
    again = state(env, owner)
    assert again["state"] == "agent_created" and again["agent_id"] == created["agent_id"]
    assert len(calls(env["fake"], "create_agent")) == 1
    assert len(env["fake"].agents) == 1


def test_uncertain_create_without_consistent_lookup_goes_to_manual_review(env):
    owner = headers(env)
    connect_calendar(env)
    to_state(env, owner, "calendar_connected")
    env["fake"].consistent_agent_lookup = False  # Like BolnaClient: listing may lag.
    env["fake"].fail_next("create_agent", 1, ProviderTimeout)  # Nothing created in fact.
    step(env, owner)
    work(env)
    review = state(env, owner)
    assert (review["state"], review["last_error"]) == ("manual_review", "create_uncertain")
    assert len(calls(env["fake"], "create_agent")) == 1 and env["fake"].agents == {}
    # Advance is refused until the owner resets (after checking the Bolna account).
    assert advance(env, owner).status_code == 409
    held = provisioning_row(env)["attempt_credential_id"]
    # A plain reset keeps the create intent: the next step refuses to create again.
    r = env["client"].post(f"{BASE}/reset", headers=h(owner))
    assert r.status_code == 200 and r.json()["state"] == "calendar_connected"
    assert r.json()["agent_create_pending"] is True
    review = step(env, owner)
    assert (review["state"], review["last_error"]) == ("manual_review", "create_uncertain")
    assert len(calls(env["fake"], "create_agent")) == 1
    # The owner confirms no stray agent exists: the intent and its credential are cleared.
    r = env["client"].post(f"{BASE}/reset", headers=h(owner), json={"confirm_no_stray_agent": True})
    assert r.status_code == 200 and r.json()["agent_create_pending"] is False
    assert provisioning_row(env)["attempt_credential_id"] is None
    assert rows(env["url"], "SELECT revoked_at FROM tenant_credentials WHERE id=%s", (held,))[0][
        "revoked_at"
    ]
    assert step(env, owner)["state"] == "agent_created"
    assert len(env["fake"].agents) == 1
    actions = [a["action"] for a in rows(env["url"], "SELECT action FROM audit_events")]
    assert "provisioning.reset" in actions and "provisioning.reset_confirmed" in actions


def test_confirming_no_stray_agent_needs_an_uncertain_create(env):
    owner = headers(env)
    connect_calendar(env)
    to_state(env, owner, "calendar_connected")
    env["fake"].fail_next("create_agent", 1, ProviderPermanent)
    step(env, owner)
    assert state(env, owner)["state"] == "failed"
    r = env["client"].post(f"{BASE}/reset", headers=h(owner), json={"confirm_no_stray_agent": True})
    assert r.status_code == 409
    assert env["client"].post(f"{BASE}/reset", headers=h(owner), json={}).status_code == 200


def test_uncertain_create_without_lookup_goes_to_manual_review(env):
    class NoListing(FakeBolna):
        list_agents = None

    bolna = NoListing()
    owner = headers(env)
    connect_calendar(env)
    to_state(env, owner, "calendar_connected")
    bolna.timeout_after_write.add("create_agent")
    step(env, owner, bolna)
    work(env, bolna)
    review = state(env, owner)
    assert (review["state"], review["last_error"]) == ("manual_review", "lookup_unavailable")
    assert len(calls(bolna, "create_agent")) == 1


def test_two_agents_with_the_clinic_name_are_ambiguous(env):
    owner = headers(env)
    connect_calendar(env)
    to_state(env, owner, "calendar_connected")
    name = service.agent_name(CLINIC)
    for _ in range(2):
        env["fake"].create_agent({"agent_config": {"agent_name": name, "tasks": []}})
    review = step(env, owner)
    assert (review["state"], review["last_error"]) == ("manual_review", "agent_ambiguous")
    assert len(env["fake"].agents) == 2


def test_hand_configured_agent_is_never_replaced(env):
    owner = headers(env)
    connect_calendar(env)
    to_state(env, owner, "calendar_connected")
    execute(
        env["url"],
        "UPDATE clinics SET bolna_agent_id='33333333-3333-4333-8333-333333333333' WHERE id=%s",
        (CLINIC,),
    )
    review = step(env, owner)
    assert (review["state"], review["last_error"]) == ("manual_review", "agent_preconfigured")
    assert not calls(env["fake"], "create_agent")


CONFLICT_AGENT_SQL = {
    "own_outbound": "UPDATE clinics SET bolna_outbound_agent_id=%(a)s WHERE id=%(c)s",
    "other_inbound": "UPDATE clinics SET bolna_agent_id=%(a)s WHERE id=%(o)s",
    "other_outbound": "UPDATE clinics SET bolna_outbound_agent_id=%(a)s WHERE id=%(o)s",
    "other_provisioning": "INSERT INTO provisioning (clinic_id,state,resume_state,agent_id)"
    " VALUES (%(o)s,'agent_created','agent_created',%(a)s)",
}


@pytest.mark.parametrize("owner_of", list(CONFLICT_AGENT_SQL))
def test_found_agent_owned_elsewhere_is_never_written(env, owner_of):
    owner = headers(env)
    connect_calendar(env)
    to_state(env, owner, "calendar_connected")
    # An agent with this clinic's deterministic name that belongs elsewhere.
    found = env["fake"].create_agent(
        {"agent_config": {"agent_name": service.agent_name(CLINIC), "tasks": []}}
    )
    execute(env["url"], CONFLICT_AGENT_SQL[owner_of], {"a": found, "c": CLINIC, "o": OTHER_CLINIC})
    review = step(env, owner)
    assert (review["state"], review["last_error"]) == ("manual_review", "agent_conflict")
    assert not calls(env["fake"], "update_agent")
    assert len(calls(env["fake"], "create_agent")) == 1  # Only the setup's own create.
    assert provisioning_credentials(env) == []  # Refused before any credential was issued.
    assert review["agent_id"] is None


def test_created_agent_claimed_elsewhere_is_reviewed_and_its_credential_revoked(env):
    owner = headers(env)
    connect_calendar(env)
    to_state(env, owner, "calendar_connected")
    fake = env["fake"]
    original = fake.create_agent

    def create_then_claimed(payload):
        agent_id = original(payload)
        execute(
            env["url"],
            "UPDATE clinics SET bolna_agent_id=%s WHERE id=%s",
            (agent_id, OTHER_CLINIC),
        )
        return agent_id

    fake.create_agent = create_then_claimed
    review = step(env, owner)
    assert (review["state"], review["last_error"]) == ("manual_review", "agent_conflict")
    assert review["agent_id"] is None and review["tool_credential_id"] is None
    assert all(c["revoked_at"] is not None for c in provisioning_credentials(env))
    assert rows(env["url"], "SELECT bolna_agent_id FROM clinics WHERE id=%s", (CLINIC,)) == [
        {"bolna_agent_id": None}
    ]


def test_concurrent_set_agent_is_not_overwritten(env):
    owner = headers(env)
    connect_calendar(env)
    to_state(env, owner, "calendar_connected")
    fake = env["fake"]
    original = fake.create_agent
    hand_set = "55555555-5555-4555-8555-555555555555"

    def create_while_set_agent_runs(payload):
        agent_id = original(payload)
        execute(env["url"], "UPDATE clinics SET bolna_agent_id=%s WHERE id=%s", (hand_set, CLINIC))
        return agent_id

    fake.create_agent = create_while_set_agent_runs
    review = step(env, owner)
    assert (review["state"], review["last_error"]) == ("manual_review", "agent_conflict")
    assert review["agent_id"] is None
    assert rows(env["url"], "SELECT bolna_agent_id FROM clinics WHERE id=%s", (CLINIC,)) == [
        {"bolna_agent_id": hand_set}
    ]


class Unauthorized(ProviderPermanent):
    def __init__(self):
        super().__init__("unauthorized")


@pytest.mark.parametrize(
    ("error", "code"), [(ProviderPermanent, "provider_permanent"), (Unauthorized, "unauthorized")]
)
def test_permanent_create_error_fails_and_reset_returns_to_the_step(env, error, code):
    owner = headers(env)
    connect_calendar(env)
    to_state(env, owner, "calendar_connected")
    env["fake"].fail_next("create_agent", 1, error)
    failed = step(env, owner)
    assert (failed["state"], failed["last_error"], failed["resume_state"]) == (
        "failed",
        code,
        "calendar_connected",
    )
    assert (
        rows(env["url"], "SELECT agent_create_pending FROM provisioning")[0]["agent_create_pending"]
        is False
    )
    assert jobs(env)[-1]["status"] == "done"
    r = env["client"].post(f"{BASE}/reset", headers=h(owner))
    assert r.json()["state"] == "calendar_connected"
    assert step(env, owner)["state"] == "agent_created"


def test_missing_gateway_url_fails(env, monkeypatch):
    owner = headers(env)
    connect_calendar(env)
    to_state(env, owner, "calendar_connected")
    monkeypatch.setenv("BOLNA_GATEWAY_BASE_URL", "")
    get_settings.cache_clear()
    failed = step(env, owner)
    assert (failed["state"], failed["last_error"]) == ("failed", "gateway_unconfigured")
    assert not calls(env["fake"], "create_agent")


def test_refusals_do_not_count_against_the_attempt_budget(env):
    owner = headers(env)
    for _ in range(service.STEP_MAX_ATTEMPTS + 2):
        assert step(env, owner)["last_error"] == "calendar_not_connected"
    connect_calendar(env)
    to_state(env, owner, "agent_created")
    assert select(env, owner, "pn-not-ours").status_code == 200
    for _ in range(service.STEP_MAX_ATTEMPTS + 2):
        refused = step(env, owner)
        assert (refused["state"], refused["last_error"]) == (
            "agent_created",
            "number_not_in_account",
        )
    assert refused["attempts"]["number"] == 0 and refused["attempts"]["calendar"] == 0
    assert select(env, owner).status_code == 200
    assert step(env, owner)["state"] == "number_assigned"
    assert state(env, owner)["attempts"]["number"] == 1  # The assignment write.


def provisioning_row(env, clinic=CLINIC):
    return rows(env["url"], "SELECT * FROM provisioning WHERE clinic_id=%s", (clinic,))[0]


def provisioning_credentials(env):
    return rows(
        env["url"],
        "SELECT id, revoked_at FROM tenant_credentials WHERE label=%s ORDER BY created_at",
        (service.CREDENTIAL_LABEL,),
    )


def test_credentials_of_failed_attempts_are_revoked(env):
    owner = headers(env)
    connect_calendar(env)
    to_state(env, owner, "calendar_connected")
    env["fake"].fail_next("create_agent", 1, ProviderPermanent)
    assert step(env, owner)["state"] == "failed"
    [issued] = provisioning_credentials(env)
    assert issued["revoked_at"] is not None  # Bolna refused the write: nobody holds it.
    row = rows(env["url"], "SELECT * FROM provisioning")[0]
    assert (row["tool_credential_id"], row["attempt_credential_id"]) == (None, None)


def test_possibly_held_credential_survives_review_until_reconciled(env):
    owner = headers(env)
    connect_calendar(env)
    to_state(env, owner, "calendar_connected")
    env["fake"].consistent_agent_lookup = False
    env["fake"].timeout_after_write.add("create_agent")  # Created; the client saw a timeout.
    env["fake"]._failures["list_agents"] = []  # noqa: SLF001
    step(env, owner)
    env["fake"].fail_next("list_agents", 50, ProviderRetryable)
    for _ in range(service.STEP_MAX_ATTEMPTS + 1):
        work(env)
    review = state(env, owner)
    assert (review["state"], review["last_error"]) == ("manual_review", "retries_exhausted")
    [held] = provisioning_credentials(env)
    assert held["revoked_at"] is None  # The created agent may hold it.
    [agent_id] = env["fake"].agents
    with connect(env["url"]) as conn:
        assert credentials.authenticate(conn, agent_token(env["fake"], agent_id)) == CLINIC
    # Reset, then the lookup succeeds: the agent is updated with a new credential and the
    # held one is revoked.
    env["fake"]._failures["list_agents"] = []  # noqa: SLF001
    env["client"].post(f"{BASE}/reset", headers=h(owner))
    assert step(env, owner)["state"] == "agent_created"
    first, second = provisioning_credentials(env)
    assert first["id"] == held["id"] and first["revoked_at"] is not None
    assert second["revoked_at"] is None
    assert state(env, owner)["tool_credential_id"] == str(second["id"])


def test_repeated_retryable_errors_end_in_manual_review(env):
    owner = headers(env)
    connect_calendar(env)
    to_state(env, owner, "calendar_connected")
    env["fake"].fail_next("list_agents", 50, ProviderRetryable)
    step(env, owner)
    for _ in range(service.STEP_MAX_ATTEMPTS + 1):
        work(env)
    review = state(env, owner)
    assert (review["state"], review["last_error"]) == ("manual_review", "retries_exhausted")
    assert review["attempts"]["agent"] == service.STEP_MAX_ATTEMPTS
    assert not calls(env["fake"], "create_agent")
    assert not [j for j in jobs(env) if j["status"] in ("pending", "processing")]


def test_dead_lettered_step_moves_to_manual_review(env):
    owner = headers(env)
    connect_calendar(env)
    to_state(env, owner, "calendar_connected")
    env["fake"].fail_next("list_agents", 1, ProviderTimeout)
    assert advance(env, owner).status_code == 202
    execute(
        env["url"],
        "UPDATE outbox SET max_attempts=1 WHERE kind='provision.step' AND status='pending'",
    )
    work(env)
    review = state(env, owner)
    assert (review["state"], review["last_error"]) == ("manual_review", "retries_exhausted")
    assert jobs(env)[-1]["status"] == "dead"
    assert review["events"][0]["to_state"] == "manual_review"
    admin = headers(env, "admin")
    r = env["client"].post(f"/v1/admin/outbox/{jobs(env)[-1]['id']}/requeue", headers=h(admin))
    assert r.status_code == 409


def test_lost_lease_dead_letter_is_lease_lost_and_allows_confirming_no_stray_agent(env):
    owner = headers(env)
    connect_calendar(env)
    to_state(env, owner, "calendar_connected")
    assert advance(env, owner).status_code == 202
    # A worker died mid-create: write intent recorded, job processing with its lease gone.
    execute(
        env["url"],
        "UPDATE provisioning SET agent_create_pending=true WHERE clinic_id=%s",
        (CLINIC,),
    )
    execute(
        env["url"],
        "UPDATE outbox SET status='processing', attempts=max_attempts, lease_owner='dead',"
        " lease_expires_at=now() - interval '1 second'"
        " WHERE kind='provision.step' AND status='pending'",
    )
    work(env)
    review = state(env, owner)
    assert (review["state"], review["last_error"]) == ("manual_review", "lease_lost")
    assert jobs(env)[-1]["status"] == "dead"
    r = env["client"].post(f"{BASE}/reset", headers=h(owner), json={"confirm_no_stray_agent": True})
    assert r.status_code == 200 and r.json()["agent_create_pending"] is False


# Numbers


def test_number_not_in_the_account_is_refused(env):
    owner = headers(env)
    connect_calendar(env)
    to_state(env, owner, "agent_created")
    assert select(env, owner, "pn-not-ours").status_code == 200
    refused = step(env, owner)
    assert (refused["state"], refused["last_error"]) == ("agent_created", "number_not_in_account")
    assert not calls(env["fake"], "assign_phone_number")
    assert not calls(env["fake"], "buy_phone_number")


def test_number_routed_to_another_agent_is_refused(env):
    env["fake"].phone_numbers.append(
        PhoneNumber("pn-busy", "+910000000998", "44444444-4444-4444-8444-444444444444", "plivo")
    )
    owner = headers(env)
    connect_calendar(env)
    to_state(env, owner, "agent_created")
    assert select(env, owner, "pn-busy").status_code == 200
    refused = step(env, owner)
    assert (refused["state"], refused["last_error"]) == ("agent_created", "number_in_use")
    assert not calls(env["fake"], "assign_phone_number")


def test_assignment_timeout_is_retried_and_recognized(env):
    owner = headers(env)
    connect_calendar(env)
    to_state(env, owner, "agent_created")
    select(env, owner)
    env["fake"].timeout_after_write.add("assign_phone_number")
    assert step(env, owner)["state"] == "agent_created"
    work(env)
    assert state(env, owner)["state"] == "number_assigned"
    assert len(calls(env["fake"], "assign_phone_number")) == 1


def test_number_is_unique_across_clinics_and_validated(env):
    owner = headers(env)
    other = headers(env, clinic=OTHER_CLINIC)
    assert select(env, owner).status_code == 200
    r = select(env, other)
    assert (r.status_code, r.json()["detail"]) == (409, "That number cannot be selected")
    for bad in ["../x", "a" * 65, "+910000000999", ""]:
        assert select(env, other, bad).status_code == 422


def test_number_cannot_change_once_assigned_or_while_a_step_is_pending(env):
    owner = headers(env)
    connect_calendar(env)
    assert advance(env, owner).status_code == 202
    assert select(env, owner).status_code == 409  # Step pending.
    work(env)
    to_state(env, owner, "number_assigned")
    assert select(env, owner, "pn-other").status_code == 409


# Verification


def test_verify_detects_a_changed_agent_and_reset_repairs_it(env):
    owner = headers(env)
    connect_calendar(env)
    assigned = to_state(env, owner, "number_assigned")
    agent_id = assigned["agent_id"]
    tasks = env["fake"].agents[agent_id]["agent_config"]["tasks"]
    tasks[0]["tools_config"]["api_tools"]["tools_params"]["check_slots"]["url"] = (
        "https://evil.example.test/x"
    )
    review = step(env, owner)
    assert (review["state"], review["last_error"], review["resume_state"]) == (
        "manual_review",
        "tools_mismatch",
        "calendar_connected",
    )
    assert env["client"].post(f"{BASE}/reset", headers=h(owner)).json()["state"] == (
        "calendar_connected"
    )
    assert step(env, owner)["state"] == "agent_created"  # get_agent by id, then update.
    assert step(env, owner)["state"] == "number_assigned"  # Already routed: no new assign.
    assert len(calls(env["fake"], "assign_phone_number")) == 1
    assert step(env, owner)["state"] == "verified"
    assert len(env["fake"].agents) == 1


def test_verify_detects_an_unrouted_number(env):
    owner = headers(env)
    connect_calendar(env)
    to_state(env, owner, "number_assigned")
    env["fake"].phone_numbers[0] = PhoneNumber(NUMBER_ID, LINE, None, "plivo")
    review = step(env, owner)
    assert (review["state"], review["last_error"], review["resume_state"]) == (
        "manual_review",
        "number_mismatch",
        "agent_created",
    )


def test_verify_detects_a_foreign_credential(env):
    owner = headers(env)
    connect_calendar(env)
    assigned = to_state(env, owner, "number_assigned")
    with connect(env["url"]) as conn:
        _, other = credentials.issue(conn, CLINIC, "elsewhere")
    params = env["fake"].agents[assigned["agent_id"]]["agent_config"]["tasks"][0]["tools_config"][
        "api_tools"
    ]["tools_params"]
    params["clinic_context"]["api_token"] = f"Bearer {other}"
    review = step(env, owner)
    assert (review["state"], review["last_error"]) == ("manual_review", "credential_mismatch")


@pytest.mark.parametrize(
    ("token", "outcome"),
    [("Bearer not-a-tool-token", "credential_mismatch"), ("Bearer ********", None)],
)
def test_verify_fails_an_unreadable_token_and_flags_only_a_true_mask(env, token, outcome):
    owner = headers(env)
    connect_calendar(env)
    assigned = to_state(env, owner, "number_assigned")
    params = env["fake"].agents[assigned["agent_id"]]["agent_config"]["tasks"][0]["tools_config"][
        "api_tools"
    ]["tools_params"]
    for entry in params.values():
        entry["api_token"] = token
    view = step(env, owner)
    if outcome is None:
        assert view["state"] == "verified"
        assert view["events"][0]["outcome"] == "token_unchecked"
    else:
        assert (view["state"], view["last_error"]) == ("manual_review", outcome)


def test_verify_and_activate_require_an_active_tool_credential(env):
    owner = headers(env)
    connect_calendar(env)
    approve_scripts(env, owner)
    assigned = to_state(env, owner, "number_assigned")
    execute(
        env["url"],
        "UPDATE tenant_credentials SET revoked_at=now() WHERE id=%s",
        (assigned["tool_credential_id"],),
    )
    review = step(env, owner)
    assert (review["state"], review["last_error"], review["resume_state"]) == (
        "manual_review",
        "credential_inactive",
        "calendar_connected",
    )
    assert not calls(env["fake"], "get_agent")
    # Reset re-runs the agent step, which issues a fresh credential; then it verifies.
    assert env["client"].post(f"{BASE}/reset", headers=h(owner)).status_code == 200
    verified = to_state(env, owner, "verified")
    execute(
        env["url"],
        "UPDATE tenant_credentials SET expires_at=now() - interval '1 second' WHERE id=%s",
        (verified["tool_credential_id"],),
    )
    r = env["client"].post(f"{BASE}/activate", headers=h(owner))
    assert r.status_code == 409 and "credential" in r.json()["detail"]


# Activation


def test_activation_requires_every_prerequisite_and_does_not_enable_outbound(env):
    owner = headers(env)
    r = env["client"].post(f"{BASE}/activate", headers=h(owner))
    assert r.status_code == 409  # draft
    connect_calendar(env)
    to_state(env, owner, "verified")
    assert env["client"].post(f"{BASE}/activate", headers=h(owner)).status_code == 409  # scripts
    r = env["client"].patch(
        "/v1/admin/clinic",
        headers=h(owner),
        json={"emergency_text": EMERGENCY, "handoff_text": HANDOFF},
    )
    assert r.json()["scripts_approved"] is False
    assert env["client"].post(f"{BASE}/activate", headers=h(owner)).status_code == 409  # approval
    approve_scripts(env, owner)
    execute(env["url"], "UPDATE calendar_accounts SET status='error'")
    assert env["client"].post(f"{BASE}/activate", headers=h(owner)).status_code == 409  # calendar
    execute(env["url"], "UPDATE calendar_accounts SET status='active'")
    r = env["client"].post(f"{BASE}/activate", headers=h(owner))
    assert r.status_code == 200 and r.json()["state"] == "active"
    clinic = rows(env["url"], "SELECT outbound_enabled FROM clinics WHERE id=%s", (CLINIC,))[0]
    assert clinic["outbound_enabled"] is False
    assert not calls(env["fake"], "start_outbound_call")
    assert advance(env, owner).status_code == 409  # Nothing left to run.
    assert env["client"].post(f"{BASE}/reset", headers=h(owner)).status_code == 409
    assert "provisioning.activated" in [
        a["action"] for a in rows(env["url"], "SELECT action FROM audit_events")
    ]


def test_activation_refuses_a_changed_agent(env):
    owner = headers(env)
    connect_calendar(env)
    to_state(env, owner, "verified")
    approve_scripts(env, owner)
    execute(env["url"], "UPDATE clinics SET bolna_agent_id=NULL WHERE id=%s", (CLINIC,))
    assert env["client"].post(f"{BASE}/activate", headers=h(owner)).status_code == 409


def test_script_edits_clear_approval_and_empty_scripts_cannot_be_approved(env):
    owner = headers(env)
    admin = headers(env, "admin")
    r = env["client"].patch("/v1/admin/clinic", headers=h(owner), json={"scripts_approved": True})
    assert r.status_code == 422
    approve_scripts(env, owner)
    # An admin may edit a text (which clears the approval) but never approve.
    r = env["client"].patch("/v1/admin/clinic", headers=h(admin), json={"handoff_text": "Hold on."})
    assert (r.json()["handoff_text"], r.json()["scripts_approved"]) == ("Hold on.", False)
    for bad in ["{execution_id}", "50% off", "line\nbreak", "x" * 1001]:
        r = env["client"].patch("/v1/admin/clinic", headers=h(admin), json={"emergency_text": bad})
        assert r.status_code == 422
    viewer = headers(env, "viewer")
    r = env["client"].patch("/v1/admin/clinic", headers=h(viewer), json={"scripts_approved": True})
    assert r.status_code == 403


def test_only_the_owner_approves_scripts_or_enables_test_calls(env):
    owner = headers(env)
    admin = headers(env, "admin")
    texts = {"emergency_text": EMERGENCY, "handoff_text": HANDOFF}
    assert env["client"].patch("/v1/admin/clinic", headers=h(admin), json=texts).status_code == 200
    for field in ("scripts_approved", "test_calls_enabled"):
        r = env["client"].patch("/v1/admin/clinic", headers=h(admin), json={field: True})
        assert r.status_code == 403, field
    clinic = env["client"].get("/v1/admin/clinic", headers=h(admin)).json()
    assert (clinic["scripts_approved"], clinic["test_calls_enabled"]) == (False, False)
    r = env["client"].patch(
        "/v1/admin/clinic",
        headers=h(owner),
        json={"scripts_approved": True, "test_calls_enabled": True},
    )
    assert (r.json()["scripts_approved"], r.json()["test_calls_enabled"]) == (True, True)
    r = env["client"].patch(
        "/v1/admin/clinic", headers=h(owner), json={"test_calls_enabled": False}
    )
    assert r.json()["test_calls_enabled"] is False
    actions = [a["action"] for a in rows(env["url"], "SELECT action FROM audit_events")]
    assert {
        "clinic.scripts_approved",
        "clinic.test_calls_enabled",
        "clinic.test_calls_disabled",
    } <= set(actions)


# Roles, tenants, concurrency


def test_viewer_reads_and_only_the_owner_mutates(env):
    for role in ("viewer", "admin"):
        auth = headers(env, role)
        assert env["client"].get(BASE, headers=h(auth)).status_code == 200
        for path, body in [
            ("advance", None),
            ("number", {"phone_number_id": NUMBER_ID}),
            ("activate", None),
            ("reset", None),
        ]:
            r = env["client"].post(f"{BASE}/{path}", headers=h(auth), json=body)
            assert r.status_code == 403, (role, path)
    assert env["client"].get(BASE).status_code == 401
    assert not jobs(env)
    assert rows(env["url"], "SELECT * FROM provisioning") == []


def test_tenant_isolation(env):
    owner = headers(env)
    other = headers(env, clinic=OTHER_CLINIC)
    connect_calendar(env)
    mine = to_state(env, owner, "agent_created")
    theirs = state(env, other)
    assert (theirs["state"], theirs["agent_id"], theirs["events"]) == ("draft", None, [])
    # The other clinic's steps act on its own row only (it has no calendar: refused).
    refused = step(env, other)
    assert (refused["state"], refused["last_error"]) == ("draft", "calendar_not_connected")
    assert state(env, owner)["version"] == mine["version"]
    assert {j["clinic_id"] for j in jobs(env)} == {CLINIC, OTHER_CLINIC}
    assert (
        rows(env["url"], "SELECT bolna_agent_id FROM clinics WHERE id=%s", (OTHER_CLINIC,))[0][
            "bolna_agent_id"
        ]
        is None
    )
    assert select(env, other, NUMBER_ID).status_code == 200
    assert select(env, owner, NUMBER_ID).status_code == 409


def test_concurrent_advance_enqueues_one_step(env):
    owner = headers(env)
    connect_calendar(env)
    barrier = Barrier(4)

    def go(_):
        barrier.wait()
        return advance(env, owner).status_code

    with ThreadPoolExecutor(4) as pool:
        codes = sorted(pool.map(go, range(4)))
    assert codes == [202, 409, 409, 409]
    assert len(jobs(env)) == 1
    with pytest.raises(psycopg.errors.UniqueViolation), connect(env["url"]) as conn:
        conn.execute(
            "INSERT INTO outbox (id,clinic_id,kind,payload) VALUES (%s,%s,'provision.step','{}')",
            (uuid4(), CLINIC),
        )
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: work(env), range(2)))
    assert state(env, owner)["state"] == "calendar_connected"
    assert len([e for e in state(env, owner)["events"] if e["step"] == "calendar"]) == 1


def test_stale_job_does_not_step_twice(env):
    owner = headers(env)
    connect_calendar(env)
    to_state(env, owner, "calendar_connected")
    old_version = rows(env["url"], "SELECT version FROM provisioning")[0]["version"] - 1
    execute(
        env["url"],
        "INSERT INTO outbox (id,clinic_id,kind,payload) VALUES (%s,%s,'provision.step',%s)",
        (uuid4(), CLINIC, json.dumps({"version": old_version})),
    )
    before = state(env, owner)
    work(env)
    after = state(env, owner)
    assert (after["state"], after["version"]) == (before["state"], before["version"])
    assert env["fake"].calls == []
    assert jobs(env)[-1]["status"] == "done"


def test_worker_without_bolna_leaves_provisioning_jobs(env):
    owner = headers(env)
    connect_calendar(env)
    assert advance(env, owner).status_code == 202
    run_once(env["url"], "w1", Ports(calendar=FakeCalendar()))
    assert jobs(env)[0]["status"] == "pending"


# Secrets and append-only history


def test_no_credential_plaintext_or_phone_number_in_database_or_logs(env, caplog):
    caplog.set_level(logging.DEBUG)
    owner = headers(env)
    connect_calendar(env)
    env["fake"].timeout_after_write.add("create_agent")
    to_state(env, owner, "calendar_connected")
    step(env, owner)
    work(env)
    to_state(env, owner, "verified")
    approve_scripts(env, owner)
    env["client"].post(f"{BASE}/activate", headers=h(owner))
    token = agent_token(env["fake"], state(env, owner)["agent_id"])
    secret = token.split(".", 1)[1]
    tables = [
        r["tablename"]
        for r in rows(env["url"], "SELECT tablename FROM pg_tables WHERE schemaname='public'")
    ]
    dump = []
    for table in tables:
        dump += [
            r["row"]
            for r in rows(env["url"], f"SELECT row_to_json(t)::text AS row FROM {table} t")  # noqa: S608
        ]
    text = "\n".join(dump)
    assert secret not in text and LINE not in text and "Bearer" not in text
    assert NUMBER_ID in text  # The id is stored; the number is not.
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in logged and LINE not in logged
    view = json.dumps(state(env, owner))
    assert secret not in view and LINE not in view


def test_events_are_append_only(env):
    owner = headers(env)
    connect_calendar(env)
    step(env, owner)
    for sql in ("UPDATE provisioning_events SET outcome='x'", "DELETE FROM provisioning_events"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), connect(env["url"]) as conn:
            conn.execute(sql)
    assert len(rows(env["url"], "SELECT * FROM provisioning_events")) == 1


def test_owner_events_carry_the_actor(env):
    owner = headers(env)
    select(env, owner)
    [event] = state(env, owner)["events"]
    assert (event["step"], event["actor_id"]) == ("select_number", owner["_id"])


# Adapter: agent lookups (MockTransport only)


AGENT = "33333333-3333-4333-8333-333333333333"


def agent_body(token="cvt_" + "a" * 32 + "." + "b" * 43):
    return {
        "id": AGENT,
        "agent_name": "clinic-x",
        "agent_status": "processed",
        "agent_prompts": {"task_1": {"system_prompt": "synthetic prompt"}},
        "tasks": [
            {
                "tools_config": {
                    "api_tools": {
                        "tools": [],
                        "tools_params": {
                            "check_slots": {
                                "url": GATEWAY + "/v1/bolna/tools/check-slots",
                                "api_token": f"Bearer {token}",
                                "param": "{}",
                            }
                        },
                    }
                }
            }
        ],
    }


def client(handler):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return BolnaClient("synthetic-bolna-key", http, base_url="https://api.example.test")


def test_client_list_agents_skips_malformed_items():
    body = [agent_body(), {"agent_name": "no id"}, "junk"]
    bolna = client(lambda request: httpx.Response(200, json=body))
    assert [agent.id for agent in bolna.list_agents()] == [AGENT]


def test_client_get_and_list_agents_keep_only_allowlisted_fields():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path == "/v2/agent/all":
            return httpx.Response(200, json=[agent_body()])
        if request.url.path == f"/v2/agent/{AGENT}":
            return httpx.Response(200, json=agent_body())
        return httpx.Response(404, json={"message": "not found"})

    bolna = client(handler)
    agent = bolna.get_agent(AGENT)
    assert agent.id == AGENT and agent.name == "clinic-x"
    assert agent.tool_urls == {"check_slots": GATEWAY + "/v1/bolna/tools/check-slots"}
    assert agent.token_credential_ids == ("a" * 32,)
    assert "b" * 43 not in repr(agent)
    assert bolna.list_agents() == [agent]
    assert bolna.get_agent("44444444-4444-4444-8444-444444444444") is None
    assert seen[:2] == [("GET", f"/v2/agent/{AGENT}"), ("GET", "/v2/agent/all")]
    assert BolnaClient.consistent_agent_lookup is False
    with pytest.raises(ValueError):
        bolna.get_agent("../all")


def test_client_agent_reads_map_errors():
    responses = iter(
        [
            httpx.Response(200, json={"agents": []}),
            httpx.Response(200, json=[{"agent_name": "no id"}]),
            httpx.Response(503),
            httpx.Response(401),
            httpx.Response(200, json={"no": "id"}),
        ]
    )
    bolna = client(lambda request: next(responses))
    with pytest.raises(ProviderRetryable):
        bolna.list_agents()
    assert bolna.list_agents() == []  # A malformed item is skipped, not fatal.
    with pytest.raises(ProviderRetryable):
        bolna.list_agents()
    with pytest.raises(ProviderPermanent):
        bolna.get_agent(AGENT)
    with pytest.raises(ProviderRetryable):
        bolna.get_agent(AGENT)


def test_summarize_agent_accepts_string_encoded_tools_and_masked_tokens():
    body = agent_body()
    tools = body["tasks"][0]["tools_config"]["api_tools"]
    tools["tools_params"]["check_slots"]["api_token"] = "Bearer ****"
    body["tasks"][0]["tools_config"]["api_tools"] = json.dumps(tools)
    agent = summarize_agent(body)
    assert agent.tool_urls == {"check_slots": GATEWAY + "/v1/bolna/tools/check-slots"}
    assert agent.token_credential_ids == (None,)
    for unreadable in ("Bearer garbage", "", None, "cvt_short.***"):
        tools["tools_params"]["check_slots"]["api_token"] = unreadable
        body["tasks"][0]["tools_config"]["api_tools"] = tools
        assert summarize_agent(body).token_credential_ids == (UNREADABLE,)
    assert summarize_agent({"id": "bad id"}) is None
    assert summarize_agent([]) is None


# Gateway gating (state x tool)


EXECUTION = "55555555-5555-4555-8555-555555555555"
CONTEXT = {"execution_id": EXECUTION, "from_number": "+910000000123", "agent_id": ""}
DAY = date(2031, 3, 5)
TOOLS = {
    "context": ("/v1/bolna/context", None, False),
    "check-slots": (
        "/v1/bolna/tools/check-slots",
        {"doctor": "Demo Doctor", "service": "Consultation", "day": DAY.isoformat()},
        False,
    ),
    "appointment-status": (
        "/v1/bolna/tools/appointment-status",
        {"appointment_id": str(uuid4())},
        False,
    ),
    "verify-booking": ("/v1/bolna/tools/verify-booking", {"booking_reference": "ZZ-0000"}, False),
    "reserve-slot": (
        "/v1/bolna/tools/reserve-slot",
        {
            "doctor": "Demo Doctor",
            "service": "Consultation",
            "starts_at": "2031-03-05T10:00:00+05:30",
        },
        True,
    ),
    "cancel-booking": ("/v1/bolna/tools/cancel-booking", {"booking_reference": "ZZ-0000"}, True),
    "reschedule-booking": (
        "/v1/bolna/tools/reschedule-booking",
        {"booking_reference": "ZZ-0000", "starts_at": "2031-03-05T11:00:00+05:30"},
        True,
    ),
    "opt-out": ("/v1/bolna/tools/opt-out", {"booking_reference": "ZZ-0000"}, True),
}
STATES = [
    None,
    "draft",
    "calendar_connected",
    "agent_created",
    "number_assigned",
    "verified",
    "verified+test",
    "active",
    "failed",
    "manual_review",
]


def set_line(env, value):
    execute(env["url"], "DELETE FROM provisioning")
    if value is None:
        return
    test_calls = value.endswith("+test")
    current = value.removesuffix("+test")
    execute(
        env["url"],
        "INSERT INTO provisioning (clinic_id,state,agent_id,phone_number_id) VALUES (%s,%s,%s,%s)",
        (CLINIC, current, "66666666-6666-4666-8666-666666666666", NUMBER_ID),
    )
    execute(
        env["url"], "UPDATE clinics SET test_calls_enabled=%s WHERE id=%s", (test_calls, CLINIC)
    )


def tool_token(env):
    with connect(env["url"]) as conn:
        return credentials.issue(conn, CLINIC, "gating")[1]


def call_tool(env, token, name):
    path, extra, _ = TOOLS[name]
    body = None if extra is None else {**CONTEXT, **extra}
    return env["client"].post(path, json=body, headers={"Authorization": f"Bearer {token}"})


def allowed(line, changes):
    if line == "active":
        return True
    if line == "verified+test":
        return True
    return line == "verified" and not changes


@pytest.mark.parametrize("line", STATES)
def test_gateway_gating_matrix(env, line):
    token = tool_token(env)
    set_line(env, line)
    for name, (_, _, changes) in TOOLS.items():
        r = call_tool(env, token, name)
        refused = r.status_code == 403 and r.json() == {"detail": gateway.NOT_ACCEPTING}
        assert refused is not allowed(line, changes), (line, name, r.status_code, r.text)
    if not allowed(line, False):
        # Refusals have no side effects: no call session, appointment or attempt recorded.
        assert rows(env["url"], "SELECT * FROM call_sessions") == []
    assert rows(env["url"], "SELECT * FROM appointments") == [] or allowed(line, True)


def test_test_calls_allow_bookings_only_while_verified(env):
    token = tool_token(env)
    set_line(env, "verified")
    assert call_tool(env, token, "reserve-slot").status_code == 403
    set_line(env, "verified+test")
    r = call_tool(env, token, "reserve-slot")
    assert r.status_code != 403 or r.json()["detail"] != gateway.NOT_ACCEPTING
    # The flag means nothing before verified.
    set_line(env, "number_assigned+test")
    assert call_tool(env, token, "check-slots").status_code == 403


def test_other_clinic_state_does_not_open_this_line(env, open_line):
    token = tool_token(env)
    with connect(env["url"]) as conn:
        open_line(conn, OTHER_CLINIC)
    r = call_tool(env, token, "check-slots")
    assert (r.status_code, r.json()["detail"]) == (403, gateway.NOT_ACCEPTING)


def test_context_serves_approved_scripts_only(env):
    owner = headers(env)
    token = tool_token(env)
    set_line(env, "active")
    context = call_tool(env, token, "context").json()
    assert (context["emergency_text"], context["handoff_text"], context["approved_text"]) == (
        gateway.PLACEHOLDER_EMERGENCY,
        gateway.PLACEHOLDER_HANDOFF,
        False,
    )
    env["client"].patch(
        "/v1/admin/clinic",
        headers=h(owner),
        json={"emergency_text": EMERGENCY, "handoff_text": HANDOFF},
    )
    assert call_tool(env, token, "context").json()["approved_text"] is False  # Not approved.
    approve_scripts(env, owner)
    context = call_tool(env, token, "context").json()
    assert (context["emergency_text"], context["handoff_text"], context["approved_text"]) == (
        EMERGENCY,
        HANDOFF,
        True,
    )
    assert context["faq_text"] == gateway.PLACEHOLDER_FAQ


# Manual activation (CLI)


def manual(env, clinic=CLINIC):
    return cli.main(["provisioning", "activate-manual", "--clinic", str(clinic)])


def test_manual_activation_checks_prerequisites_and_opens_the_line(env, capsys):
    owner = headers(env)
    token = tool_token(env)
    with pytest.raises(SystemExit, match="set-agent"):
        manual(env)
    execute(
        env["url"],
        "UPDATE clinics SET bolna_agent_id='77777777-7777-4777-8777-777777777777' WHERE id=%s",
        (CLINIC,),
    )
    with pytest.raises(SystemExit, match="scripts"):
        manual(env)
    approve_scripts(env, owner)
    with pytest.raises(SystemExit, match="calendar"):
        manual(env)
    connect_calendar(env)
    with pytest.raises(SystemExit, match="Clinic not found"):
        manual(env, uuid4())
    assert rows(env["url"], "SELECT * FROM provisioning") == []
    assert call_tool(env, token, "check-slots").status_code == 403
    manual(env)
    assert "state=active" in capsys.readouterr().out
    view = state(env, owner)
    assert (view["state"], view["agent_id"], view["phone_number_id"]) == (
        "active",
        "77777777-7777-4777-8777-777777777777",
        None,
    )
    [event] = view["events"]
    assert (event["outcome"], event["actor_id"], event["to_state"]) == (
        "manual_activation",
        None,
        "active",
    )
    assert call_tool(env, token, "reserve-slot").json().get("detail") != gateway.NOT_ACCEPTING
    with pytest.raises(SystemExit, match="already has provisioning"):
        manual(env)


def test_hand_configured_clinic_cannot_start_api_provisioning(env):
    owner = headers(env)
    connect_calendar(env)
    approve_scripts(env, owner)
    execute(
        env["url"],
        "UPDATE clinics SET bolna_agent_id='77777777-7777-4777-8777-777777777777' WHERE id=%s",
        (CLINIC,),
    )
    for r in (
        advance(env, owner),
        select(env, owner),
        env["client"].post(f"{BASE}/reset", headers=h(owner)),
        env["client"].post(f"{BASE}/activate", headers=h(owner)),
    ):
        assert r.status_code == 409 and r.json()["detail"] == service.MANUAL_SETUP
    assert rows(env["url"], "SELECT * FROM provisioning") == []
    manual(env)
    assert state(env, owner)["state"] == "active"


def test_manual_activation_accepts_an_untouched_draft_row(env):
    owner = headers(env)
    connect_calendar(env)
    approve_scripts(env, owner)
    assert select(env, owner).status_code == 200  # Creates the draft row.
    execute(
        env["url"],
        "UPDATE clinics SET bolna_agent_id='77777777-7777-4777-8777-777777777777' WHERE id=%s",
        (CLINIC,),
    )
    manual(env)
    view = state(env, owner)
    assert (view["state"], view["agent_id"]) == ("active", "77777777-7777-4777-8777-777777777777")


def test_manual_activation_refuses_an_agent_used_elsewhere(env):
    owner = headers(env)
    connect_calendar(env)
    approve_scripts(env, owner)
    agent = "77777777-7777-4777-8777-777777777777"
    execute(env["url"], "UPDATE clinics SET bolna_agent_id=%s WHERE id=%s", (agent, CLINIC))
    execute(
        env["url"],
        "UPDATE clinics SET bolna_outbound_agent_id=%s WHERE id=%s",
        (agent, OTHER_CLINIC),
    )
    with pytest.raises(SystemExit, match="in use elsewhere"):
        manual(env)


def test_manual_activation_reports_a_concurrent_change_cleanly(env, monkeypatch):
    def collide(conn, clinic_id):
        raise psycopg.errors.UniqueViolation("synthetic")

    monkeypatch.setattr(service, "activate_manual", collide)
    with pytest.raises(SystemExit, match="Concurrent change"):
        manual(env)


def test_concurrent_number_selection_returns_409(env):
    owner = headers(env)
    execute(
        env["url"],
        "INSERT INTO provisioning (clinic_id) VALUES (%s)",
        (OTHER_CLINIC,),
    )
    with connect(env["url"]) as blocker:
        # Another clinic takes the number; uncommitted, so the taken check cannot see it and
        # the selection waits on the unique index until it commits.
        blocker.execute(
            "UPDATE provisioning SET phone_number_id=%s WHERE clinic_id=%s",
            (NUMBER_ID, OTHER_CLINIC),
        )
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(select, env, owner)
            waited = rows(
                env["url"],
                "SELECT count(*) AS n FROM pg_stat_activity WHERE wait_event_type='Lock'",
            )
            for _ in range(50):
                if waited[0]["n"]:
                    break
                waited = rows(
                    env["url"],
                    "SELECT count(*) AS n FROM pg_stat_activity WHERE wait_event_type='Lock'",
                )
            blocker.commit()
            r = pending.result(timeout=30)
    assert r.status_code == 409 and r.json()["detail"] == "That number cannot be selected"


def test_manual_activation_refuses_a_provisioned_clinic(env):
    owner = headers(env)
    connect_calendar(env)
    approve_scripts(env, owner)
    to_state(env, owner, "agent_created")
    with pytest.raises(SystemExit, match="already has provisioning"):
        manual(env)


def test_line_closed_after_the_first_check_refuses_the_write(env, monkeypatch):
    token = tool_token(env)
    set_line(env, "verified+test")
    original = gateway.upsert_session

    def upsert_then_close(conn, *args, **kwargs):
        session = original(conn, *args, **kwargs)
        # The owner turns test calls off between the route's check and the write.
        execute(env["url"], "UPDATE clinics SET test_calls_enabled=false WHERE id=%s", (CLINIC,))
        return session

    monkeypatch.setattr(gateway, "upsert_session", upsert_then_close)
    r = call_tool(env, token, "reserve-slot")
    assert r.status_code == 403 and r.json() == {"detail": gateway.NOT_ACCEPTING}
    assert rows(env["url"], "SELECT id FROM appointments") == []
