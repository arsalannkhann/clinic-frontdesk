"""Contact handlers: notification.send (messages), reminder.call (calls) and reminder.stop (T9).

notification.send and reminder.call carry {reminder_id}. An attempt:
1. Settles a row left 'unknown' (ambiguous timeout) or 'sending' (a worker died mid-send)
   before anything else: a message is looked up by its stable provider reference
   (MessagingPort.get_message); found means sent, not found means nothing was sent and the
   contact is attempted again. A call cannot be looked up (Bolna documents no lookup by
   client reference, and a timed-out POST /call returns no execution id), so it becomes
   needs_review for staff and is never placed again automatically.
2. In one transaction: locks the appointment row (FOR SHARE), the contact-budget day, the
   reminder row; re-checks the appointment status and version (a stale version cancels the
   contact), the start time, expiry, outbound_enabled, consent and opt-out; defers the job
   until the planned time or out of quiet hours; enforces the daily budget (postpone or
   suppress); then marks the row 'sending', renews the job's lease (fenced) and decrypts the
   patient's phone number. Nothing is sent if any check fails or the lease was lost.
3. Sends with the connection idle. The message body or call context is composed here and
   handed to the port only; it is never stored or logged.
4. Records the outcome on the row in its own commit (even if the lease was since lost: the
   contact happened), then settles the job. A timeout records 'unknown' and (messages)
   retries, so the next attempt reconciles first; a retryable error returns the row to
   'scheduled' and retries; a permanent error marks it failed and dead-letters the job.

reminder.stop (appointment id and old version on the job) cancels that version's unsent
contacts and stops each placed call that has no stop outcome yet through
BolnaPort.stop_scheduled_call. A refused stop (the call is no longer queued or scheduled)
is recorded as not_stoppable unless a lookup shows the call was stopped. While a contact of
that version is still 'sending' the job retries shortly.
"""

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from clinic_voice import reminders, service
from clinic_voice.config import get_settings
from clinic_voice.crypto import CryptoError, Keyring
from clinic_voice.domain import as_uuid
from clinic_voice.outbox import (
    complete,
    defer,
    extend_lease,
    fail_dead,
    mark_done,
    provider_failure,
    renew,
    require_idle,
    retry,
)
from clinic_voice.patients import decrypt_field
from clinic_voice.providers.calendar import (
    ProviderError,
    ProviderPermanent,
    ProviderRetryable,
)

log = logging.getLogger("clinic_voice.worker")

SETTLED = ("sent", "needs_review", "cancelled", "suppressed", "failed")
STOP_RETRY = timedelta(seconds=60)
MAX_MESSAGE_ID = 128


def _keyring() -> Keyring | None:
    keys = get_settings().token_encryption_keys
    if not keys or not keys.get_secret_value():
        return None
    try:
        return Keyring.from_setting(keys.get_secret_value())
    except CryptoError:
        log.error("patient keyring is invalid; contact not sent")
        return None


def _set(conn, clinic_id, reminder_id, status, code, *, current=("sending",), **values):
    """Move a reminder from one of `current` to `status`, with code as last_error; commits.
    values: provider_execution_id / provider_message_id. Returns whether a row moved."""
    sets = ["status=%(status)s", "last_error=%(code)s", "updated_at=now()"]
    if status == "sent":
        sets.append("sent_at=now()")
    for column in ("provider_execution_id", "provider_message_id"):
        if column in values:
            sets.append(f"{column}=%({column})s")
    row = conn.execute(
        f"UPDATE reminders SET {', '.join(sets)} WHERE clinic_id=%(clinic)s AND id=%(id)s"  # noqa: S608
        " AND status = ANY(%(current)s) RETURNING id",
        {
            "status": status,
            "code": code,
            "clinic": clinic_id,
            "id": reminder_id,
            "current": list(current),
            **values,
        },
    ).fetchone()
    conn.commit()
    return row is not None


def _settle(conn, job, status, code, reminder_id) -> bool:
    """Record a policy outcome (cancelled/suppressed/failed) in the open transaction and
    complete the job with it (fenced; both roll back if the lease was lost)."""
    conn.execute(
        "UPDATE reminders SET status=%s, last_error=%s, updated_at=now()"
        " WHERE clinic_id=%s AND id=%s AND status='scheduled'",
        (status, code, job.clinic_id, reminder_id),
    )
    if not mark_done(conn, job):
        conn.rollback()
        return False
    conn.commit()
    log.info("contact job done id=%s kind=%s outcome=%s:%s", job.id, job.kind, status, code)
    return True


