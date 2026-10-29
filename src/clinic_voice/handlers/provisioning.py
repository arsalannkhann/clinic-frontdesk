"""provision.step handler (T10): advance a clinic's provisioning by exactly one step.

Payload {version}: the provisioning version when the step was enqueued. A job whose version
no longer matches (a reset, a number change, another step) completes as 'stale' and does
nothing. Each run first counts the step attempt in its own committed statement (more than
STEP_MAX_ATTEMPTS runs of one step goes to manual_review), then reads with the connection
idle, calls Bolna with no transaction open, and applies the outcome in one transaction that
takes its own fenced outbox row, then the provisioning row (re-checking version and state),
writes the transition and its provisioning_events row and completes the job.

Steps:
- calendar (draft): provisioning.calendar_ready, no provider call. Not ready is a refusal:
  the state stays draft with last_error calendar_not_connected.
- agent (calendar_connected): compile the agent (agent_config) under the deterministic name
  agent_name(); find an existing agent by the stored agent_id, else by name in
  list_agents(); then issue a fresh tool credential (plaintext in memory only; its id is
  recorded) and update the found agent (PUT, full replacement) or create one. Before a
  create, agent_create_pending is committed as write intent. A create that timed out (or a
  crash after the intent) is reconciled on the next attempt by the name lookup; when the
  port has no lookup, or the lookup is not documented as consistent and finds nothing, the
  step goes to manual_review instead of creating a second agent. On success agent_id and
  clinics.bolna_agent_id are set and earlier provisioning credentials are revoked.
- number (agent_created): the owner-selected phone_number_id must be in list_phone_numbers()
  and not routed to another agent (refusal otherwise: number_not_in_account /
  number_in_use); then assign_phone_number (idempotent; a timeout retries and the next
  attempt sees the assignment in the listing). Never purchases.
- verify (number_assigned): get_agent must return the recorded id and name, every expected
  tool URL, and (where the token is readable) the recorded credential id; the number must be
  routed to the agent. Any mismatch goes to manual_review (resume at the step to redo).

Errors: ProviderPermanent -> failed (unauthorized or provider_permanent); timeouts and
retryable errors retry with backoff; a dead-lettered job (retries exhausted, lease expired)
moves the state to manual_review in the dead-letter transaction (on_dead).
"""

import logging
from datetime import timedelta

from clinic_voice import credentials
from clinic_voice import provisioning as service
from clinic_voice.bolna import agent_config
from clinic_voice.config import get_settings
from clinic_voice.outbox import (
    complete,
    extend_lease,
    fail_dead,
    mark_done,
    require_idle,
    retry,
)
from clinic_voice.providers.calendar import (
    ProviderError,
    ProviderPermanent,
    ProviderRetryable,
    ProviderTimeout,
)

log = logging.getLogger("clinic_voice.worker")

ATTEMPT_COLUMNS = {
    "calendar": "calendar_attempts",
    "agent": "agent_attempts",
    "number": "number_attempts",
    "verify": "verify_attempts",
}


def _version(job):
    value = (job.payload or {}).get("version")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _finish(conn, job, row, to_state, step, outcome, *, resume=None, **values) -> bool:
    """Apply one outcome: fenced outbox row first, then the provisioning row at the job's
    version and the state read at the start. to_state == row state records a refusal (no
    version change). Returns False (rolled back) when the lease or the version was lost."""
    if not extend_lease(conn, job):
        conn.rollback()
        return False
    current = service.load(conn, job.clinic_id, lock=True)
    if current is None or current["version"] != row["version"] or current["state"] != row["state"]:
        conn.rollback()
        log.info("provision step stale id=%s", job.id)
        return complete(conn, job)
    changes = dict(values)
    advanced = to_state != row["state"] and to_state not in ("failed", "manual_review")
    # A refusal (same state), failure or review keeps its code; an advance clears it.
    changes["last_error"] = None if advanced else outcome
    if to_state != row["state"]:
        changes["state"] = to_state
        if resume is not None:
            changes["resume_state"] = resume
        elif to_state not in ("failed", "manual_review"):
            changes["resume_state"] = to_state
    sets = ", ".join(f"{name}=%s" for name in changes)
    bump = ", version=version + 1" if to_state != row["state"] else ""
    conn.execute(
        f"UPDATE provisioning SET {sets}{bump}, updated_at=now() WHERE clinic_id=%s",  # noqa: S608
        (*changes.values(), job.clinic_id),
    )
    service.record(conn, job.clinic_id, row["state"], to_state, step, outcome)
    if not mark_done(conn, job):
        conn.rollback()
        return False
    conn.commit()
    log.info("provision step done id=%s step=%s outcome=%s", job.id, step, outcome)
    return True


