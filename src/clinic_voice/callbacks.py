"""Staff callback queue and staff-initiated callback calls (P2, ADR 0011).

Callback requests come from the website, the chat assistant and voice (public.store_callback).
Staff list them, resolve them (done / dismissed) and may place one outbound call per request
through BolnaPort ("Call now"). That call is a contact like a reminder call and obeys the same
clinic policy, checked in this order under the locks below:

1. The request exists in the staff member's clinic (404) and is not resolved
   (409 callback_resolved). A request already 'calling' answers 202 with its execution id
   (None while unknown) and places nothing: a repeated click never dials twice.
2. outbound_enabled (409 outbound_disabled), bolna_outbound_from_number (409 no_from_number),
   bolna_outbound_agent_id (409 no_outbound_agent; never the inbound agent with the booking
   tools), and a synthetic target (409 synthetic_contact: a seeded request, or one whose phone
   matches a seeded patient of the clinic, migration 019).
3. No Bolna API key (503 bolna_unconfigured), no keyring (503 unavailable).
4. Quiet hours at the current clinic-local time (409 quiet_hours) and the clinic-local day's
   contact budget, shared with reminders (409 budget_exhausted).

Lock order (locks.py, contacts): the contact-budget advisory lock of today, then the request
row FOR UPDATE, then the clinic row FOR SHARE (so a settings change waits for the decision).
The row is marked 'calling' (call_requested_at/by, call_contact_day, which counts against the
budget) and committed before the provider call, which runs with the connection idle. Outcomes:
success stores the execution id (call_outcome 'placed'); a timeout keeps 'calling' with
call_outcome 'unknown' (the call may have been placed; never redialled automatically, staff
resolve it); a definite failure returns the request to 'open' and releases the budget slot
(502 call_failed). The phone number and call context go to the port only, never to logs.
"""

import logging
import re
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from clinic_voice import reminders, service
from clinic_voice.audit import audit
from clinic_voice.crypto import CryptoError, Keyring
from clinic_voice.db import connect
from clinic_voice.errors import PublicError
from clinic_voice.providers.calendar import ProviderError, ProviderTimeout
from clinic_voice.public import callback_aad

log = logging.getLogger("clinic_voice.callbacks")

NOT_FOUND = "Not found"
STATUSES = ("open", "calling", "done", "dismissed")
RESOLVED = ("done", "dismissed")
EXECUTION_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")  # The column CHECK (migration 018).
COLUMNS = (
    "id, clinic_id, name_ciphertext, name_key_id, phone_ciphertext, phone_key_id, phone_hash,"
    " reason_ciphertext, reason_key_id, source, status, created_at, resolved_at,"
    " outbound_execution_id, call_requested_at, call_outcome, synthetic, patient_id"
)


def refuse(code: str, message: str, status: int = 409) -> PublicError:
    return PublicError(status, code, message)


def _decrypt(keyring, row, field) -> str | None:
    blob, key_id = row[f"{field}_ciphertext"], row[f"{field}_key_id"]
    if keyring is None or blob is None or key_id is None:
        return None
    try:
        return keyring.decrypt(
            key_id, bytes(blob), callback_aad(row["clinic_id"], row["id"], field)
        ).decode()
    except CryptoError:
        log.error("callback field could not be decrypted field=%s", field)
        return None


def mask_phone(phone: str | None) -> str | None:
    """The last four digits only, e.g. '****1234'."""
    if not phone:
        return None
    return "****" + phone[-4:]


def _out(row, keyring) -> dict:
    return {
        "id": row["id"],
        "name": _decrypt(keyring, row, "name"),
        "phone_masked": mask_phone(_decrypt(keyring, row, "phone")),
        "reason": _decrypt(keyring, row, "reason"),
        "source": row["source"],
        "status": row["status"],
        "created_at": row["created_at"],
        "resolved_at": row["resolved_at"],
        "execution_id": row["outbound_execution_id"],
        "call_requested_at": row["call_requested_at"],
        "synthetic": row["synthetic"],
    }