def _port(ports, channel):
    return getattr(ports, "messaging" if channel == "message" else "bolna", None)


def _load(conn, job, reminder_id):
    row = conn.execute(
        "SELECT id, channel, status, provider_reference FROM reminders"
        " WHERE clinic_id=%s AND id=%s",
        (job.clinic_id, reminder_id),
    ).fetchone()
    conn.commit()
    return row


def contact(conn, job, ports, channel) -> bool:
    reminder_id = as_uuid((job.payload or {}).get("reminder_id"))
    if reminder_id is None:
        return fail_dead(conn, job, "bad_payload")
    row = _load(conn, job, reminder_id)
    if row is None or row["status"] in SETTLED:
        return complete(conn, job)
    if row["channel"] != channel:
        return fail_dead(conn, job, "bad_payload")
    port = _port(ports, channel)
    if port is None:
        return fail_dead(
            conn, job, f"{'messaging' if channel == 'message' else 'bolna'}_unconfigured"
        )
    if row["status"] in ("unknown", "sending"):
        settled = _reconcile(conn, job, port, row, channel)
        if settled is not None:
            return settled
    return _attempt(conn, job, port, channel, reminder_id)


def send_message(conn, job, ports) -> bool:
    return contact(conn, job, ports, "message")


def place_call(conn, job, ports) -> bool:
    return contact(conn, job, ports, "call")


def _reconcile(conn, job, port, row, channel):
    """Settle an ambiguous earlier attempt. Returns the job outcome, or None when nothing was
    sent and the contact may be attempted again (the row is back to 'scheduled')."""
    unsettled = ("unknown", "sending")
    if channel == "call":
        _set(conn, job.clinic_id, row["id"], "needs_review", "outcome_unknown", current=unsettled)
        log.warning("contact needs review id=%s kind=%s code=outcome_unknown", job.id, job.kind)
        return complete(conn, job)
    require_idle(conn)
    try:
        found = port.get_message(row["provider_reference"])
    except ProviderError as error:
        return provider_failure(conn, job, error)  # Still unknown; retried later.
    if found is not None:
        message_id = found.provider_message_id
        if not isinstance(message_id, str) or not 0 < len(message_id) <= MAX_MESSAGE_ID:
            message_id = None
        _set(
            conn,
            job.clinic_id,
            row["id"],
            "sent",
            None,
            current=unsettled,
            provider_message_id=message_id,
        )
        log.info("contact reconciled id=%s kind=%s outcome=sent", job.id, job.kind)
        return complete(conn, job)
    _set(conn, job.clinic_id, row["id"], "scheduled", "reconciled_not_sent", current=unsettled)
    return None


def _blocked(conn, ctx, reminder, now, keyring):
    """(status, code) when the contact must not be made, else None."""
    if (
        ctx is None
        or ctx["status"] != "confirmed"
        or ctx["version"] != reminder["appointment_version"]
    ):
        return "cancelled", "stale"
    if ctx["starts_at"] - reminders.MIN_NOTICE < now:
        return "suppressed", "too_late"
    if now - reminder["scheduled_for"] > reminders.EXPIRY:
        return "suppressed", "expired"
    if not ctx["outbound_enabled"]:
        return "suppressed", "outbound_disabled"
    refused = reminders.refusal(
        conn, ctx["clinic_id"], ctx["patient_id"], reminder["purpose"], reminder["channel"]
    )
    if refused:
        return "suppressed", refused
    if reminder["channel"] == "call" and not ctx["bolna_agent_id"]:
        return "failed", "agent_unconfigured"
    if keyring is None:
        return "failed", "keyring_unconfigured"
    return None


def _phone(conn, keyring, ctx):
    row = conn.execute(
        "SELECT phone_ciphertext, phone_key_id FROM patients WHERE clinic_id=%s AND id=%s",
        (ctx["clinic_id"], ctx["patient_id"]),
    ).fetchone()
    if row is None:
        return None
    return decrypt_field(
        keyring,
        ctx["clinic_id"],
        ctx["patient_id"],
        "phone",
        row["phone_key_id"],
        row["phone_ciphertext"],
    )


