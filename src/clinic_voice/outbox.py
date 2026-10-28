"""Durable outbox processing: claims, leases, fencing, retries and the worker loop (T4).

Jobs are outbox rows. A worker claims due jobs in one short transaction (FOR UPDATE SKIP
LOCKED), which sets a lease (owner + expiry) and increments attempts, and commits before any
provider I/O. Every later state change is fenced on (id, clinic_id, lease_owner, attempts,
status='processing'): if another worker reclaimed the job after this one's lease expired, the
fence matches nothing and the stale worker must stop. Attempts never decrease (requeue and
release() on shutdown extend max_attempts instead), so a fence value is never reused by a
later claim.

The lease is renewed just before a job starts and by handlers between provider calls
(renew()). Adapter timeouts must therefore stay well under the lease: check_ports() rejects a
calendar adapter whose `timeout_seconds` exceeds lease / LEASE_TIMEOUT_RATIO.

Provider I/O always runs with the connection idle (no open transaction). `last_error` holds
fixed short codes from this package, never provider text, and logs carry job id, kind and a
code or exception type only. Workers act across tenants, but each job carries its clinic_id
and every per-job statement filters on it.

Handlers live in clinic_voice.handlers (registry HANDLERS: kind -> Handler(run, on_dead)).
Kinds without a handler are dead-lettered with 'unknown_kind'.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta
from threading import Event
from typing import Any
from uuid import UUID

from psycopg import Error as DatabaseError
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from clinic_voice.db import connect
from clinic_voice.providers.calendar import (
    CalendarPort,
    FakeCalendar,
    ProviderError,
    ProviderPermanent,
    ProviderRetryable,
    ProviderTimeout,
)

log = logging.getLogger("clinic_voice.worker")

DEFAULT_LEASE = timedelta(minutes=2)
DEFAULT_MAX_ATTEMPTS = 8
BASE_DELAY = timedelta(seconds=30)
MAX_DELAY = timedelta(hours=1)
# Upper bound on a provider-requested Retry-After, so a bogus value cannot park a job.
MAX_PROVIDER_DELAY = timedelta(hours=1)
# A single provider call must fit in the lease many times over; handlers renew between calls.
LEASE_TIMEOUT_RATIO = 4
MAX_ERROR_DELAY = 30.0


@dataclass(frozen=True)
class Job:
    id: UUID
    clinic_id: UUID
    appointment_id: UUID | None
    appointment_version: int
    kind: str
    attempts: int
    max_attempts: int
    payload: dict[str, Any]
    lease_owner: str
    lease: timedelta = DEFAULT_LEASE


@dataclass(frozen=True)
class Ports:
    """Provider ports for handlers. Either one calendar adapter for every job (`calendar`,
    e.g. the FakeCalendar) or a resolver `calendar_for`.

    A resolver is called as calendar_for(conn, clinic_id, doctor_id, account_id) and returns
    (port, account_id) or None. With account_id set it must use exactly that account (the
    one recorded as having written an event) and return None unless it is active; without
    it, the doctor's account mapping decides. None means no usable account; handlers treat
    that as unmapped. A resolver may read the database but must leave the connection idle
    (committed), since provider I/O follows. It must declare `timeout_seconds` for its
    adapters (checked by check_ports). Optional resolver methods: mark_error(conn,
    clinic_id, account_id) when the provider rejected the account's credentials, and
    revoke_grant(conn, clinic_id, account_id) for calendar.revoke_grant jobs."""

    calendar: CalendarPort | None = None
    calendar_for: Callable[..., tuple[CalendarPort, UUID | None] | None] | None = None

    def __post_init__(self):
        if (self.calendar is None) == (self.calendar_for is None):
            raise ValueError("Ports needs exactly one of calendar or calendar_for")

    def calendar_port(
        self, conn, clinic_id, doctor_id, account_id=None
    ) -> tuple[CalendarPort, UUID | None] | None:
        if self.calendar_for is None:
            return self.calendar, None
        resolved = self.calendar_for(conn, clinic_id, doctor_id, account_id)
        require_idle(conn)
        return resolved

    def account_failed(self, conn, clinic_id, account_id) -> None:
        mark = getattr(self.calendar_for, "mark_error", None)
        if mark is not None and account_id is not None:
            mark(conn, clinic_id, account_id)
            require_idle(conn)

    def grant_revoker(self):
        return getattr(self.calendar_for, "revoke_grant", None)


@dataclass(frozen=True)
class Handler:
    run: Callable[[Any, Job, Ports], bool]
    # Runs inside the dead-lettering transaction (no commit), e.g. to flag an appointment.
    on_dead: Callable[[Any, Job], None] | None = None


def _handlers() -> dict[str, Handler]:
    from clinic_voice.handlers import HANDLERS  # Late import: handlers import this module.

    return HANDLERS


def check_ports(ports, lease=DEFAULT_LEASE):
    """Every adapter (or resolver) except the in-process FakeCalendar must declare a numeric
    `timeout_seconds` (not a bool), and it must be at most lease / LEASE_TIMEOUT_RATIO."""
    adapter = ports.calendar if ports.calendar is not None else ports.calendar_for
    if isinstance(adapter, FakeCalendar):
        return
    timeout = getattr(adapter, "timeout_seconds", None)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
        raise ValueError("calendar adapter must declare a positive timeout_seconds")
    if timeout * LEASE_TIMEOUT_RATIO > lease.total_seconds():
        raise ValueError(
            f"calendar timeout {timeout}s is too long for a {lease.total_seconds():.0f}s lease"
        )


def backoff(attempts: int) -> timedelta:
    """min(30 s * 2^(attempts-1), 1 h), jitter-free."""
    # Clamp the exponent: 2**7 * 30 s already exceeds the cap, and large exponents overflow.
    return min(BASE_DELAY * 2 ** min(max(attempts - 1, 0), 8), MAX_DELAY)


def error_delay(failures: int) -> float:
    """Seconds the loop waits after consecutive database failures: 1, 2, 4 ... 30."""
    return min(2.0 ** min(max(failures - 1, 0), 8), MAX_ERROR_DELAY)


def _job(row, lease) -> Job:
    return Job(
        id=row["id"],
        clinic_id=row["clinic_id"],
        appointment_id=row["appointment_id"],
        appointment_version=row["appointment_version"],
        kind=row["kind"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        payload=row["payload"],
        lease_owner=row["lease_owner"],
        lease=lease,
    )


def require_idle(conn):
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise RuntimeError("provider I/O attempted inside an open transaction")


def _run_on_dead(conn, job):
    handler = _handlers().get(job.kind)
    if handler is not None and handler.on_dead is not None:
        handler.on_dead(conn, job)


def claim(conn, worker_id, limit=10, lease=DEFAULT_LEASE, kinds=None) -> list[Job]:
    """Lease up to `limit` due jobs to worker_id and commit (database clock throughout).

    Due: pending with available_at <= now(), or processing with an expired lease. An expired
    job that already used all its attempts is dead-lettered ('lease_expired') instead of
    reclaimed, so a job that keeps killing workers cannot loop forever.
    """
    require_idle(conn)
    params = {
        "kinds": list(kinds) if kinds is not None else None,
        "limit": limit,
        "worker": worker_id,
        "lease": lease,
    }
    kind_filter = "(%(kinds)s::text[] IS NULL OR kind = ANY(%(kinds)s::text[]))"
    exhausted = conn.execute(
        f"""WITH stale AS (
            SELECT id FROM outbox
            WHERE status = 'processing' AND lease_expires_at < now()
            AND attempts >= max_attempts AND {kind_filter}
            ORDER BY lease_expires_at LIMIT %(limit)s
            FOR UPDATE SKIP LOCKED
        )
        UPDATE outbox o SET status = 'dead', last_error = 'lease_expired',
            lease_expires_at = NULL, updated_at = now()
        FROM stale WHERE o.id = stale.id
        RETURNING o.*""",  # noqa: S608
        params,
    ).fetchall()
    for row in exhausted:
        job = _job(row, lease)
        _run_on_dead(conn, job)
        log.warning("job dead-lettered id=%s kind=%s code=lease_expired", job.id, job.kind)
    rows = conn.execute(
        f"""WITH due AS (
            SELECT id FROM outbox
            WHERE ((status = 'pending' AND available_at <= now())
                OR (status = 'processing' AND lease_expires_at < now()
                    AND attempts < max_attempts))
            AND {kind_filter}
            ORDER BY available_at
            LIMIT %(limit)s
            FOR UPDATE SKIP LOCKED
        )
        UPDATE outbox o SET status = 'processing', attempts = o.attempts + 1,
            lease_owner = %(worker)s, lease_expires_at = now() + %(lease)s, updated_at = now()
        FROM due WHERE o.id = due.id
        RETURNING o.*""",  # noqa: S608
        params,
    ).fetchall()
    conn.commit()
    rows.sort(key=lambda r: (r["available_at"], r["id"]))
    return [_job(r, lease) for r in rows]


FENCE = "id=%s AND clinic_id=%s AND lease_owner=%s AND attempts=%s AND status='processing'"


def _fence(job):
    return (job.id, job.clinic_id, job.lease_owner, job.attempts)


def renew(conn, job) -> bool:
    """Heartbeat: extend the lease by job.lease from now and commit. False = lease lost; the
    caller must stop without further side effects."""
    require_idle(conn)
    row = conn.execute(
        "UPDATE outbox SET lease_expires_at=now() + %s, updated_at=now()"
        f" WHERE {FENCE} RETURNING id",  # noqa: S608
        (job.lease, *_fence(job)),
    ).fetchone()
    conn.commit()
    return row is not None


def remember(conn, job, **values):
    """Merge values into the job's payload and renew its lease, fenced, and commit. Returns
    the updated Job, or None if the lease was lost. Used to record write intent (such as the
    target calendar) before a provider write, so later attempts act on the same target."""
    require_idle(conn)
    row = conn.execute(
        "UPDATE outbox SET payload = payload || %s, lease_expires_at=now() + %s, updated_at=now()"
        f" WHERE {FENCE} RETURNING payload",  # noqa: S608
        (Jsonb(values), job.lease, *_fence(job)),
    ).fetchone()
    conn.commit()
    return None if row is None else replace(job, payload=row["payload"])


def mark_done(conn, job) -> bool:
    """Fenced update inside the caller's transaction; does not commit."""
    row = conn.execute(
        "UPDATE outbox SET status='done', lease_expires_at=NULL, last_error=NULL,"
        f" updated_at=now() WHERE {FENCE} RETURNING id",  # noqa: S608
        _fence(job),
    ).fetchone()
    return row is not None


