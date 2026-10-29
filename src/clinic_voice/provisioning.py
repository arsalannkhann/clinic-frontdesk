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
from psycopg.types.json import Jsonb

from clinic_voice.audit import audit

AGENT_GENERATION = 1
STEP_MAX_ATTEMPTS = 6
CREDENTIAL_LABEL = "provisioning:agent"
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
EVENT_LIMIT = 50

COLUMNS = (
    "state,resume_state,agent_id,agent_name,agent_create_pending,tool_credential_id,"
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
    conn.execute(
        "UPDATE provisioning SET phone_number_id=%s, last_error=NULL, version=version + 1,"
        " updated_at=now() WHERE clinic_id=%s",
        (phone_number_id, clinic_id),
    )
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
    conn.execute(
        "UPDATE provisioning SET state='active', last_error=NULL, version=version + 1,"
        " updated_at=now() WHERE clinic_id=%s",
        (clinic_id,),
    )
    record(conn, clinic_id, "verified", "active", "activate", "activated", staff["id"])
    audit(conn, clinic_id, staff["id"], "provisioning.activated")
    return view(conn, clinic_id)


def reset(conn, staff):
    """failed | manual_review -> resume_state (owner). Clears the error, the step attempt
    counters and the create intent: the owner asserts that the provider state was checked
    (for an ambiguous create, that no stray agent named agent_name exists, or that it is the
    one to keep; the next step looks it up again by name either way)."""
    clinic_id = staff["clinic_id"]
    row = _locked(conn, clinic_id)
    if row["state"] not in RESETTABLE:
        raise HTTPException(409, "Only failed or manual_review provisioning can be reset")
    if step_pending(conn, clinic_id):
        raise HTTPException(409, "A provisioning step is already pending")
    conn.execute(
        "UPDATE provisioning SET state=resume_state, last_error=NULL, agent_create_pending=false,"
        " calendar_attempts=0, agent_attempts=0, number_attempts=0, verify_attempts=0,"
        " version=version + 1, updated_at=now() WHERE clinic_id=%s",
        (clinic_id,),
    )
    record(conn, clinic_id, row["state"], row["resume_state"], "reset", "reset", staff["id"])
    audit(conn, clinic_id, staff["id"], "provisioning.reset")
    return view(conn, clinic_id)