def _attempt(conn, job, port, channel, reminder_id) -> bool:
    now = service.utc_now()
    keyring = _keyring()
    ctx = reminders.context(conn, job.clinic_id, job.appointment_id, lock="share")
    reminder = conn.execute(
        "SELECT * FROM reminders WHERE clinic_id=%s AND id=%s",
        (job.clinic_id, reminder_id),
    ).fetchone()
    if reminder is None or reminder["appointment_id"] != job.appointment_id:
        conn.rollback()
        return fail_dead(conn, job, "bad_payload")
    if reminder["status"] != "scheduled":
        conn.rollback()
        return complete(conn, job)
    blocked = _blocked(conn, ctx, reminder, now, keyring)
    if blocked:
        return _settle(conn, job, *blocked, reminder_id)
    if reminder["scheduled_for"] > now:
        conn.rollback()
        return defer(conn, job, reminder["scheduled_for"], "not_due")
    zone = ZoneInfo(ctx["timezone"])
    start, end = ctx["quiet_hours_start"], ctx["quiet_hours_end"]
    latest = ctx["starts_at"] - reminders.MIN_NOTICE
    if reminders.in_quiet(now, zone, start, end):
        return _postpone(
            conn,
            job,
            reminder_id,
            reminders.next_allowed(now, zone, start, end),
            latest,
            zone,
            "quiet_hours",
        )
    day = reminders.local_day(now, zone)
    reminders.lock_budget_day(conn, job.clinic_id, day)
    reminder = conn.execute(
        "SELECT * FROM reminders WHERE clinic_id=%s AND id=%s AND status='scheduled' FOR UPDATE",
        (job.clinic_id, reminder_id),
    ).fetchone()
    if reminder is None:
        conn.rollback()
        return complete(conn, job)
    blocked = _blocked(conn, ctx, reminder, now, keyring)  # Again, now that the row is locked.
    if blocked:
        return _settle(conn, job, *blocked, reminder_id)
    used = reminders.budget_used(conn, job.clinic_id, day, reminders.ATTEMPTED, reminder_id)
    if used >= ctx["daily_contact_budget"]:
        later = reminders.next_allowed(reminders.start_of_next_day(now, zone), zone, start, end)
        return _postpone(conn, job, reminder_id, later, latest, zone, "budget_exhausted")
    phone = _phone(conn, keyring, ctx)
    if phone is None:
        return _settle(conn, job, "suppressed", "no_phone", reminder_id)
    if not extend_lease(conn, job):
        conn.rollback()
        return False
    conn.execute(
        "UPDATE reminders SET status='sending', attempts=attempts + 1, contact_day=%s,"
        " last_error=NULL, updated_at=now() WHERE clinic_id=%s AND id=%s",
        (day, job.clinic_id, reminder_id),
    )
    conn.commit()
    return _send(conn, job, port, channel, ctx, reminder, phone, zone)


def _postpone(conn, job, reminder_id, when, latest, zone, code) -> bool:
    """Move the contact to `when` and defer its job, or suppress it with `code` when `when`
    is past its window."""
    if when > latest:
        return _settle(conn, job, "suppressed", code, reminder_id)
    conn.execute(
        "UPDATE reminders SET scheduled_for=%s, contact_day=%s, updated_at=now()"
        " WHERE clinic_id=%s AND id=%s AND status='scheduled'",
        (when, reminders.local_day(when, zone), job.clinic_id, reminder_id),
    )
    return defer(conn, job, when, code)  # Commits the move with the deferral (fenced).


