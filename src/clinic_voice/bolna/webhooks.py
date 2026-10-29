"""Bolna execution webhooks: inbox, monotonic call-session state and reconciliation (T8).

Trust model (ADR 0007):
- The tenant comes only from the webhook credential in the URL path
  (/v1/webhooks/bolna/cvw_<id>.<secret>). Bolna documents no signature, no secret and no
  API field for webhook headers, so the URL carries the credential. The payload's agent_id
  is cross-checked against the clinic's configured agent id (and the session's), never used
  to select a tenant.
- Every accepted delivery is stored minimized (providers.bolna.minimize_execution: an
  explicit allowlist of ids, status, timestamps, durations, disposition codes and numeric
  cost) together with a webhook.process outbox job, in one transaction. Nothing else from the
  body is stored or logged.
- Bolna events carry no event id; the dedupe key is execution id + status + SHA-256 of the
  canonical minimized payload, unique per clinic. A redelivery returns 2xx and adds nothing.

Call-session state is monotonic (providers.bolna.STATUS_RANK): a status of lower rank than
the current one is ignored ('stale'), and nothing replaces a final status. A repeat of the
final status may only fill disposition fields that are still empty. Reaching
call-disconnected or a final status ends the session (tools then refuse it). Events for an
execution with no call session are kept in the inbox and marked ignored
('unknown_execution'): a session exists only once the trusted gateway has seen the call.
"""

import hashlib
import logging
from datetime import datetime, timedelta
from uuid import uuid4

from psycopg.types.json import Jsonb

from clinic_voice.providers.bolna import (
    ENDED_RANK,
    FINAL_RANK,
    STATUS_RANK,
    canonical,
)

log = logging.getLogger("clinic_voice.bolna")

# Most reconciliation polls per session; afterwards the session stays unfinished and is
# visible to operators (OPERATIONS.md).
MAX_RECONCILES = 6
MAX_RECONCILE_DELAY = timedelta(hours=24)

SESSION_COLUMNS = (
    "id, execution_id, agent_id, provider_status, provider_final, provider_event_at,"
    " termination_reason,"
    " hangup_reason, hangup_by, hangup_provider_code, call_type, conversation_seconds,"
    " total_cost"
)
# Minimized field -> call_sessions column, with the SQL type for NULL-safe parameters.
DISPOSITION = {
    "termination_reason": ("termination_reason", "text"),
    "hangup_reason": ("hangup_reason", "text"),
    "hangup_by": ("hangup_by", "text"),
    "hangup_provider_code": ("hangup_provider_code", "integer"),
    "call_type": ("call_type", "text"),
    "conversation_duration": ("conversation_seconds", "double precision"),
    "total_cost": ("total_cost", "double precision"),
}


def event_key(minimized: dict) -> str:
    digest = hashlib.sha256(canonical(minimized).encode()).hexdigest()
    return f"{minimized['execution_id']}:{minimized['status']}:{digest}"


def clinic_agent(conn, clinic_id) -> str | None:
    """The clinic's configured Bolna agent id, or None."""
    row = conn.execute("SELECT bolna_agent_id FROM clinics WHERE id=%s", (clinic_id,)).fetchone()
    return row["bolna_agent_id"] if row else None


def agent_refused(configured, named) -> bool:
    """With a configured agent id, an event must name exactly that agent (a missing agent id
    is refused too). Without one, nothing is refused here."""
    return configured is not None and named != configured


def agent_mismatch(conn, clinic_id, minimized) -> bool:
    return agent_refused(clinic_agent(conn, clinic_id), minimized.get("agent_id"))


def record(conn, clinic_id, minimized) -> bool:
    """Store one delivery and enqueue its webhook.process job in one transaction, and commit.
    Returns False for a duplicate (nothing new is written)."""
    inbox_id = uuid4()
    row = conn.execute(
        """INSERT INTO webhook_inbox (id,clinic_id,event_key,execution_id,provider_status,payload)
        VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (clinic_id, event_key) DO NOTHING RETURNING id""",
        (
            inbox_id,
            clinic_id,
            event_key(minimized),
            minimized["execution_id"],
            minimized["status"],
            Jsonb(minimized),
        ),
    ).fetchone()
    if row is None:
        conn.commit()
        return False
    conn.execute(
        "INSERT INTO outbox (id,clinic_id,kind,payload) VALUES (%s,%s,'webhook.process',%s)",
        (uuid4(), clinic_id, Jsonb({"inbox_id": str(inbox_id)})),
    )
    conn.commit()
    return True


def lock_session(conn, clinic_id, execution_id):
    return conn.execute(
        f"SELECT {SESSION_COLUMNS} FROM call_sessions"  # noqa: S608
        " WHERE clinic_id=%s AND execution_id=%s FOR NO KEY UPDATE",
        (clinic_id, execution_id),
    ).fetchone()


