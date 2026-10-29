"""Clinic provisioning state machine (T10): staff-side service and shared helpers.

States (migration 015): draft -> calendar_connected -> agent_created -> number_assigned ->
verified -> active; any step may end in failed (permanent provider or configuration error)
or manual_review (ambiguous outcome, exhausted retries, provider state that does not match).
The worker advances one step per provision.step job (handlers/provisioning.py); activation
and reset are owner actions here. Nothing in this module calls a provider.

Fencing: `version` increases on every state change, number selection, reset and activation.
A job carries the version it was enqueued at and only acts while the row still has it, so a
stale or duplicate job never steps twice. At most one provision.step job per clinic is
pending or processing (unique index outbox_provision_step); advance returns 409 otherwise,
and number selection, reset and activation are refused while a step is in flight, so the
worker never acts on a number or state that changed underneath it.

Lock order: the provisioning row FOR UPDATE, then (advance) the new outbox row. The worker's
final transaction takes its own fenced outbox row first, then the provisioning row; its
dead-letter hook runs with the outbox row already locked. Staff requests never lock an
existing outbox row, so the two orders cannot deadlock.

Every transition and refusal appends a provisioning_events row; staff mutations are audited
with ids only. No credential, provider body or phone number is stored or logged.
"""

from uuid import UUID, uuid4

from fastapi import HTTPException
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from clinic_voice.audit import audit

AGENT_GENERATION = 1
STEP_MAX_ATTEMPTS = 6
CREDENTIAL_LABEL = "provisioning:agent"
# The agent's credential works on /v1/bolna/* only (deps.tenant), so it cannot reach the
# generic /v1/tools/* API (T12).
CREDENTIAL_SCOPE = "bolna"
# The agent's webhook_url carries a webhook-scope credential of this label (migration 017);
# operator-issued webhook credentials (other labels) are never touched by provisioning.
WEBHOOK_LABEL = "provisioning:webhook"
WEBHOOK_SCOPE = "webhook"
# Labels of the credentials provisioning issues and revokes.
LABELS = (CREDENTIAL_LABEL, WEBHOOK_LABEL)
# State -> the step a provision.step job runs from it.
STEPS = {
    "draft": "calendar",
    "calendar_connected": "agent",
    "agent_created": "number",
    "number_assigned": "verify",
}
NEXT = {
    "draft": "calendar_connected",
    "calendar_connected": "agent_created",
    "agent_created": "number_assigned",
    "number_assigned": "verified",
}
NUMBER_SELECTABLE = ("draft", "calendar_connected", "agent_created")
RESETTABLE = ("failed", "manual_review")
# Errors after which the owner may clear an ambiguous create intent (reset with
# confirm_no_stray_agent): the lookup could not rule out an agent created by a lost write.
STRAY_AGENT_ERRORS = ("create_uncertain", "lease_lost")
MANUAL_SETUP = "Manual setup: use clinic-voice provisioning activate-manual"
EVENT_LIMIT = 50

COLUMNS = (
    "state,resume_state,agent_id,agent_name,agent_create_pending,tool_credential_id,"
    "attempt_credential_id,webhook_credential_id,attempt_webhook_credential_id,"
    "phone_number_id,calendar_attempts,agent_attempts,number_attempts,verify_attempts,"
    "last_error,version,updated_at"
)


def agent_name(clinic_id: UUID, generation: int = AGENT_GENERATION) -> str:
    """Deterministic provider reference for the clinic's inbound agent. The full clinic id
    keeps names unique across the shared Bolna account."""
    return f"clinic-{clinic_id.hex}-v{generation}"


def ensure(conn, clinic_id) -> None:
    conn.execute(
        "INSERT INTO provisioning (clinic_id) VALUES (%s) ON CONFLICT DO NOTHING", (clinic_id,)
    )


def load(conn, clinic_id, lock=False):
    suffix = " FOR UPDATE" if lock else ""
    return conn.execute(
        f"SELECT {COLUMNS} FROM provisioning WHERE clinic_id=%s{suffix}",  # noqa: S608
        (clinic_id,),
    ).fetchone()


