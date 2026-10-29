"""Clinic provisioning state machine (T10).

Synthetic data only: generated credentials, a "+910000000999" clinic line on a FakeBolna
account and example.test URLs. FakeCalendar and FakeBolna stand in for every provider;
BolnaClient is exercised through httpx.MockTransport only. Nothing reaches the network.
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from clinic_voice import credentials, staff
from clinic_voice import provisioning as service
from clinic_voice.api import app
from clinic_voice.cli import DEMO_CLINIC_ID as CLINIC
from clinic_voice.config import get_settings
from clinic_voice.db import connect
from clinic_voice.outbox import Ports, run_once
from clinic_voice.providers.bolna import BolnaClient, FakeBolna, PhoneNumber, summarize_agent
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


@pytest.fixture(autouse=True)
def cheap_hashing(monkeypatch):
    monkeypatch.setattr(staff, "SCRYPT_N", 2**10)


@pytest.fixture
def env(database, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database)
    monkeypatch.setenv("BOLNA_GATEWAY_BASE_URL", GATEWAY)
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
    first_credential = pending["tool_credential_id"]

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
    r = env["client"].post(f"{BASE}/reset", headers=h(owner))
    assert r.status_code == 200 and r.json()["state"] == "calendar_connected"
    assert step(env, owner)["state"] == "agent_created"
    assert len(env["fake"].agents) == 1
    assert "provisioning.reset" in [
        a["action"] for a in rows(env["url"], "SELECT action FROM audit_events")
    ]


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
    assert review["attempts"]["agent"] == service.STEP_MAX_ATTEMPTS + 1
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
    admin = headers(env, "admin")
    r = env["client"].patch("/v1/admin/clinic", headers=h(admin), json={"scripts_approved": True})
    assert r.status_code == 422
    approve_scripts(env, admin)
    r = env["client"].patch("/v1/admin/clinic", headers=h(admin), json={"handoff_text": "Hold on."})
    assert (r.json()["handoff_text"], r.json()["scripts_approved"]) == ("Hold on.", False)
    for bad in ["{execution_id}", "50% off", "line\nbreak", "x" * 1001]:
        r = env["client"].patch("/v1/admin/clinic", headers=h(admin), json={"emergency_text": bad})
        assert r.status_code == 422
    viewer = headers(env, "viewer")
    r = env["client"].patch("/v1/admin/clinic", headers=h(viewer), json={"scripts_approved": True})
    assert r.status_code == 403


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
    with pytest.raises(ProviderRetryable):
        bolna.list_agents()
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
    assert summarize_agent({"id": "bad id"}) is None
    assert summarize_agent([]) is None