def _count_attempt(conn, job, step):
    column = ATTEMPT_COLUMNS[step]
    row = conn.execute(
        f"UPDATE provisioning SET {column}={column} + 1 WHERE clinic_id=%s AND version=%s"  # noqa: S608
        f" RETURNING {column} AS n",
        (job.clinic_id, _version(job)),
    ).fetchone()
    conn.commit()
    return None if row is None else row["n"]


def _provider_error(conn, job, row, step, error: ProviderError) -> bool:
    if isinstance(error, ProviderPermanent):
        code = "unauthorized" if error.code == "unauthorized" else "provider_permanent"
        return _finish(conn, job, row, "failed", step, code)
    if isinstance(error, ProviderRetryable):
        delay = None if error.retry_after is None else timedelta(seconds=error.retry_after)
        return retry(conn, job, "retryable", delay)
    if isinstance(error, ProviderTimeout):
        return retry(conn, job, "timeout")
    return retry(conn, job, "provider_error")


def step(conn, job, ports) -> bool:
    version = _version(job)
    if version is None:
        return fail_dead(conn, job, "bad_payload")
    row = service.load(conn, job.clinic_id)
    conn.commit()
    if row is None or row["version"] != version or row["state"] not in service.STEPS:
        log.info("provision step stale id=%s", job.id)
        return complete(conn, job)
    name = service.STEPS[row["state"]]
    attempts = _count_attempt(conn, job, name)
    if attempts is None:
        return complete(conn, job)
    if attempts > service.STEP_MAX_ATTEMPTS:
        return _finish(conn, job, row, "manual_review", name, "retries_exhausted")
    port = getattr(ports, "bolna", None)
    if name != "calendar" and port is None:
        return fail_dead(conn, job, "bolna_unconfigured")
    return {"calendar": _calendar, "agent": _agent, "number": _number, "verify": _verify}[name](
        conn, job, port, row
    )


# calendar


def _calendar(conn, job, port, row) -> bool:
    ready = service.calendar_ready(conn, job.clinic_id)
    conn.commit()
    if ready:
        return _finish(conn, job, row, "calendar_connected", "calendar", "ok")
    return _finish(conn, job, row, "draft", "calendar", "calendar_not_connected")


# agent


def _find_agent(port, row, name):
    """(agent_id or None, refusal code or None). Provider errors propagate."""
    if row["agent_id"] is not None:
        found = port.get_agent(row["agent_id"])
        if found is not None:
            if found.name != name:
                return None, "agent_mismatch"
            return found.id, None
    lister = getattr(port, "list_agents", None)
    if lister is None:
        if row["agent_create_pending"] or row["agent_id"] is not None:
            return None, "lookup_unavailable"
        return None, None
    matches = [agent for agent in lister() if agent.name == name]
    if len(matches) > 1:
        return None, "agent_ambiguous"
    if matches:
        return matches[0].id, None
    if row["agent_create_pending"] and not getattr(port, "consistent_agent_lookup", False):
        return None, "create_uncertain"
    return None, None


