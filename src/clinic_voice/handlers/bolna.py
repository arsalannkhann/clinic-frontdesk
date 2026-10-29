"""Bolna handlers: webhook.process and bolna.reconcile (T8).

webhook.process (payload {inbox_id}) applies one stored, minimized webhook event to its call
session. It counts the attempt in its own committed statement, then locks the inbox row, the
call session row and (fenced) its own outbox row, in that order (locks.py), and commits the
session update, the inbox outcome and the job completion together. An event that is no
longer 'received' (already settled, or deleted by retention) completes the job unchanged,
so retries and duplicate jobs are harmless.

bolna.reconcile (payload {session_id}) polls GET /executions/{id} through Ports.bolna with
the connection idle, then applies the result with the same monotonic rule. Provider errors
follow the outbox rules (provider_failure): a timeout or retryable error retries with
backoff and changes nothing; the read itself is safe to repeat.
"""

import logging
from uuid import UUID

from clinic_voice.bolna import webhooks
from clinic_voice.outbox import (
    complete,
    fail_dead,
    mark_done,
    provider_failure,
    require_idle,
)
from clinic_voice.providers.calendar import ProviderError

log = logging.getLogger("clinic_voice.worker")


def _uuid(value):
    try:
        return None if value is None else UUID(str(value))
    except ValueError:
        return None


def _settle(conn, job, outcome) -> bool:
    if not mark_done(conn, job):
        conn.rollback()
        return False
    conn.commit()
    log.info("bolna job done id=%s kind=%s outcome=%s", job.id, job.kind, outcome)
    return True


# webhook.process


def process_webhook(conn, job, ports) -> bool:
    inbox_id = _uuid((job.payload or {}).get("inbox_id"))
    if inbox_id is None:
        return fail_dead(conn, job, "bad_payload")
    counted = conn.execute(
        "UPDATE webhook_inbox SET attempts=attempts + 1"
        " WHERE clinic_id=%s AND id=%s AND status='received' RETURNING id",
        (job.clinic_id, inbox_id),
    ).fetchone()
    conn.commit()
    if counted is None:
        return complete(conn, job)
    event = conn.execute(
        "SELECT execution_id, payload FROM webhook_inbox"
        " WHERE clinic_id=%s AND id=%s AND status='received' FOR UPDATE",
        (job.clinic_id, inbox_id),
    ).fetchone()
    if event is None:
        conn.rollback()
        return complete(conn, job)
    session = webhooks.lock_session(conn, job.clinic_id, event["execution_id"])
    if session is None:
        outcome = "unknown_execution"
    else:
        outcome = webhooks.apply(conn, job.clinic_id, session, event["payload"])
    conn.execute(
        "UPDATE webhook_inbox SET status=%s, outcome=%s, processed_at=now()"
        " WHERE clinic_id=%s AND id=%s",
        ("processed" if outcome == "applied" else "ignored", outcome, job.clinic_id, inbox_id),
    )
    return _settle(conn, job, outcome)


def webhook_failed_on_dead(conn, job):
    """Mark the event failed when its job is dead-lettered. Runs with the outbox row already
    locked, so it skips an inbox row a (stale) processor holds rather than wait on it; that
    event then stays 'received' and a requeue processes it."""
    inbox_id = _uuid((job.payload or {}).get("inbox_id"))
    if inbox_id is None:
        return
    conn.execute(
        """UPDATE webhook_inbox SET status='failed', processed_at=now()
        WHERE id IN (SELECT id FROM webhook_inbox WHERE clinic_id=%s AND id=%s
            AND status='received' FOR UPDATE SKIP LOCKED)""",
        (job.clinic_id, inbox_id),
    )


# bolna.reconcile


def reconcile(conn, job, ports) -> bool:
    session_id = _uuid((job.payload or {}).get("session_id"))
    if session_id is None:
        return fail_dead(conn, job, "bad_payload")
    row = conn.execute(
        "SELECT execution_id, provider_final FROM call_sessions WHERE clinic_id=%s AND id=%s",
        (job.clinic_id, session_id),
    ).fetchone()
    conn.commit()
    if row is None or row["provider_final"]:
        return complete(conn, job)  # Purged, or a webhook already delivered the final status.
    port = getattr(ports, "bolna", None)
    if port is None:
        return fail_dead(conn, job, "bolna_unconfigured")
    require_idle(conn)
    try:
        execution = port.get_execution(row["execution_id"])
    except ProviderError as error:
        return provider_failure(conn, job, error)
    except ValueError:
        return fail_dead(conn, job, "bad_execution_id")
    if execution is None:
        log.info("bolna job done id=%s kind=%s outcome=not_found", job.id, job.kind)
        return complete(conn, job)
    minimized = execution.minimized()
    if minimized.get("execution_id") != row["execution_id"]:
        return fail_dead(conn, job, "execution_mismatch")
    session = webhooks.lock_session(conn, job.clinic_id, row["execution_id"])
    if session is None:
        conn.rollback()
        return complete(conn, job)
    outcome = webhooks.apply(conn, job.clinic_id, session, minimized)
    return _settle(conn, job, outcome)
