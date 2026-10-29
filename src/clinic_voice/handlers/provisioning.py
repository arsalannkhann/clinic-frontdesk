"""provision.step handler (T10): advance a clinic's provisioning by exactly one step.

Payload {version}: the provisioning version when the step was enqueued. A job whose version
no longer matches (a reset, a number change, another step) completes as 'stale' and does
nothing. Each run reads with the connection idle, calls Bolna with no transaction open, and
applies the outcome in one transaction that takes its own fenced outbox row, then the
provisioning row (re-checking version and state), writes the transition and its
provisioning_events row and completes the job.

Attempt budget: a step's counter grows for each provider write (agent create/update, number
assignment) and each provider error; refusals (calendar_not_connected, number_not_in_account,
...) and successful reads do not count. A step whose counter has reached STEP_MAX_ATTEMPTS
goes to manual_review (retries_exhausted) before any further provider call.

Credentials: an agent write is sent with two credentials issued together: a bolna-scope tool
credential (attempt_credential_id) and a webhook-scope credential carried in the agent's
webhook_url (attempt_webhook_credential_id). Once Bolna acknowledges the write they become
tool_credential_id and webhook_credential_id. Whenever a step ends in failed or
manual_review, every provisioning credential of the clinic (both labels) except those four
(held by the agent, or possibly held: a write whose outcome is unknown) is revoked; a write
Bolna refused (4xx) or never sent gives its credentials up at once. Operator-issued
credentials (other labels) are never touched.

Steps:
- calendar (draft): provisioning.calendar_ready, no provider call. Not ready is a refusal:
  the state stays draft with last_error calendar_not_connected.
- agent (calendar_connected): compile the agent (agent_config) under the deterministic name
  agent_name(); find an existing agent by the stored agent_id, else by name in
  list_agents(); then issue a fresh bolna-scope credential and webhook credential (plaintext
  in memory only; their ids are recorded as attempt_credential_id and
  attempt_webhook_credential_id) and update the found agent (PUT, full replacement) or create
  one, with webhook_url <BOLNA_GATEWAY_BASE_URL>/v1/webhooks/bolna/<webhook token>. Before a
  create, agent_create_pending is committed as write intent. A create that timed out (or a
  crash after the intent) is reconciled on the next attempt by the name lookup; when the
  port has no lookup, or the lookup is not documented as consistent and finds nothing, the
  step goes to manual_review instead of creating a second agent. On success agent_id and
  clinics.bolna_agent_id are set and earlier provisioning credentials of both labels are
  revoked.
- number (agent_created): the owner-selected phone_number_id must be in list_phone_numbers()
  and not routed to another agent (refusal otherwise: number_not_in_account /
  number_in_use); then assign_phone_number (idempotent; a timeout retries and the next
  attempt sees the assignment in the listing). Never purchases.
- verify (number_assigned): the recorded tool credential and webhook credential must be
  active; get_agent must return the recorded id and name, every expected tool URL, (where
  the token is readable) the recorded credential id and (where the read exposes
  webhook_url, which GET /v2/agent does not document) the expected webhook URL naming the
  recorded webhook credential (webhook_mismatch otherwise); the number must be routed to the
  agent. Any mismatch goes to manual_review (resume at the step to redo). The outcome is ok,
  or names what could not be compared: token_unchecked (masked api_token),
  webhook_unchecked (no webhook_url in the read, or webhook_credential_id NULL: a row
  provisioned before migration 017, whose hand-set webhook URL is not managed here) or
  token_and_webhook_unchecked.

Errors: ProviderPermanent -> failed (unauthorized or provider_permanent); timeouts and
retryable errors retry with backoff; a dead-lettered job (retries exhausted, lease expired)
moves the state to manual_review in the dead-letter transaction (on_dead).
"""

import logging
from datetime import timedelta

from psycopg.errors import UniqueViolation

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
    stopped = to_state in ("failed", "manual_review")
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
    if stopped:
        _revoke_unheld(conn, job.clinic_id)
    service.record(conn, job.clinic_id, row["state"], to_state, step, outcome)
    if not mark_done(conn, job):
        conn.rollback()
        return False
    conn.commit()
    log.info("provision step done id=%s step=%s outcome=%s", job.id, step, outcome)
    return True