def complete(conn, job) -> bool:
    """Mark the job done. False means the lease was lost: do no further side effects."""
    done = mark_done(conn, job)
    conn.commit()
    return done


def fail_dead(conn, job, error_code) -> bool:
    """Dead-letter the job and run its handler's on_dead hook in the same transaction."""
    row = conn.execute(
        "UPDATE outbox SET status='dead', lease_expires_at=NULL, last_error=%s, updated_at=now()"
        f" WHERE {FENCE} RETURNING id",  # noqa: S608
        (error_code, *_fence(job)),
    ).fetchone()
    if row is None:
        conn.rollback()
        return False
    _run_on_dead(conn, job)
    conn.commit()
    log.warning("job dead-lettered id=%s kind=%s code=%s", job.id, job.kind, error_code)
    return True


def retry(conn, job, error_code, delay=None) -> bool:
    """Return the job to pending after a delay (backoff by attempts unless given, capped at
    an hour). Once attempts reach max_attempts the job is dead-lettered instead."""
    if job.attempts >= job.max_attempts:
        return fail_dead(conn, job, error_code)
    if delay is None:
        delay = backoff(job.attempts)
    delay = min(max(delay, timedelta(0)), MAX_PROVIDER_DELAY)
    row = conn.execute(
        "UPDATE outbox SET status='pending', lease_expires_at=NULL, last_error=%s,"
        f" available_at=now() + %s, updated_at=now() WHERE {FENCE} RETURNING id",  # noqa: S608
        (error_code, delay, *_fence(job)),
    ).fetchone()
    conn.commit()
    if row is not None:
        log.info("job retry scheduled id=%s kind=%s code=%s", job.id, job.kind, error_code)
    return row is not None