def list_callbacks(conn, staff, keyring, status, limit) -> list[dict]:
    """Newest first. Name, masked phone and reason only for owner/admin (and only with a
    keyring); each request whose details were decrypted is audited as
    'callback.details_viewed'."""
    clinic_id = staff["clinic_id"]
    with_details = staff["role"] in ("owner", "admin") and keyring is not None
    rows = conn.execute(
        f"""SELECT {COLUMNS} FROM callback_requests WHERE clinic_id=%(clinic)s
        AND (%(status)s::text IS NULL OR status=%(status)s)
        ORDER BY created_at DESC, id LIMIT %(limit)s""",  # noqa: S608
        {"clinic": clinic_id, "status": status, "limit": limit},
    ).fetchall()
    result = [_out(row, keyring if with_details else None) for row in rows]
    if with_details:
        for row in rows:
            audit(conn, clinic_id, staff["id"], "callback.details_viewed", row["id"])
    return result


def resolve(conn, staff, callback_id: UUID, status: str) -> dict:
    """Mark a request done or dismissed (from open or calling). Repeating the same
    resolution returns it unchanged; a different one is 409 callback_resolved."""
    clinic_id = staff["clinic_id"]
    row = conn.execute(
        "SELECT id, status, resolved_at FROM callback_requests WHERE clinic_id=%s AND id=%s"
        " FOR UPDATE",
        (clinic_id, callback_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, NOT_FOUND)
    if row["status"] in RESOLVED:
        if row["status"] != status:
            raise refuse("callback_resolved", "The callback request is already resolved")
        return dict(row)
    row = conn.execute(
        "UPDATE callback_requests SET status=%s, resolved_at=%s, resolved_by=%s"
        " WHERE clinic_id=%s AND id=%s RETURNING id, status, resolved_at",
        (status, service.utc_now(), staff["id"], clinic_id, callback_id),
    ).fetchone()
    audit(conn, clinic_id, staff["id"], f"callback.{status}", callback_id)
    return dict(row)


OUTBOUND_SQL = """SELECT id, name, timezone, outbound_enabled, bolna_outbound_agent_id,
    bolna_outbound_from_number, quiet_hours_start, quiet_hours_end, daily_contact_budget
    FROM clinics WHERE id=%s"""


def outbound_status(conn, clinic_id) -> dict:
    row = conn.execute(OUTBOUND_SQL, (clinic_id,)).fetchone()
    return {
        "enabled": bool(row["outbound_enabled"]),
        "has_from_number": row["bolna_outbound_from_number"] is not None,
        "has_outbound_agent": row["bolna_outbound_agent_id"] is not None,
    }


def _synthetic(conn, row) -> bool:
    if row["synthetic"]:
        return True
    return (
        conn.execute(
            """SELECT 1 FROM patients WHERE clinic_id=%s AND synthetic AND (phone_hash=%s
            OR id=%s) LIMIT 1""",
            (row["clinic_id"], row["phone_hash"], row["patient_id"]),
        ).fetchone()
        is not None
    )


def _calling(row) -> dict:
    return {"status": "calling", "execution_id": row["outbound_execution_id"]}


def _decide(conn, staff, callback_id, keyring, port):
    """The checks and the 'calling' mark in one transaction (module docstring). Returns
    (call arguments, None) to place a call, or (None, response) for a repeated click."""
    clinic_id = staff["clinic_id"]
    now = service.utc_now()
    zone = ZoneInfo(conn.execute(OUTBOUND_SQL, (clinic_id,)).fetchone()["timezone"])
    day = reminders.local_day(now, zone)
    reminders.lock_budget_day(conn, clinic_id, day)
    row = conn.execute(
        f"SELECT {COLUMNS} FROM callback_requests WHERE clinic_id=%s AND id=%s"  # noqa: S608
        " FOR UPDATE",
        (clinic_id, callback_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, NOT_FOUND)
    if row["status"] == "calling":
        return None, _calling(row)
    if row["status"] in RESOLVED:
        raise refuse("callback_resolved", "The callback request is already resolved")
    clinic = conn.execute(OUTBOUND_SQL + " FOR SHARE", (clinic_id,)).fetchone()
    if str(clinic["timezone"]) != str(zone.key):
        raise refuse("retry", "Clinic settings changed; please try again")
    if not clinic["outbound_enabled"]:
        raise refuse("outbound_disabled", "Outbound calls are turned off for this clinic")
    if clinic["bolna_outbound_from_number"] is None:
        raise refuse("no_from_number", "No outbound caller number is configured")
    if clinic["bolna_outbound_agent_id"] is None:
        raise refuse("no_outbound_agent", "No outbound voice agent is configured")
    if _synthetic(conn, row):
        raise refuse("synthetic_contact", "Synthetic demo contacts are never called")
    if port is None:
        raise refuse("bolna_unconfigured", "Calling is unavailable", 503)
    if keyring is None:
        raise refuse("unavailable", "Calling is unavailable", 503)
    start, end = clinic["quiet_hours_start"], clinic["quiet_hours_end"]
    if reminders.in_quiet(now, zone, start, end):
        raise refuse("quiet_hours", "It is within the clinic's quiet hours")
    if (
        reminders.budget_used(conn, clinic_id, day, reminders.ATTEMPTED)
        >= (clinic["daily_contact_budget"])
    ):
        raise refuse("budget_exhausted", "Today's contact budget is used up")
    phone = _decrypt(keyring, row, "phone")
    if not phone:
        raise refuse("unavailable", "Calling is unavailable", 503)
    conn.execute(
        """UPDATE callback_requests SET status='calling', call_requested_at=%s,
        call_requested_by=%s, call_contact_day=%s, call_outcome=NULL, outbound_execution_id=NULL
        WHERE clinic_id=%s AND id=%s""",
        (now, staff["id"], day, clinic_id, callback_id),
    )
    audit(conn, clinic_id, staff["id"], "callback.call_requested", callback_id)
    user_data = {
        "call_purpose": "callback_request",
        "clinic_name": clinic["name"],
        "contact_reference": f"cvc_{callback_id.hex}",
    }
    return (
        {
            "agent_id": clinic["bolna_outbound_agent_id"],
            "phone": phone,
            "from_number": clinic["bolna_outbound_from_number"],
            "user_data": user_data,
        },
        None,
    )


def _record(url, clinic_id, callback_id, outcome, execution_id=None, reopen=False):
    with connect(url) as conn:
        if reopen:
            conn.execute(
                """UPDATE callback_requests SET status='open', call_requested_at=NULL,
                call_requested_by=NULL, call_contact_day=NULL, call_outcome=%s
                WHERE clinic_id=%s AND id=%s AND status='calling'""",
                (outcome, clinic_id, callback_id),
            )
        else:
            conn.execute(
                """UPDATE callback_requests SET call_outcome=%s,
                outbound_execution_id=COALESCE(%s, outbound_execution_id)
                WHERE clinic_id=%s AND id=%s AND status='calling'""",
                (outcome, execution_id, clinic_id, callback_id),
            )


def call(url, staff, callback_id: UUID, keyring: Keyring | None, port) -> dict:
    """Place the callback call ("Call now"); see the module docstring for the checks."""
    clinic_id = staff["clinic_id"]
    with connect(url) as conn:
        arguments, repeated = _decide(conn, staff, callback_id, keyring, port)
    if repeated is not None:
        return repeated
    try:
        execution_id = port.start_outbound_call(
            arguments["agent_id"],
            arguments["phone"],
            from_phone_number=arguments["from_number"],
            user_data=arguments["user_data"],
        )
    except ProviderTimeout:
        _record(url, clinic_id, callback_id, "unknown")
        log.warning("callback call outcome unknown callback=%s clinic=%s", callback_id, clinic_id)
        return {"status": "calling", "execution_id": None}
    except ProviderError as error:
        _record(url, clinic_id, callback_id, "failed", reopen=True)
        log.warning(
            "callback call failed callback=%s clinic=%s error=%s",
            callback_id,
            clinic_id,
            type(error).__name__,
        )
        raise refuse("call_failed", "The call could not be placed", 502) from None
    if not isinstance(execution_id, str) or not EXECUTION_ID.fullmatch(execution_id):
        execution_id = None
    _record(url, clinic_id, callback_id, "placed", execution_id)
    log.info("callback call placed callback=%s clinic=%s", callback_id, clinic_id)
    return {"status": "calling", "execution_id": execution_id}