def _revoke_unheld(conn, clinic_id):
    """Revoke the clinic's provisioning credentials (tool and webhook labels) that no agent
    holds or may hold (all but tool_credential_id, attempt_credential_id,
    webhook_credential_id and attempt_webhook_credential_id). Runs after the row update, in
    its transaction."""
    conn.execute(
        "UPDATE tenant_credentials t SET revoked_at=now() FROM provisioning p"
        " WHERE p.clinic_id=%(c)s AND t.clinic_id=%(c)s AND t.label = ANY(%(l)s)"
        " AND t.revoked_at IS NULL"
        " AND t.id IS DISTINCT FROM p.tool_credential_id"
        " AND t.id IS DISTINCT FROM p.attempt_credential_id"
        " AND t.id IS DISTINCT FROM p.webhook_credential_id"
        " AND t.id IS DISTINCT FROM p.attempt_webhook_credential_id",
        {"c": clinic_id, "l": list(service.LABELS)},
    )


def _exhausted(row, step) -> bool:
    return row[ATTEMPT_COLUMNS[step]] >= service.STEP_MAX_ATTEMPTS


def _count_attempt(conn, job, step):
    """Count one attempt, fenced on the job's lease and the row version, and commit. None:
    the lease or the version was lost (the caller stops)."""
    column = ATTEMPT_COLUMNS[step]
    if not extend_lease(conn, job):
        conn.rollback()
        return None
    row = conn.execute(
        f"UPDATE provisioning SET {column}={column} + 1 WHERE clinic_id=%s AND version=%s"  # noqa: S608
        f" RETURNING {column} AS n",
        (job.clinic_id, _version(job)),
    ).fetchone()
    conn.commit()
    return None if row is None else row["n"]


def _provider_error(conn, job, row, step, error: ProviderError, counted=False) -> bool:
    """Map a provider error. It counts against the step budget unless the write that failed
    was already counted (counted=True)."""
    if not counted and _count_attempt(conn, job, step) is None:
        return complete(conn, job)  # The row moved on: this job is stale.
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
    if _exhausted(row, name):
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
    if agent_id is not None:
        taken = service.agent_taken(conn, clinic_id, agent_id)
        conn.commit()
        if taken:
            # Checked before any credential is issued or any write is sent.
            return _finish(conn, job, row, "manual_review", "agent", "agent_conflict")
    # Issue the credential and record write intent together, fenced, before provider I/O.
    if not extend_lease(conn, job):
        conn.rollback()
        return False
    # bolna scope: accepted by /v1/bolna/* only, never by the generic /v1/tools/* API.
    credential_id, token = credentials.issue(
        conn, clinic_id, service.CREDENTIAL_LABEL, scope=service.CREDENTIAL_SCOPE
    )
    # webhook scope: accepted by /v1/webhooks/bolna/{token} only; carried in webhook_url.
    webhook_id, webhook_token = credentials.issue(
        conn, clinic_id, service.WEBHOOK_LABEL, scope=service.WEBHOOK_SCOPE
    )
    # The write below counts as an attempt whatever its outcome.
    updated = conn.execute(
        "UPDATE provisioning SET attempt_credential_id=%s, attempt_webhook_credential_id=%s,"
        " agent_name=%s, agent_create_pending = agent_create_pending OR %s,"
        " agent_attempts = agent_attempts + 1, updated_at=now()"
        " WHERE clinic_id=%s AND version=%s AND state='calendar_connected' RETURNING clinic_id",
        (credential_id, webhook_id, name, agent_id is None, clinic_id, row["version"]),
    ).fetchone()
    if updated is None:
        conn.rollback()
        return complete(conn, job)
    conn.commit()
    try:
        # The tokens exist only in this payload, in memory; they are never stored or logged.
        payload = agent_config.compile_agent(
            {"name": clinic["name"], "timezone": clinic["timezone"]},
            gateway_base_url=base,
            tool_token=token,
            webhook_url=agent_config.webhook_url(base, webhook_token),
        )
    except ValueError:
        # Nothing was sent: give this attempt's credentials up and restore the previous
        # intent (an earlier timed-out create may still be unresolved).
        return _finish(
            conn,
            job,
            row,
            "failed",
            "agent",
            "agent_config_invalid",
            agent_create_pending=row["agent_create_pending"],
            attempt_credential_id=row["attempt_credential_id"],
            attempt_webhook_credential_id=row["attempt_webhook_credential_id"],
        )
    finally:
        del token, webhook_token
    payload["agent_config"]["agent_name"] = name
    require_idle(conn)
    created = agent_id is None
    try:
        if created:
            agent_id = port.create_agent(payload)
        else:
            port.update_agent(agent_id, payload)
    except ProviderPermanent as error:
        # A 4xx means this write was refused: its credentials are given up. An earlier
        # timed-out create, if any, is still unresolved, so its intent and credentials stay.
        code = "unauthorized" if error.code == "unauthorized" else "provider_permanent"
        return _finish(
            conn,
            job,
            row,
            "failed",
            "agent",
            code,
            agent_create_pending=row["agent_create_pending"],
            attempt_credential_id=row["attempt_credential_id"],
            attempt_webhook_credential_id=row["attempt_webhook_credential_id"],
        )
    except ProviderError as error:
        return _provider_error(conn, job, row, "agent", error, counted=True)
    finally:
        del payload
    return _agent_recorded(conn, job, row, clinic, agent_id, credential_id, webhook_id, created)