def record(conn, clinic_id, from_state, to_state, step, outcome, actor_id=None) -> None:
    conn.execute(
        "INSERT INTO provisioning_events (id,clinic_id,from_state,to_state,step,outcome,actor_id)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (uuid4(), clinic_id, from_state, to_state, step, outcome, actor_id),
    )


def agent_taken(conn, clinic_id, agent_id) -> bool:
    """Whether agent_id belongs elsewhere: this clinic's outbound agent, or any other
    clinic's provisioned, inbound or outbound agent. The Bolna account is shared, so a name
    or id lookup could return another clinic's agent; it must never be overwritten."""
    row = conn.execute(
        """SELECT EXISTS (SELECT 1 FROM provisioning WHERE agent_id=%(a)s AND clinic_id<>%(c)s)
        OR EXISTS (SELECT 1 FROM clinics WHERE id<>%(c)s
            AND (bolna_agent_id=%(a)s OR bolna_outbound_agent_id=%(a)s))
        OR EXISTS (SELECT 1 FROM clinics WHERE id=%(c)s AND bolna_outbound_agent_id=%(a)s)
        AS taken""",
        {"a": agent_id, "c": clinic_id},
    ).fetchone()
    return row["taken"]


def credential_active(conn, clinic_id, credential_id, scopes=("bolna", "tools")) -> bool:
    """The agent's credential exists for this clinic with one of `scopes` (default: a gateway
    scope, bolna, or tools from a database provisioned before migration 016; ('webhook',) for
    the webhook credential), is unrevoked and unexpired."""
    return (
        conn.execute(
            "SELECT 1 FROM tenant_credentials WHERE clinic_id=%s AND id=%s"
            " AND scope = ANY(%s)"
            " AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())",
            (clinic_id, credential_id, list(scopes)),
        ).fetchone()
        is not None
    )


def calendar_ready(conn, clinic_id) -> bool:
    """An active calendar account, and at least one active doctor mapped to a booking
    calendar through an active account (its own, or the clinic's only active account)."""
    row = conn.execute(
        """SELECT EXISTS (
            SELECT 1 FROM doctors d WHERE d.clinic_id=%(c)s AND d.active
            AND d.booking_calendar_id IS NOT NULL
            AND CASE WHEN d.calendar_account_id IS NOT NULL THEN EXISTS (
                    SELECT 1 FROM calendar_accounts a WHERE a.clinic_id=d.clinic_id
                    AND a.id=d.calendar_account_id AND a.status='active')
                ELSE (SELECT count(*) FROM calendar_accounts a WHERE a.clinic_id=d.clinic_id
                    AND a.status='active') = 1 END
        ) AS ready""",
        {"c": clinic_id},
    ).fetchone()
    return row["ready"]


def step_pending(conn, clinic_id) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM outbox WHERE clinic_id=%s AND kind='provision.step'"
            " AND status IN ('pending','processing')",
            (clinic_id,),
        ).fetchone()
        is not None
    )


def _attempts(row):
    return {
        "calendar": row["calendar_attempts"],
        "agent": row["agent_attempts"],
        "number": row["number_attempts"],
        "verify": row["verify_attempts"],
    }


def view(conn, clinic_id):
    """State, ids and recent events (newest first). A clinic that never started is draft."""
    row = load(conn, clinic_id)
    events = conn.execute(
        "SELECT from_state,to_state,step,outcome,actor_id,created_at FROM provisioning_events"
        " WHERE clinic_id=%s ORDER BY seq DESC LIMIT %s",
        (clinic_id, EVENT_LIMIT),
    ).fetchall()
    pending = step_pending(conn, clinic_id)
    if row is None:
        return {
            "state": "draft",
            "resume_state": "draft",
            "agent_id": None,
            "agent_name": None,
            "agent_create_pending": False,
            "tool_credential_id": None,
            "phone_number_id": None,
            "attempts": {"calendar": 0, "agent": 0, "number": 0, "verify": 0},
            "last_error": None,
            "version": 0,
            "updated_at": None,
            "step_pending": pending,
            "events": events,
        }
    return {
        "state": row["state"],
        "resume_state": row["resume_state"],
        "agent_id": row["agent_id"],
        "agent_name": row["agent_name"],
        "agent_create_pending": row["agent_create_pending"],
        "tool_credential_id": row["tool_credential_id"],
        "phone_number_id": row["phone_number_id"],
        "attempts": _attempts(row),
        "last_error": row["last_error"],
        "version": row["version"],
        "updated_at": row["updated_at"],
        "step_pending": pending,
        "events": events,
    }


