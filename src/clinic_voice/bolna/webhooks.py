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
from datetime import timedelta
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
    "id, execution_id, agent_id, provider_status, provider_final, termination_reason,"
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


def agent_mismatch(conn, clinic_id, minimized) -> bool:
    """True when the clinic has a configured Bolna agent id and the payload names another."""
    row = conn.execute("SELECT bolna_agent_id FROM clinics WHERE id=%s", (clinic_id,)).fetchone()
    configured = row["bolna_agent_id"] if row else None
    named = minimized.get("agent_id")
    return bool(configured and named and named != configured)


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


def apply(conn, clinic_id, session, minimized) -> str:
    """Apply a minimized status to a call session locked by the caller (FOR NO KEY UPDATE);
    does not commit. Returns an outcome code: applied, stale, unknown_status or
    agent_mismatch."""
    status = minimized.get("status")
    rank = STATUS_RANK.get(status)
    if rank is None:
        return "unknown_status"
    named = minimized.get("agent_id")
    if session["agent_id"] and named and named != session["agent_id"]:
        return "agent_mismatch"
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
        sets = [
            f"{column}=COALESCE(%({key})s::{kind}, {column})"
            for key, (column, kind) in DISPOSITION.items()
        ]
        sets += [
            "provider_status=%(status)s",
            "provider_final=%(final)s",
            "provider_updated_at=now()",
            "status=CASE WHEN %(ended)s THEN 'ended' ELSE status END",
            "ended_at=CASE WHEN %(ended)s THEN COALESCE(ended_at, now()) ELSE ended_at END",
            "next_reconcile_at=CASE WHEN %(final)s THEN NULL ELSE next_reconcile_at END",
        ]
    params = {key: minimized.get(key) for key in DISPOSITION}
    params |= {
        "status": status,
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
    at 24 h) and at most MAX_RECONCILES polls are made per session. Cross-tenant like the
    worker; each job carries its session's clinic_id. Sessions locked elsewhere are skipped."""
    rows = conn.execute(
        """WITH due AS (
            SELECT clinic_id, id FROM call_sessions
            WHERE NOT provider_final AND reconcile_count < %(max)s
            AND GREATEST(started_at + %(after)s, provider_updated_at + %(after)s,
                next_reconcile_at) <= now()
            ORDER BY started_at LIMIT %(limit)s
            FOR NO KEY UPDATE SKIP LOCKED
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
