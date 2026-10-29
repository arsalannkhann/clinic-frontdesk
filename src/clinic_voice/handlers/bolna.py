"""Bolna handlers: webhook.process and bolna.reconcile (T8).

webhook.process (payload {inbox_id}) applies one stored, minimized webhook event to its call
session. It counts the attempt in its own committed statement, then locks the inbox row, the
call session row and (fenced) its own outbox row, in that order (locks.py), and commits the
session update, the inbox outcome and the job completion together. An event that is no
longer 'received' (already settled, or deleted by retention) completes the job unchanged,
so retries and duplicate jobs are harmless.

bolna.reconcile (payload {session_id}) polls GET /executions/{id} through Ports.bolna with
the connection idle, then applies the result with the same monotonic rule. The execution id
is caller-asserted and the Bolna account is shared, so the result is applied only when its
agent id equals the clinic's configured bolna_agent_id; a clinic without one is never
polled, and a mismatch completes the job without writing anything ('agent_mismatch'). Provider errors
follow the outbox rules (provider_failure): a timeout or retryable error retries with
backoff and changes nothing; the read itself is safe to repeat.
"""

import logging

from clinic_voice.bolna import webhooks
from clinic_voice.domain import as_uuid
from clinic_voice.outbox import (
    complete,
    fail_dead,
    mark_done,
    provider_failure,
    require_idle,
)
from clinic_voice.providers.calendar import ProviderError

log = logging.getLogger("clinic_voice.worker")


def _settle(conn, job, outcome) -> bool:
    if not mark_done(conn, job):
        conn.rollback()
        return False
    conn.commit()
    log.info("bolna job done id=%s kind=%s outcome=%s", job.id, job.kind, outcome)
    return True


# webhook.process


def process_webhook(conn, job, ports) -> bool:
    inbox_id = as_uuid((job.payload or {}).get("inbox_id"))
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
        configured = webhooks.clinic_agent(conn, job.clinic_id)
        outcome = webhooks.apply(conn, job.clinic_id, session, event["payload"], configured)
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
    inbox_id = as_uuid((job.payload or {}).get("inbox_id"))
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
    session_id = as_uuid((job.payload or {}).get("session_id"))
    if session_id is None:
        return fail_dead(conn, job, "bad_payload")
    row = conn.execute(
        "SELECT s.execution_id, s.provider_final, c.bolna_agent_id FROM call_sessions s"
        " JOIN clinics c ON c.id = s.clinic_id WHERE s.clinic_id=%s AND s.id=%s",
        (job.clinic_id, session_id),
    ).fetchone()
    conn.commit()
    if row is None or row["provider_final"]:
        return complete(conn, job)  # Purged, or a webhook already delivered the final status.
    configured = row["bolna_agent_id"]
    if configured is None:
        # The Bolna account is shared: without the clinic's agent id nothing ties the
        # (caller-asserted) execution id to this clinic, so do not ask the provider.
        log.info("bolna job done id=%s kind=%s outcome=agent_unconfigured", job.id, job.kind)
        return complete(conn, job)
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
    if webhooks.agent_refused(configured, minimized.get("agent_id")):
        # Another clinic's (or an unknown agent's) execution: write nothing.
        log.warning("bolna job done id=%s kind=%s outcome=agent_mismatch", job.id, job.kind)
        return complete(conn, job)
    session = webhooks.lock_session(conn, job.clinic_id, row["execution_id"])
    if session is None:
        conn.rollback()
        return complete(conn, job)
    outcome = webhooks.apply(conn, job.clinic_id, session, minimized, configured)
    return _settle(conn, job, outcome)