def _locked(conn, clinic_id):
    """The clinic's provisioning row, created as draft if missing and locked. A clinic set
    up by hand (agent id recorded, no row) is refused: a draft row would only start a second
    agent for it; the operator activates it with activate-manual instead."""
    row = load(conn, clinic_id, lock=True)
    if row is not None:
        return row
    clinic = conn.execute(
        "SELECT bolna_agent_id FROM clinics WHERE id=%s FOR SHARE", (clinic_id,)
    ).fetchone()
    if clinic is not None and clinic["bolna_agent_id"] is not None:
        raise HTTPException(409, MANUAL_SETUP)
    ensure(conn, clinic_id)
    return load(conn, clinic_id, lock=True)


def advance(conn, staff):
    """Enqueue one provision.step job for the current state (owner). 409 when a step is
    already pending, when the state has no next step, or when the number step would run
    without a selected number."""
    clinic_id = staff["clinic_id"]
    row = _locked(conn, clinic_id)
    if row["state"] not in STEPS:
        raise HTTPException(409, "No provisioning step to run from this state")
    if row["state"] == "agent_created" and row["phone_number_id"] is None:
        raise HTTPException(409, "Select a phone number first")
    if step_pending(conn, clinic_id):
        raise HTTPException(409, "A provisioning step is already pending")
    job_id = uuid4()
    from clinic_voice.outbox import DEFAULT_MAX_ATTEMPTS

    conn.execute(
        "INSERT INTO outbox (id,clinic_id,kind,payload,max_attempts)"
        " VALUES (%s,%s,'provision.step',%s,%s)",
        (job_id, clinic_id, Jsonb({"version": row["version"]}), DEFAULT_MAX_ATTEMPTS),
    )
    audit(conn, clinic_id, staff["id"], "provisioning.advance", job_id)
    return {"job_id": job_id, "state": row["state"], "step": STEPS[row["state"]]}


def select_number(conn, staff, phone_number_id):
    """Record the owner's choice of an existing Bolna number id. It is checked against the
    account's numbers when the number step runs; nothing is purchased."""
    clinic_id = staff["clinic_id"]
    row = _locked(conn, clinic_id)
    if row["state"] not in NUMBER_SELECTABLE:
        raise HTTPException(409, "The number can no longer be changed in this state")
    if step_pending(conn, clinic_id):
        raise HTTPException(409, "A provisioning step is already pending")
    taken = conn.execute(
        "SELECT 1 FROM provisioning WHERE phone_number_id=%s AND clinic_id<>%s",
        (phone_number_id, clinic_id),
    ).fetchone()
    if taken:
        # Same response as any other conflict: do not reveal which clinic holds it.
        raise HTTPException(409, "That number cannot be selected")
    try:
        with conn.transaction():
            conn.execute(
                "UPDATE provisioning SET phone_number_id=%s, last_error=NULL,"
                " version=version + 1, updated_at=now() WHERE clinic_id=%s",
                (phone_number_id, clinic_id),
            )
    except UniqueViolation:
        # Another clinic selected it concurrently (unique index on phone_number_id).
        raise HTTPException(409, "That number cannot be selected") from None
    record(
        conn, clinic_id, row["state"], row["state"], "select_number", "number_selected", staff["id"]
    )
    audit(conn, clinic_id, staff["id"], "provisioning.number_selected")
    return view(conn, clinic_id)