def _event_at(minimized):
    value = minimized.get("updated_at")
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def apply(conn, clinic_id, session, minimized, configured_agent=None) -> str:
    """Apply a minimized status to a call session locked by the caller (FOR NO KEY UPDATE);
    does not commit. `configured_agent` is the clinic's bolna_agent_id. Returns an outcome
    code: applied, stale, unknown_status or agent_mismatch.

    Equal-rank non-final events (a repeated or lateral status) apply only when their
    updated_at is at least the latest one applied (provider_event_at); one without
    updated_at applies only if none was recorded yet."""
    status = minimized.get("status")
    rank = STATUS_RANK.get(status)
    if rank is None:
        return "unknown_status"
    named = minimized.get("agent_id")
    if agent_refused(configured_agent, named):
        return "agent_mismatch"
    if session["agent_id"] and named and named != session["agent_id"]:
        return "agent_mismatch"
    event_at = _event_at(minimized)
    if session["provider_final"]:
        if status != session["provider_status"]:
            return "stale"
        # Same final status again: only fill dispositions that are still empty.
        sets = [
            f"{column}=COALESCE({column}, %({key})s::{kind})"
            for key, (column, kind) in DISPOSITION.items()
        ]
    else:
        current = STATUS_RANK.get(session["provider_status"], 0)
        if rank < current:
            return "stale"
        last = session["provider_event_at"]
        if rank == current and last is not None and (event_at is None or event_at < last):
            return "stale"
        sets = [
            f"{column}=COALESCE(%({key})s::{kind}, {column})"
            for key, (column, kind) in DISPOSITION.items()
        ]
        sets += [
            "provider_status=%(status)s",
            "provider_final=%(final)s",
            "provider_updated_at=now()",
            "provider_event_at=GREATEST(provider_event_at, %(event_at)s::timestamptz)",
            "status=CASE WHEN %(ended)s THEN 'ended' ELSE status END",
            "ended_at=CASE WHEN %(ended)s THEN COALESCE(ended_at, now()) ELSE ended_at END",
            "next_reconcile_at=CASE WHEN %(final)s THEN NULL ELSE next_reconcile_at END",
        ]
    params = {key: minimized.get(key) for key in DISPOSITION}
    params |= {
        "status": status,
        "event_at": event_at,
        "final": rank >= FINAL_RANK,
        "ended": rank >= ENDED_RANK,
        "clinic": clinic_id,
        "id": session["id"],
    }
    conn.execute(
        f"UPDATE call_sessions SET {', '.join(sets)}"  # noqa: S608
        " WHERE clinic_id=%(clinic)s AND id=%(id)s",
        params,
    )
    return "applied"


def enqueue_reconciles(conn, after: timedelta, limit: int) -> int:
    """Housekeeping: enqueue a bolna.reconcile job for at most `limit` sessions without a
    final status whose start and last provider update are both older than `after` and whose
    previous poll's backoff has passed; and commit. Each poll doubles the next delay (capped
    at 24 h) and at most MAX_RECONCILES polls are made per session. Only clinics with a
    configured bolna_agent_id are reconciled: the shared Bolna account answers for any
    execution id, so the agent id is what ties a polled execution to the clinic. Cross-tenant
    like the worker; each job carries its session's clinic_id. Sessions locked elsewhere are
    skipped."""
    rows = conn.execute(
        """WITH due AS (
            SELECT s.clinic_id, s.id FROM call_sessions s
            JOIN clinics c ON c.id = s.clinic_id AND c.bolna_agent_id IS NOT NULL
            WHERE NOT s.provider_final AND s.reconcile_count < %(max)s
            AND GREATEST(s.started_at + %(after)s, s.provider_updated_at + %(after)s,
                s.next_reconcile_at) <= now()
            ORDER BY s.started_at LIMIT %(limit)s
            FOR NO KEY UPDATE OF s SKIP LOCKED
        ), bumped AS (
            UPDATE call_sessions s SET reconcile_count = s.reconcile_count + 1,
                next_reconcile_at = now()
                    + LEAST(%(after)s * power(2, s.reconcile_count), %(cap)s)
            FROM due WHERE s.clinic_id = due.clinic_id AND s.id = due.id
            RETURNING s.clinic_id, s.id
        )
        INSERT INTO outbox (id, clinic_id, kind, payload)
        SELECT gen_random_uuid(), clinic_id, 'bolna.reconcile',
            jsonb_build_object('session_id', id)
        FROM bumped RETURNING id""",
        {"max": MAX_RECONCILES, "after": after, "limit": limit, "cap": MAX_RECONCILE_DELAY},
    ).fetchall()
    conn.commit()
    if rows:
        log.info("reconciliation enqueued count=%d", len(rows))
    return len(rows)


# Opt-out from a call disposition (T9): a documented hook, deliberately inert. Bolna documents
# no opt-out field or disposition value: termination_reason, hangup_reason and hangup_by are
# not documented as fixed codes (docs/SOURCE_REVIEW.md), and the free-text and
# extracted_data fields are never stored. Nothing is inferred from them. Once Bolna documents
# such a field (or the owner approves a contract-tested extraction schema), map it here and
# record it with reminders.opt_out(..., source="webhook", appointment_id=<the reminder
# call's appointment>) in the webhook.process transaction (never patient-wide: only staff
# write patient-wide opt-outs).
OPT_OUT_DISPOSITIONS: frozenset[str] = frozenset()


def disposition_opts_out(minimized: dict) -> bool:
    """Whether a minimized execution says the callee opted out. Always False today (see
    OPT_OUT_DISPOSITIONS)."""
    return minimized.get("termination_reason") in OPT_OUT_DISPOSITIONS
