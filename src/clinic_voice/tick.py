"""Serverless outbox processing: the bounded worker tick and the inline confirm (ADR 0010).

On serverless hosting there is no long-running `clinic-voice worker`. A scheduler (Supabase
pg_cron through pg_net, every minute) calls POST /v1/internal/worker/tick, which claims and
runs jobs until a time budget is spent and then runs the bounded housekeeping. Everything
goes through outbox.run_batch / outbox.process, the same claim, lease, fencing, retry and
dead-letter code as the CLI worker; only the loop differs (a deadline instead of a stop
signal; between jobs, unstarted jobs of a batch are released when the budget is spent).

Ports, built from settings:
- calendar: providers.ledger.LedgerResolver, wrapping the Google resolver when Google is
  configured (GOOGLE_* and TOKEN_ENCRYPTION_KEYS). Without Google, a job for a doctor that
  is not ledger-mapped resolves to no calendar and dead-letters 'calendar_unmapped' for
  staff, exactly as the CLI worker does for an unmapped doctor.
- bolna: the live BolnaClient when BOLNA_API_KEY is set; otherwise None, so bolna.reconcile,
  reminder.call, reminder.stop and provision.step jobs are not claimed here and wait
  (outbox.excluded_kinds), and housekeeping enqueues no reconciliation.
- messaging: None (no live provider exists), so notification.send jobs wait unclaimed.

confirm_inline runs one booking's own calendar.create job right after the booking committed,
so a public booking for a ledger-mapped doctor normally answers 'confirmed'. It claims only
that job (outbox.claim ids=...) and runs it through outbox.process; it never inlines a
Google-mapped doctor (provider I/O stays in the tick). Any failure leaves the job to the
tick and the booking answers 'pending_sync'.
"""

import logging
import os
import socket
import time
from threading import Lock

from clinic_voice.db import connect
from clinic_voice.outbox import (
    DEFAULT_LEASE,
    Ports,
    _default_housekeeping,
    check_ports,
    claim,
    process,
    run_batch,
)
from clinic_voice.providers.ledger import LedgerResolver, is_ledger

log = logging.getLogger("clinic_voice.worker")

TICK_BATCH = 5

_ports_lock = Lock()
_ports_cache: dict[int, Ports] = {}


class Deadline:
    """A stop flag for outbox.run_batch that becomes set when the budget is spent."""

    def __init__(self, seconds: float, clock=time.monotonic):
        self._clock = clock
        self._end = clock() + seconds

    def is_set(self) -> bool:
        return self._clock() >= self._end


def worker_id(kind: str) -> str:
    return f"{kind}:{socket.gethostname()}:{os.getpid()}"[:128]


def build_ports(settings) -> Ports:
    """Ports for the tick and inline confirm (see the module docstring)."""
    import httpx

    inner = None
    if settings.google_configured():
        from clinic_voice.calendar_accounts import GoogleCalendarResolver
        from clinic_voice.providers.google_calendar import DEFAULT_TIMEOUT

        try:
            inner = GoogleCalendarResolver(
                settings, httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=False)
            )
        except Exception as error:  # Invalid keyring: serve ledger doctors only.
            log.error("google calendar resolver unavailable error=%s", type(error).__name__)
    bolna = None
    key = settings.bolna_api_key
    if key is not None and key.get_secret_value():
        from clinic_voice.providers.bolna import DEFAULT_TIMEOUT as BOLNA_TIMEOUT
        from clinic_voice.providers.bolna import BolnaClient

        bolna = BolnaClient(
            key.get_secret_value(),
            httpx.Client(timeout=BOLNA_TIMEOUT, follow_redirects=False),
            settings.bolna_api_base,
        )
    return Ports(calendar_for=LedgerResolver(inner), bolna=bolna)


def ports_for(settings) -> Ports:
    """build_ports, cached per Settings object (so Google access tokens and HTTP clients
    are reused across requests of one instance)."""
    with _ports_lock:
        ports = _ports_cache.get(id(settings))
        if ports is None:
            _ports_cache.clear()
            ports = _ports_cache[id(settings)] = build_ports(settings)
        return ports


def run_tick(
    url, ports, budget_seconds, housekeeping=None, clock=time.monotonic, lease=DEFAULT_LEASE
):
    """Claim and run job batches until the budget is spent or nothing is due, then run
    housekeeping (default: the worker's, outbox._default_housekeeping). Returns counts only:
    claimed (jobs leased by this tick, including any released unstarted when the budget ran
    out) and housekeeping ('ran' or 'failed')."""
    check_ports(ports, lease)
    deadline = Deadline(budget_seconds, clock)
    owner = worker_id("tick")
    claimed = 0
    with connect(url) as conn:
        while not deadline.is_set():
            count = run_batch(conn, owner, ports, TICK_BATCH, lease, stop=deadline)
            claimed += count
            if count == 0:
                break
        try:
            (housekeeping or _default_housekeeping)(conn, ports)
            outcome = "ran"
        except Exception as error:
            # Never fail the tick for housekeeping; log the type only.
            log.error("housekeeping failed error=%s", type(error).__name__)
            outcome = "failed"
    return {"claimed": claimed, "housekeeping": outcome}


def confirm_inline(url, ports, clinic_id, appointment_id, budget_seconds) -> str:
    """Run the appointment's pending calendar.create job now if its doctor is ledger-mapped,
    and return the appointment's resulting status. Never raises: on any failure (job
    already claimed elsewhere, provider error, database error) the job stays for the tick
    and the status read back (normally 'pending_sync') is returned."""
    if budget_seconds <= 0:
        return "pending_sync"
    deadline = Deadline(budget_seconds)
    try:
        with connect(url) as conn:
            row = conn.execute(
                """SELECT o.id, d.booking_calendar_id FROM outbox o
                JOIN appointments a ON a.clinic_id=o.clinic_id AND a.id=o.appointment_id
                JOIN doctors d ON d.clinic_id=a.clinic_id AND d.id=a.doctor_id
                WHERE o.clinic_id=%s AND o.appointment_id=%s AND o.kind='calendar.create'
                AND o.appointment_version=a.version AND o.status='pending'""",
                (clinic_id, appointment_id),
            ).fetchone()
            conn.commit()
            if row is not None and is_ledger(row["booking_calendar_id"]) and not deadline.is_set():
                check_ports(ports)
                for job in claim(
                    conn, worker_id("inline"), 1, kinds=("calendar.create",), ids=[row["id"]]
                ):
                    process(conn, job, ports)
            status = conn.execute(
                "SELECT status FROM appointments WHERE clinic_id=%s AND id=%s",
                (clinic_id, appointment_id),
            ).fetchone()
            conn.commit()
    except Exception as error:
        log.error("inline confirm failed error=%s", type(error).__name__)
        return "pending_sync"
    return status["status"] if status else "pending_sync"