def activate(conn, staff):
    """verified -> active (owner). Requires a connected calendar, the clinic's agent id to
    still be the provisioned one, and approved non-empty emergency and handoff scripts. It
    does not enable outbound contact (clinics.outbound_enabled is untouched)."""
    clinic_id = staff["clinic_id"]
    row = _locked(conn, clinic_id)
    if row["state"] != "verified":
        raise HTTPException(409, "Provisioning must be verified before activation")
    clinic = conn.execute(
        "SELECT bolna_agent_id, emergency_text, handoff_text, scripts_approved FROM clinics"
        " WHERE id=%s FOR SHARE",
        (clinic_id,),
    ).fetchone()
    if not calendar_ready(conn, clinic_id):
        raise HTTPException(409, "Connect a calendar and map a doctor first")
    if not (
        clinic["scripts_approved"]
        and clinic["emergency_text"].strip()
        and clinic["handoff_text"].strip()
    ):
        raise HTTPException(409, "Approved emergency and handoff scripts are required")
    if clinic["bolna_agent_id"] != row["agent_id"]:
        raise HTTPException(409, "The clinic's agent changed; reset and verify again")
    if row["tool_credential_id"] is None or not credential_active(
        conn, clinic_id, row["tool_credential_id"]
    ):
        raise HTTPException(409, "The agent's tool credential is no longer active")
    # NULL: provisioned before migration 017, webhook URL not managed here.
    if row["webhook_credential_id"] is not None and not credential_active(
        conn, clinic_id, row["webhook_credential_id"], (WEBHOOK_SCOPE,)
    ):
        raise HTTPException(409, "The agent's webhook credential is no longer active")
    conn.execute(
        "UPDATE provisioning SET state='active', last_error=NULL, version=version + 1,"
        " updated_at=now() WHERE clinic_id=%s",
        (clinic_id,),
    )
    record(conn, clinic_id, "verified", "active", "activate", "activated", staff["id"])
    audit(conn, clinic_id, staff["id"], "provisioning.activated")
    return view(conn, clinic_id)


def reset(conn, staff, confirm_no_stray_agent=False):
    """failed | manual_review -> resume_state (owner). Clears the error and the step attempt
    counters. An ambiguous create intent (agent_create_pending) is kept, so the next agent
    step still refuses to create while the lookup cannot rule out a stray agent. Clearing it
    needs confirm_no_stray_agent: the owner checked the Bolna account and no agent named
    agent_name exists. It is accepted only after create_uncertain or lease_lost; it also
    revokes the credentials (tool and webhook) that the lost write may have handed to that
    agent. Without the confirmation attempt_credential_id and attempt_webhook_credential_id
    are kept (never cleared without revoking)."""
    clinic_id = staff["clinic_id"]
    row = _locked(conn, clinic_id)
    if row["state"] not in RESETTABLE:
        raise HTTPException(409, "Only failed or manual_review provisioning can be reset")
    if step_pending(conn, clinic_id):
        raise HTTPException(409, "A provisioning step is already pending")
    pending = row["agent_create_pending"]
    if confirm_no_stray_agent:
        if not pending or row["last_error"] not in STRAY_AGENT_ERRORS:
            raise HTTPException(409, "There is no uncertain agent create to confirm")
        from clinic_voice import credentials

        for attempt, held_by_agent in (
            ("attempt_credential_id", "tool_credential_id"),
            ("attempt_webhook_credential_id", "webhook_credential_id"),
        ):
            held = row[attempt]
            if held is not None and held != row[held_by_agent]:
                credentials.revoke(conn, held)
        pending = False
    # The attempt credentials are kept unless the owner confirmed no stray agent (they were
    # then revoked above): an agent update whose outcome was unknown may still hold them, so
    # they are neither cleared unrevoked nor revoked here. The next agent step replaces them,
    # and revokes them once newer credentials are acknowledged (or the step stops).
    conn.execute(
        "UPDATE provisioning SET state=resume_state, last_error=NULL, agent_create_pending=%s,"
        " attempt_credential_id=CASE WHEN %s THEN NULL ELSE attempt_credential_id END,"
        " attempt_webhook_credential_id=CASE WHEN %s THEN NULL"
        " ELSE attempt_webhook_credential_id END,"
        " calendar_attempts=0, agent_attempts=0, number_attempts=0, verify_attempts=0,"
        " version=version + 1, updated_at=now() WHERE clinic_id=%s",
        (pending, confirm_no_stray_agent, confirm_no_stray_agent, clinic_id),
    )
    outcome = "reset_confirmed_no_stray_agent" if confirm_no_stray_agent else "reset"
    record(conn, clinic_id, row["state"], row["resume_state"], "reset", outcome, staff["id"])
    audit(
        conn,
        clinic_id,
        staff["id"],
        "provisioning.reset_confirmed" if confirm_no_stray_agent else "provisioning.reset",
    )
    return view(conn, clinic_id)