def _agent(conn, job, port, row) -> bool:
    clinic_id = job.clinic_id
    base = get_settings().bolna_gateway_base_url
    clinic = conn.execute(
        "SELECT name, timezone, bolna_agent_id, bolna_outbound_agent_id FROM clinics WHERE id=%s",
        (clinic_id,),
    ).fetchone()
    conn.commit()
    if not base:
        return _finish(conn, job, row, "failed", "agent", "gateway_unconfigured")
    if clinic["bolna_agent_id"] is not None and clinic["bolna_agent_id"] != row["agent_id"]:
        # An agent recorded by hand (bolna set-agent): never replace it silently.
        return _finish(conn, job, row, "manual_review", "agent", "agent_preconfigured")
    name = row["agent_name"] or service.agent_name(clinic_id)
    require_idle(conn)
    try:
        agent_id, refusal = _find_agent(port, row, name)
    except ProviderError as error:
        return _provider_error(conn, job, row, "agent", error)
    if refusal is not None:
        return _finish(conn, job, row, "manual_review", "agent", refusal)
    # Issue the credential and record write intent together, fenced, before provider I/O.
    if not extend_lease(conn, job):
        conn.rollback()
        return False
    credential_id, token = credentials.issue(conn, clinic_id, service.CREDENTIAL_LABEL)
    updated = conn.execute(
        "UPDATE provisioning SET tool_credential_id=%s, agent_name=%s,"
        " agent_create_pending = agent_create_pending OR %s, updated_at=now()"
        " WHERE clinic_id=%s AND version=%s AND state='calendar_connected' RETURNING clinic_id",
        (credential_id, name, agent_id is None, clinic_id, row["version"]),
    ).fetchone()
    if updated is None:
        conn.rollback()
        return complete(conn, job)
    conn.commit()
    try:
        # The token exists only in this payload, in memory; it is never stored or logged.
        payload = agent_config.compile_agent(
            {"name": clinic["name"], "timezone": clinic["timezone"]},
            gateway_base_url=base,
            tool_token=token,
        )
    except ValueError:
        return _finish(conn, job, row, "failed", "agent", "agent_config_invalid")
    finally:
        del token
    payload["agent_config"]["agent_name"] = name
    require_idle(conn)
    created = agent_id is None
    try:
        if created:
            agent_id = port.create_agent(payload)
        else:
            port.update_agent(agent_id, payload)
    except ProviderPermanent as error:
        # A 4xx means the write was refused; nothing was created.
        values = {"agent_create_pending": False} if created else {}
        code = "unauthorized" if error.code == "unauthorized" else "provider_permanent"
        return _finish(conn, job, row, "failed", "agent", code, **values)
    except ProviderError as error:
        return _provider_error(conn, job, row, "agent", error)
    finally:
        del payload
    return _agent_recorded(conn, job, row, clinic, agent_id, credential_id, created)


def _agent_recorded(conn, job, row, clinic, agent_id, credential_id, created) -> bool:
    if clinic["bolna_outbound_agent_id"] == agent_id:
        return _finish(conn, job, row, "manual_review", "agent", "agent_conflict")
    if not extend_lease(conn, job):
        conn.rollback()
        return False
    current = service.load(conn, job.clinic_id, lock=True)
    if current is None or current["version"] != row["version"] or current["state"] != row["state"]:
        conn.rollback()
        return complete(conn, job)
    taken = conn.execute(
        "SELECT 1 FROM provisioning WHERE agent_id=%s AND clinic_id<>%s", (agent_id, job.clinic_id)
    ).fetchone()
    if taken:
        conn.rollback()
        return _finish(conn, job, row, "manual_review", "agent", "agent_conflict")
    conn.execute(
        "UPDATE provisioning SET agent_id=%s, agent_create_pending=false, state='agent_created',"
        " resume_state='agent_created', last_error=NULL, version=version + 1, updated_at=now()"
        " WHERE clinic_id=%s",
        (agent_id, job.clinic_id),
    )
    conn.execute("UPDATE clinics SET bolna_agent_id=%s WHERE id=%s", (agent_id, job.clinic_id))
    # The agent now carries the new credential: earlier provisioning credentials (from
    # attempts whose outcome was unknown, or a previous agent version) are revoked.
    conn.execute(
        "UPDATE tenant_credentials SET revoked_at=now() WHERE clinic_id=%s AND label=%s"
        " AND id<>%s AND revoked_at IS NULL",
        (job.clinic_id, service.CREDENTIAL_LABEL, credential_id),
    )
    outcome = "created" if created else "reconciled"
    service.record(conn, job.clinic_id, row["state"], "agent_created", "agent", outcome)
    if not mark_done(conn, job):
        conn.rollback()
        return False
    conn.commit()
    log.info("provision step done id=%s step=agent outcome=%s", job.id, outcome)
    return True