def release(conn, job) -> bool:
    """Give back a claimed job that never started (worker stopping): pending and due now.
    attempts stays (monotonic fences); max_attempts grows by one so the unused claim costs
    nothing. Fenced, so a job already reclaimed elsewhere is untouched."""
    row = conn.execute(
        "UPDATE outbox SET status='pending', max_attempts=max_attempts + 1, lease_expires_at=NULL,"
        f" available_at=now(), updated_at=now() WHERE {FENCE} RETURNING id",  # noqa: S608
        _fence(job),
    ).fetchone()
    conn.commit()
    return row is not None


def provider_failure(conn, job, error: ProviderError) -> bool:
    """Map a provider error to retry/dead. Codes are fixed here, never taken from the error."""
    if isinstance(error, ProviderTimeout):
        return retry(conn, job, "timeout")
    if isinstance(error, ProviderRetryable):
        delay = None if error.retry_after is None else timedelta(seconds=error.retry_after)
        return retry(conn, job, "retryable", delay)
    if isinstance(error, ProviderPermanent):
        return fail_dead(conn, job, "provider_permanent")
    return retry(conn, job, "provider_error")


def _broken(conn) -> bool:
    return conn.closed or conn.broken


def process(conn, job, ports) -> bool:
    """Run one claimed job. Handler failures never propagate: unexpected exceptions retry
    the job with code 'internal'. Returns False if the lease was lost."""
    handler = _handlers().get(job.kind)
    if handler is None:
        return fail_dead(conn, job, "unknown_kind")
    if not renew(conn, job):
        log.info("lease lost before start id=%s kind=%s", job.id, job.kind)
        return False
    try:
        return handler.run(conn, job, ports)
    except Exception as error:
        # Log the type only: exception text may carry provider or database detail.
        log.error(
            "job handler failed id=%s kind=%s error=%s", job.id, job.kind, type(error).__name__
        )
        if _broken(conn):
            raise
        conn.rollback()
        return retry(conn, job, "internal")