def _agent_recorded(conn, job, row, clinic, agent_id, credential_id, webhook_id, created) -> bool:
    if not extend_lease(conn, job):
        conn.rollback()
        return False
    current = service.load(conn, job.clinic_id, lock=True)
    if current is None or current["version"] != row["version"] or current["state"] != row["state"]:
        conn.rollback()
        return complete(conn, job)
    if service.agent_taken(conn, job.clinic_id, agent_id):
        # The target was checked before the write, so this is a created id that belongs
        # elsewhere, or an agent claimed elsewhere while the write ran. That agent must not
        # keep this clinic's credentials: the attempt's are revoked with the review.
        conn.rollback()
        return _finish(
            conn,
            job,
            row,
            "manual_review",
            "agent",
            "agent_conflict",
            agent_create_pending=False if created else row["agent_create_pending"],
            attempt_credential_id=None,
            attempt_webhook_credential_id=None,
        )
    try:
        # Savepoint: another clinic's provisioning may record the same agent id between the
        # agent_taken check and this write (unique index provisioning_agent).
        with conn.transaction():
            # Compare-and-set: the clinic's agent id must still be what this step read before
            # the write (a concurrent bolna set-agent must not be overwritten).
            swapped = conn.execute(
                "UPDATE clinics SET bolna_agent_id=%s WHERE id=%s"
                " AND bolna_agent_id IS NOT DISTINCT FROM %s RETURNING id",
                (agent_id, job.clinic_id, clinic["bolna_agent_id"]),
            ).fetchone()
            if swapped is not None:
                conn.execute(
                    "UPDATE provisioning SET agent_id=%s, agent_create_pending=false,"
                    " state='agent_created', tool_credential_id=%s,"
                    " attempt_credential_id=NULL, webhook_credential_id=%s,"
                    " attempt_webhook_credential_id=NULL, resume_state='agent_created',"
                    " last_error=NULL, version=version + 1, updated_at=now()"
                    " WHERE clinic_id=%s",
                    (agent_id, credential_id, webhook_id, job.clinic_id),
                )
    except UniqueViolation:
        # Claimed elsewhere concurrently: same outcome as agent_taken above.
        conn.rollback()
        return _finish(
            conn,
            job,
            row,
            "manual_review",
            "agent",
            "agent_conflict",
            agent_create_pending=False if created else row["agent_create_pending"],
            attempt_credential_id=None,
            attempt_webhook_credential_id=None,
        )
    if swapped is None:
        conn.rollback()
        return _finish(conn, job, row, "manual_review", "agent", "agent_conflict")
    # The agent now carries the new credentials: earlier provisioning credentials of both
    # labels (from attempts whose outcome was unknown, or a previous agent version) are
    # revoked.
    conn.execute(
        "UPDATE tenant_credentials SET revoked_at=now() WHERE clinic_id=%s AND label = ANY(%s)"
        " AND id <> ALL(%s) AND revoked_at IS NULL",
        (job.clinic_id, list(service.LABELS), [credential_id, webhook_id]),
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
        if _count_attempt(conn, job, "number") is None:
            return complete(conn, job)
        require_idle(conn)
        try:
            port.assign_phone_number(number_id, row["agent_id"])
        except ProviderError as error:
            return _provider_error(conn, job, row, "number", error, counted=True)
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
    active = row["tool_credential_id"] is not None and service.credential_active(
        conn, job.clinic_id, row["tool_credential_id"]
    )
    # NULL webhook_credential_id: provisioned before migration 017; not managed, not checked.
    managed_webhook = row["webhook_credential_id"]
    if active and managed_webhook is not None:
        active = service.credential_active(
            conn, job.clinic_id, managed_webhook, (service.WEBHOOK_SCOPE,)
        )
    conn.commit()
    if not active:
        return _finish(
            conn,
            job,
            row,
            "manual_review",
            "verify",
            "credential_inactive",
            resume="calendar_connected",
        )
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
    # None: Bolna masked the token (only that counts as unchecked). UNREADABLE (absent or
    # not a tool credential) fails like a different credential.
    readable = [token_id for token_id in agent.token_credential_ids if token_id is not None]
    expected_credential = row["tool_credential_id"].hex
    if not agent.token_credential_ids or any(
        token_id != expected_credential for token_id in readable
    ):
        return _finish(
            conn,
            job,
            row,
            "manual_review",
            "verify",
            "credential_mismatch",
            resume="calendar_connected",
        )
    # GET /v2/agent does not document webhook_url: compared only when the read exposes it
    # (a masked token segment still has its URL compared; an unusable value fails).
    exposed = agent.webhook_base is not None or agent.webhook_credential_id is not None
    webhook_checked = managed_webhook is not None and exposed
    if webhook_checked and (
        agent.webhook_base != agent_config.gateway_url(base) + agent_config.WEBHOOK_PATH
        or agent.webhook_credential_id not in (None, managed_webhook.hex)
    ):
        return _finish(
            conn,
            job,
            row,
            "manual_review",
            "verify",
            "webhook_mismatch",
            resume="calendar_connected",
        )
    webhook_checked = webhook_checked and agent.webhook_credential_id is not None
    if number is None or number.agent_id != row["agent_id"]:
        return _finish(
            conn, job, row, "manual_review", "verify", "number_mismatch", resume="agent_created"
        )
    # Bolna may mask api_token in GET responses (undocumented): then the credential cannot
    # be compared and the outcome says so; likewise for a webhook URL the read does not show.
    token_checked = len(readable) == len(agent.token_credential_ids)
    outcome = {
        (True, True): "ok",
        (False, True): "token_unchecked",
        (True, False): "webhook_unchecked",
        (False, False): "token_and_webhook_unchecked",
    }[(token_checked, webhook_checked)]
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
    # The outbox row already carries the dead-letter code (same transaction). A lease that
    # expired with every attempt spent means workers died mid-step (possibly mid-create).
    dead = conn.execute(
        "SELECT last_error FROM outbox WHERE clinic_id=%s AND id=%s", (job.clinic_id, job.id)
    ).fetchone()
    code = "lease_lost" if dead and dead["last_error"] == "lease_expired" else "retries_exhausted"
    conn.execute(
        "UPDATE provisioning SET state='manual_review', last_error=%s,"
        " version=version + 1, updated_at=now() WHERE clinic_id=%s",
        (code, job.clinic_id),
    )
    _revoke_unheld(conn, job.clinic_id)
    service.record(
        conn, job.clinic_id, row["state"], "manual_review", service.STEPS[row["state"]], code
    )