def _send(conn, job, port, channel, ctx, reminder, phone, zone) -> bool:
    require_idle(conn)
    clinic_id, reminder_id = job.clinic_id, reminder["id"]
    reference = reminder["provider_reference"]
    try:
        if channel == "message":
            body = reminders.message_body(
                reminder["purpose"],
                ctx["clinic_name"],
                ctx["starts_at"],
                zone,
                ctx["booking_reference"],
            )
            provider_id = port.send_message(phone, body, reference)
        else:
            provider_id = port.start_outbound_call(
                ctx["bolna_agent_id"],
                phone,
                user_data=reminders.call_context(
                    reminder["purpose"],
                    ctx["clinic_name"],
                    ctx["starts_at"],
                    zone,
                    ctx["booking_reference"],
                    reference,
                ),
            )
    except ProviderRetryable as error:
        _set(conn, clinic_id, reminder_id, "scheduled", "retryable")
        return provider_failure(conn, job, error)
    except ProviderPermanent:
        _set(conn, clinic_id, reminder_id, "failed", "provider_permanent")
        return fail_dead(conn, job, "provider_permanent")
    except ValueError:
        _set(conn, clinic_id, reminder_id, "failed", "bad_request")
        return fail_dead(conn, job, "bad_request")
    except ProviderError:
        # ProviderTimeout or an unclassified error: the provider may have acted.
        _set(conn, clinic_id, reminder_id, "unknown", "timeout")
        log.warning("contact outcome unknown id=%s kind=%s", job.id, job.kind)
        if channel == "call":
            _set(
                conn,
                clinic_id,
                reminder_id,
                "needs_review",
                "outcome_unknown",
                current=("unknown",),
            )
            return complete(conn, job)
        return retry(conn, job, "timeout")  # The next attempt looks the message up first.
    values = {}
    if channel == "call":
        values["provider_execution_id"] = provider_id
    elif isinstance(provider_id, str) and 0 < len(provider_id) <= MAX_MESSAGE_ID:
        values["provider_message_id"] = provider_id
    _set(conn, clinic_id, reminder_id, "sent", None, **values)
    log.info("contact sent id=%s kind=%s", job.id, job.kind)
    return complete(conn, job)


def contact_on_dead(conn, job):
    """A dead contact job: an unsettled attempt goes to staff review, an unsent one fails."""
    reminder_id = as_uuid((job.payload or {}).get("reminder_id"))
    if reminder_id is None:
        return
    conn.execute(
        """UPDATE reminders SET status = CASE WHEN status='scheduled' THEN 'failed'
            ELSE 'needs_review' END,
        last_error = CASE WHEN status='scheduled' THEN 'job_dead' ELSE 'outcome_unknown' END,
        updated_at=now()
        WHERE id IN (SELECT id FROM reminders WHERE clinic_id=%s AND id=%s
            AND status IN ('scheduled', 'sending', 'unknown') FOR UPDATE SKIP LOCKED)""",
        (job.clinic_id, reminder_id),
    )


# reminder.stop


def stop(conn, job, ports) -> bool:
    if job.appointment_id is None:
        return fail_dead(conn, job, "bad_payload")
    scope = (job.clinic_id, job.appointment_id, job.appointment_version)
    conn.execute(
        "UPDATE reminders SET status='cancelled', last_error='appointment_changed',"
        " updated_at=now() WHERE clinic_id=%s AND appointment_id=%s AND appointment_version=%s"
        " AND status='scheduled'",
        scope,
    )
    calls = conn.execute(
        """SELECT id, status, provider_execution_id FROM reminders
        WHERE clinic_id=%s AND appointment_id=%s AND appointment_version=%s AND channel='call'
        AND (status='sending' OR (status='sent' AND stop_outcome IS NULL
            AND provider_execution_id IS NOT NULL))
        ORDER BY created_at, id""",
        scope,
    ).fetchall()
    conn.commit()
    if any(call["status"] == "sending" for call in calls):
        return retry(conn, job, "contact_in_flight", STOP_RETRY)
    if not calls:
        return complete(conn, job)
    port = getattr(ports, "bolna", None)
    if port is None:
        return retry(conn, job, "bolna_unconfigured")
    for call in calls:
        if not renew(conn, job):
            return False
        execution_id = call["provider_execution_id"]
        try:
            port.stop_scheduled_call(execution_id)
            outcome = "stopped"
        except ProviderPermanent:
            outcome = _stopped_already(port, execution_id)
        except ValueError:
            outcome = "not_stoppable"
        except ProviderError as error:
            return provider_failure(conn, job, error)
        conn.execute(
            "UPDATE reminders SET stop_outcome=%s, updated_at=now()"
            " WHERE clinic_id=%s AND id=%s AND stop_outcome IS NULL",
            (outcome, job.clinic_id, call["id"]),
        )
        conn.commit()
        log.info("reminder call stop id=%s outcome=%s", job.id, outcome)
    return complete(conn, job)


def _stopped_already(port, execution_id) -> str:
    """After a refused stop: 'stopped' if the execution is already stopped (an earlier
    attempt's stop landed before a timeout), else 'not_stoppable'."""
    try:
        execution = port.get_execution(execution_id)
    except (ProviderError, ValueError):
        return "not_stoppable"
    return "stopped" if execution is not None and execution.status == "stopped" else "not_stoppable"