# number


def _number(conn, job, port, row) -> bool:
    number_id = row["phone_number_id"]
    if number_id is None:
        return _finish(conn, job, row, "agent_created", "number", "number_not_selected")
    require_idle(conn)
    try:
        number = next((n for n in port.list_phone_numbers() if n.id == number_id), None)
    except ProviderError as error:
        return _provider_error(conn, job, row, "number", error)
    if number is None:
        return _finish(conn, job, row, "agent_created", "number", "number_not_in_account")
    if number.agent_id is not None and number.agent_id != row["agent_id"]:
        return _finish(conn, job, row, "agent_created", "number", "number_in_use")
    if number.agent_id != row["agent_id"]:
        try:
            port.assign_phone_number(number_id, row["agent_id"])
        except ProviderError as error:
            return _provider_error(conn, job, row, "number", error)
    return _finish(conn, job, row, "number_assigned", "number", "ok")


# verify


def _verify(conn, job, port, row) -> bool:
    base = get_settings().bolna_gateway_base_url
    if not base:
        return _finish(conn, job, row, "failed", "verify", "gateway_unconfigured")
    try:
        expected = {
            tool: entry["url"]
            for tool, entry in agent_config.tool_definitions(base, agent_config.REDACTED)[
                "tools_params"
            ].items()
        }
    except ValueError:
        return _finish(conn, job, row, "failed", "verify", "agent_config_invalid")
    require_idle(conn)
    try:
        agent = port.get_agent(row["agent_id"])
        number = next(
            (n for n in port.list_phone_numbers() if n.id == row["phone_number_id"]), None
        )
    except ProviderError as error:
        return _provider_error(conn, job, row, "verify", error)
    if agent is None or agent.id != row["agent_id"] or agent.name != row["agent_name"]:
        return _finish(
            conn, job, row, "manual_review", "verify", "agent_mismatch", resume="calendar_connected"
        )
    if agent.tool_urls != expected:
        return _finish(
            conn, job, row, "manual_review", "verify", "tools_mismatch", resume="calendar_connected"
        )
    readable = [token_id for token_id in agent.token_credential_ids if token_id is not None]
    expected_credential = row["tool_credential_id"].hex if row["tool_credential_id"] else None
    if any(token_id != expected_credential for token_id in readable):
        return _finish(
            conn,
            job,
            row,
            "manual_review",
            "verify",
            "credential_mismatch",
            resume="calendar_connected",
        )
    if number is None or number.agent_id != row["agent_id"]:
        return _finish(
            conn, job, row, "manual_review", "verify", "number_mismatch", resume="agent_created"
        )
    # Bolna may mask api_token in GET responses (undocumented): then the credential cannot
    # be compared and the outcome says so.
    outcome = "ok" if len(readable) == len(agent.token_credential_ids) else "token_unchecked"
    return _finish(conn, job, row, "verified", "verify", outcome)


def on_dead(conn, job):
    """Retries exhausted or lease expired: manual_review, if the row is still at the job's
    version and in a stepping state. Runs inside the dead-letter transaction (outbox row
    already locked, then the provisioning row: the worker's lock order)."""
    version = _version(job)
    if version is None:
        return
    row = conn.execute(
        "SELECT state FROM provisioning WHERE clinic_id=%s AND version=%s FOR UPDATE",
        (job.clinic_id, version),
    ).fetchone()
    if row is None or row["state"] not in service.STEPS:
        return
    conn.execute(
        "UPDATE provisioning SET state='manual_review', last_error='retries_exhausted',"
        " version=version + 1, updated_at=now() WHERE clinic_id=%s",
        (job.clinic_id,),
    )
    service.record(
        conn,
        job.clinic_id,
        row["state"],
        "manual_review",
        service.STEPS[row["state"]],
        "retries_exhausted",
    )