def run_batch(conn, worker_id, ports, limit=10, lease=DEFAULT_LEASE, kinds=None, stop=None):
    """Claim and process one batch on an open connection; returns how many were claimed.
    If `stop` is set between jobs, the jobs not yet started are released. Raises if the
    connection breaks (the caller reconnects; leases expire and jobs are reclaimed)."""
    jobs = claim(conn, worker_id, limit, lease, kinds)
    for index, job in enumerate(jobs):
        if stop is not None and stop.is_set():
            for pending in jobs[index:]:
                release(conn, pending)
            break
        try:
            process(conn, job, ports)
        except Exception as error:
            log.error(
                "job bookkeeping failed id=%s kind=%s error=%s",
                job.id,
                job.kind,
                type(error).__name__,
            )
            if _broken(conn):
                raise
            conn.rollback()
    return len(jobs)


def run_once(url, worker_id, ports, limit=10, lease=DEFAULT_LEASE, kinds=None) -> int:
    """Open a connection, process one batch and close it."""
    check_ports(ports, lease)
    with connect(url) as conn:
        return run_batch(conn, worker_id, ports, limit, lease, kinds)


HOUSEKEEPING_INTERVAL = 600.0


def _default_housekeeping(conn):
    from clinic_voice.calendar_accounts import prune_oauth_states

    prune_oauth_states(conn)


def run_loop(
    url,
    worker_id,
    ports,
    stop: Event,
    interval=2.0,
    limit=10,
    lease=DEFAULT_LEASE,
    connect_fn=connect,
    wait=None,
    housekeeping=None,
    clock=time.monotonic,
):
    """Process batches until `stop` is set, finishing (or releasing) the current batch.

    One connection is reused across idle polls. Database errors close it and back off
    exponentially (1 s doubling to 30 s) before reconnecting. When idle, `housekeeping(conn)`
    (default: prune expired OAuth states) runs at most every HOUSEKEEPING_INTERVAL seconds.
    """
    check_ports(ports, lease)
    wait = wait or stop.wait
    housekeeping = housekeeping or _default_housekeeping
    conn, failures, last_housekeeping = None, 0, None
    try:
        while not stop.is_set():
            try:
                if conn is None or _broken(conn):
                    conn = connect_fn(url)
                count = run_batch(conn, worker_id, ports, limit, lease, stop=stop)
                failures = 0
            except DatabaseError as error:
                failures += 1
                log.error(
                    "worker batch failed error=%s failures=%d", type(error).__name__, failures
                )
                if conn is not None:
                    conn.close()
                conn = None
                wait(error_delay(failures))
                continue
            if count == 0:
                now = clock()
                if last_housekeeping is None or now - last_housekeeping >= HOUSEKEEPING_INTERVAL:
                    last_housekeeping = now
                    try:
                        housekeeping(conn)
                    except Exception as error:
                        # Housekeeping must never stop job processing; log the type only.
                        log.error("housekeeping failed error=%s", type(error).__name__)
                        if _broken(conn) or isinstance(error, DatabaseError):
                            conn.close()
                            conn = None
                wait(interval)
    finally:
        if conn is not None:
            conn.close()