class ManualActivationRefused(Exception):
    """str(error) is a fixed operator-facing reason."""


def activate_manual(conn, clinic_id) -> None:
    """Mark a clinic set up by hand (agent recorded with `bolna set-agent`, number routed in
    the Bolna dashboard) as active, so the gateway serves it. Operator CLI only.

    It does NOT verify anything at Bolna (no agent, tool URL, credential or number check):
    the operator vouches for the hand configuration. It checks what still applies locally:
    the clinic exists; it has no provisioning row, or only an untouched draft row (no agent,
    no credential, no step pending); its calendar is ready; its scripts are approved; its
    bolna_agent_id is set and belongs to no other clinic (nor is its own outbound agent).
    Writes the row (state active, the hand-set agent id, no number id) and an event with no
    actor, outcome manual_activation. Nothing is sent to Bolna."""
    # Lock order (locks.py): the provisioning row, then the clinic row.
    row = load(conn, clinic_id, lock=True)
    clinic = conn.execute(
        "SELECT bolna_agent_id, emergency_text, handoff_text, scripts_approved FROM clinics"
        " WHERE id=%s FOR UPDATE",
        (clinic_id,),
    ).fetchone()
    if clinic is None:
        raise ManualActivationRefused("Clinic not found")
    if row is not None and not (
        row["state"] == "draft"
        and row["agent_id"] is None
        and row["attempt_credential_id"] is None
        and row["tool_credential_id"] is None
        and row["attempt_webhook_credential_id"] is None
        and row["webhook_credential_id"] is None
        and not row["agent_create_pending"]
        and not step_pending(conn, clinic_id)
    ):
        raise ManualActivationRefused(
            "Clinic already has provisioning state; use the provisioning API"
        )
    if clinic["bolna_agent_id"] is None:
        raise ManualActivationRefused("Set the agent first: clinic-voice bolna set-agent")
    if not (
        clinic["scripts_approved"]
        and clinic["emergency_text"].strip()
        and clinic["handoff_text"].strip()
    ):
        raise ManualActivationRefused("Approved emergency and handoff scripts are required")
    if not calendar_ready(conn, clinic_id):
        raise ManualActivationRefused("Connect a calendar and map a doctor first")
    if agent_taken(conn, clinic_id, clinic["bolna_agent_id"]):
        raise ManualActivationRefused("That agent is in use elsewhere")
    try:
        with conn.transaction():
            if row is None:
                conn.execute(
                    "INSERT INTO provisioning (clinic_id,state,resume_state,agent_id)"
                    " VALUES (%s,'active','verified',%s)",
                    (clinic_id, clinic["bolna_agent_id"]),
                )
            else:
                conn.execute(
                    "UPDATE provisioning SET state='active', resume_state='verified',"
                    " agent_id=%s, last_error=NULL, version=version + 1, updated_at=now()"
                    " WHERE clinic_id=%s",
                    (clinic["bolna_agent_id"], clinic_id),
                )
    except UniqueViolation:
        # Concurrent activation of this clinic, or the agent was provisioned elsewhere.
        raise ManualActivationRefused("Concurrent change; check the state and retry") from None
    record(conn, clinic_id, "draft", "active", "activate", "manual_activation")
    audit(conn, clinic_id, None, "provisioning.manual_activation")
