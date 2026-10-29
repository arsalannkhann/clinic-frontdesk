"""Booking confirmations, reminders and opt-outs: planning and policy (T9).

Outbound contact is off unless the clinic has outbound_enabled (false by default), and every
contact needs, both when it is planned and again just before it is sent:
- the appointment still confirmed at the planned version (a cancellation or reschedule bumps
  or ends it, so an older contact no-ops);
- an active consent grant for its purpose (booking_confirmation, reminder_call or
  reminder_message) and no matching opt-out (contact_preferences);
- a time outside the clinic's quiet hours, computed in the clinic's IANA zone (shifted to the
  next allowed instant, never later than MIN_NOTICE before the appointment);
- room in the clinic's daily contact budget (clinic-local day). Over budget, a contact is
  postponed to the next day that has room, within its window, or suppressed with reason
  'budget_exhausted'; it is never dropped silently.

Planning (inside the caller's transaction):
- on_confirmed(): called by the calendar.create handler in the transaction that sets an
  appointment confirmed. Plans a confirmation message (now, shifted out of quiet hours) and
  the reminder call and/or message (starts_at - reminder_lead) the patient consented to. A
  reminder whose lead time has already passed is not planned (the confirmation covers it).
- schedule_due(): worker housekeeping backfill for confirmed appointments that entered the
  reminder window without reminders (outbound enabled or consent granted after confirmation).
- on_cancelled(): called by appointments.cancel_locked. Cancels unsent contacts of the old
  version at once and enqueues reminder.stop, which stops a placed call that is still queued.

Each contact is one reminders row, unique per clinic by dedupe_key (appointment, version,
channel, purpose, lead minutes), so racing planners create it once; its job (notification.send
or reminder.call, payload {reminder_id}) is inserted only by the planner whose row insert won.
Rows and payloads hold ids, times and short codes only: no names, numbers or message text.

Lock order (locks.py): appointment row (5), then the contact-budget advisory lock of one
clinic-local day ("contact-budget:<clinic>:<date>", days in ascending order), then reminder
rows, then the job's own outbox row (6).
"""

import logging
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from clinic_voice import service

log = logging.getLogger("clinic_voice.reminders")

CONSENT = {
    ("booking_confirmation", "message"): "booking_confirmation",
    ("reminder", "call"): "reminder_call",
    ("reminder", "message"): "reminder_message",
}
JOB_KIND = {"message": "notification.send", "call": "reminder.call"}
OPT_OUT_PURPOSES = ("booking_confirmation", "reminder", "all")
OPT_OUT_CHANNELS = ("call", "message", "all")
# A contact must be due at least this long before the appointment starts.
MIN_NOTICE = timedelta(minutes=30)
# A contact not sent within this long of its planned time is suppressed ('expired').
EXPIRY = timedelta(hours=12)
# Days a planner looks ahead for budget room before suppressing.
MAX_BUDGET_DAYS = 7
# Statuses that hold a place in the daily budget at planning time / at send time.
PLANNED = ("scheduled", "sending", "sent", "unknown", "needs_review")
ATTEMPTED = ("sending", "sent", "unknown", "needs_review")
# Housekeeping looks this far past the reminder lead for confirmed appointments to backfill.
BACKFILL_WINDOW = timedelta(days=2)
BACKFILL_BATCH = 100


# Quiet hours (pure, DST-safe)


def in_quiet(moment: datetime, zone: ZoneInfo, start: time, end: time) -> bool:
    """Whether the clinic-local wall time of `moment` is inside [start, end). A window with
    start after end wraps midnight."""
    local = moment.astimezone(zone).time()
    if start < end:
        return start <= local < end
    return local >= start or local < end


def next_allowed(moment: datetime, zone: ZoneInfo, start: time, end: time) -> datetime:
    """The earliest instant at or after `moment` outside quiet hours, as UTC.

    The quiet window ends at the wall time `end` of the right local day. That wall time is
    resolved with both DST folds and the earliest resulting instant that is outside quiet
    hours wins: an ambiguous end (clocks going back) resolves to its first occurrence; an end
    that does not exist (clocks going forward) resolves with the pre-transition offset, at
    most one hour after the transition."""
    moment = moment.astimezone(UTC)
    if not in_quiet(moment, zone, start, end):
        return moment
    local = moment.astimezone(zone)
    day = local.date()
    if start > end and local.time() >= start:
        day += timedelta(days=1)  # Evening part of a window that wraps midnight.
    wall = datetime.combine(day, end)
    candidates = sorted({wall.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1)})
    for candidate in candidates:
        if candidate >= moment and not in_quiet(candidate, zone, start, end):
            return candidate
    raise ValueError("no allowed time after the quiet window")  # Unreachable for start != end.


def local_day(moment: datetime, zone: ZoneInfo) -> date:
    return moment.astimezone(zone).date()


def start_of_next_day(moment: datetime, zone: ZoneInfo) -> datetime:
    day = local_day(moment, zone) + timedelta(days=1)
    # Midnight may not exist on a DST day in some zones; the earliest fold is still that day.
    return min(
        datetime.combine(day, time(0)).replace(tzinfo=zone, fold=fold).astimezone(UTC)
        for fold in (0, 1)
    )


def spoken_when(moment: datetime, zone: ZoneInfo) -> str:
    local = moment.astimezone(zone)
    return (
        local.strftime("%A %d %B").replace(" 0", " ")
        + " at "
        + local.strftime("%I:%M %p").lstrip("0")
    )


def message_body(purpose: str, clinic_name: str, starts_at, zone, reference: str) -> str:
    """The only message templates. Clinic name, appointment date/time and booking reference;
    never the patient's name, the doctor, the service or any other detail. Composed at send
    time and never stored."""
    when = spoken_when(starts_at, zone)
    if purpose == "booking_confirmation":
        return f"{clinic_name}: your appointment on {when} is confirmed. Booking reference {reference}."
    return f"{clinic_name}: reminder of your appointment on {when}. Booking reference {reference}."


def call_context(purpose: str, clinic_name: str, starts_at, zone, reference, contact_reference):
    """user_data for a reminder call (Bolna POST /call). Same fields as the message template,
    plus our stable contact reference so staff can match the execution in the Bolna
    dashboard after an ambiguous timeout."""
    return {
        "call_purpose": "appointment_reminder" if purpose == "reminder" else purpose,
        "clinic_name": clinic_name,
        "appointment_time": spoken_when(starts_at, zone),
        "booking_reference": reference,
        "contact_reference": contact_reference,
    }


# Policy checks


CONTEXT_SQL = """SELECT a.id, a.clinic_id, a.status, a.version, a.starts_at, a.patient_id,
    a.booking_reference, c.name AS clinic_name, c.timezone, c.outbound_enabled,
    c.quiet_hours_start, c.quiet_hours_end, c.reminder_lead_minutes, c.daily_contact_budget,
    c.bolna_agent_id
    FROM appointments a JOIN clinics c ON c.id = a.clinic_id
    WHERE a.clinic_id=%s AND a.id=%s"""


def context(conn, clinic_id, appointment_id, lock=None):
    """The appointment with its clinic's contact settings. lock: None or 'share'."""
    suffix = " FOR SHARE OF a" if lock == "share" else ""
    return conn.execute(CONTEXT_SQL + suffix, (clinic_id, appointment_id)).fetchone()


def refusal(conn, clinic_id, patient_id, purpose, channel) -> str | None:
    """None if this patient may be contacted for (purpose, channel), else a reason code:
    no_patient, no_consent or opted_out."""
    if patient_id is None:
        return "no_patient"
    row = conn.execute(
        """SELECT EXISTS (SELECT 1 FROM consents WHERE clinic_id=%(clinic)s
            AND patient_id=%(patient)s AND purpose=%(consent)s AND revoked_at IS NULL) AS consented,
        EXISTS (SELECT 1 FROM contact_preferences WHERE clinic_id=%(clinic)s
            AND patient_id=%(patient)s AND purpose IN (%(purpose)s, 'all')
            AND channel IN (%(channel)s, 'all')) AS opted_out""",
        {
            "clinic": clinic_id,
            "patient": patient_id,
            "consent": CONSENT[(purpose, channel)],
            "purpose": purpose,
            "channel": channel,
        },
    ).fetchone()
    if row["opted_out"]:
        return "opted_out"
    if not row["consented"]:
        return "no_consent"
    return None


def lock_budget_day(conn, clinic_id, day: date) -> None:
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"contact-budget:{clinic_id}:{day.isoformat()}",),
    )


def budget_used(conn, clinic_id, day: date, statuses, exclude=None) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM reminders WHERE clinic_id=%s AND contact_day=%s"
        " AND status = ANY(%s) AND (%s::uuid IS NULL OR id <> %s::uuid)",
        (clinic_id, day, list(statuses), exclude, exclude),
    ).fetchone()["n"]


def fit_budget(conn, ctx, when, latest, statuses, exclude=None) -> datetime | None:
    """The first allowed instant at or after `when` (and not after `latest`) on a
    clinic-local day with budget room, or None. Locks each budget day it inspects, in
    ascending order, for the rest of the caller's transaction."""
    zone = ZoneInfo(ctx["timezone"])
    start, end = ctx["quiet_hours_start"], ctx["quiet_hours_end"]
    for _ in range(MAX_BUDGET_DAYS):
        if when > latest:
            return None
        day = local_day(when, zone)
        lock_budget_day(conn, ctx["clinic_id"], day)
        if (
            budget_used(conn, ctx["clinic_id"], day, statuses, exclude)
            < ctx["daily_contact_budget"]
        ):
            return when
        when = next_allowed(start_of_next_day(when, zone), zone, start, end)
    return None


# Planning


def dedupe_key(appointment_id, version, channel, purpose, lead_minutes) -> str:
    return f"{appointment_id}:{version}:{channel}:{purpose}:{lead_minutes}"


def plan(conn, ctx, purpose, channel, lead_minutes, due, now) -> UUID | None:
    """Create one contact (row + job) for a confirmed appointment in the caller's transaction,
    unless a contact with the same dedupe key exists. A contact that cannot be placed in its
    window (quiet hours, budget) is stored as suppressed with the reason and gets no job.
    Returns the new reminder id, or None if it already existed."""
    zone = ZoneInfo(ctx["timezone"])
    start, end = ctx["quiet_hours_start"], ctx["quiet_hours_end"]
    latest = ctx["starts_at"] - MIN_NOTICE
    status, reason = "scheduled", None
    when = next_allowed(max(due, now), zone, start, end)
    if when > latest:
        status, reason, when = "suppressed", "quiet_hours", max(due, now)
    else:
        fitted = fit_budget(conn, ctx, when, latest, PLANNED)
        if fitted is None:
            status, reason = "suppressed", "budget_exhausted"
        else:
            when = fitted
    reminder_id = uuid4()
    row = conn.execute(
        """INSERT INTO reminders (id,clinic_id,appointment_id,appointment_version,channel,purpose,
        scheduled_for,contact_day,status,last_error,provider_reference,dedupe_key)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (clinic_id, dedupe_key) DO NOTHING RETURNING id""",
        (
            reminder_id,
            ctx["clinic_id"],
            ctx["id"],
            ctx["version"],
            channel,
            purpose,
            when,
            local_day(when, zone),
            status,
            reason,
            "cvr_" + reminder_id.hex,
            dedupe_key(ctx["id"], ctx["version"], channel, purpose, lead_minutes),
        ),
    ).fetchone()
    if row is None:
        return None
    if status == "scheduled":
        conn.execute(
            """INSERT INTO outbox (id,clinic_id,appointment_id,kind,appointment_version,payload,
            available_at) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (
                uuid4(),
                ctx["clinic_id"],
                ctx["id"],
                JOB_KIND[channel],
                ctx["version"],
                Jsonb({"reminder_id": str(reminder_id)}),
                when,
            ),
        )
    else:
        log.info("contact suppressed at planning reminder=%s reason=%s", reminder_id, reason)
    return reminder_id


def plan_reminders(conn, ctx, now) -> int:
    """Plan the reminder call and message the patient consented to (starts_at - lead), unless
    the lead time has already passed."""
    lead = ctx["reminder_lead_minutes"]
    due = ctx["starts_at"] - timedelta(minutes=lead)
    if due < now:
        return 0
    created = 0
    for channel in ("call", "message"):
        if refusal(conn, ctx["clinic_id"], ctx["patient_id"], "reminder", channel) is None:
            created += plan(conn, ctx, "reminder", channel, lead, due, now) is not None
    return created


def _plannable(ctx, version=None) -> bool:
    return (
        ctx is not None
        and ctx["status"] == "confirmed"
        and (version is None or ctx["version"] == version)
        and ctx["outbound_enabled"]
        and ctx["patient_id"] is not None
    )


def on_confirmed(conn, clinic_id, appointment_id, version) -> int:
    """Plan the confirmation message and reminders of an appointment that this transaction
    has just set confirmed (the caller holds the appointment row). Does not commit."""
    ctx = context(conn, clinic_id, appointment_id)
    if not _plannable(ctx, version):
        return 0
    now = service.utc_now()
    created = 0
    if refusal(conn, clinic_id, ctx["patient_id"], "booking_confirmation", "message") is None:
        created += plan(conn, ctx, "booking_confirmation", "message", 0, now, now) is not None
    return created + plan_reminders(conn, ctx, now)


def schedule_due(conn, limit=BACKFILL_BATCH) -> int:
    """Housekeeping backfill across tenants: confirmed appointments of outbound-enabled
    clinics whose reminder time is still ahead (within BACKFILL_WINDOW) and that lack a
    reminder for a channel the patient has consented to. One transaction per appointment,
    which takes the appointment row FOR SHARE (skipping rows locked by a change). Commits.
    Returns the number of contacts created."""
    now = service.utc_now()
    candidates = conn.execute(
        """SELECT a.clinic_id, a.id FROM appointments a
        JOIN clinics c ON c.id = a.clinic_id AND c.outbound_enabled
        WHERE a.status = 'confirmed' AND a.patient_id IS NOT NULL
        AND a.starts_at - make_interval(mins => c.reminder_lead_minutes) >= %(now)s
        AND a.starts_at - make_interval(mins => c.reminder_lead_minutes) < %(until)s
        AND EXISTS (
            SELECT 1 FROM consents k WHERE k.clinic_id = a.clinic_id
            AND k.patient_id = a.patient_id AND k.revoked_at IS NULL
            AND k.purpose IN ('reminder_call', 'reminder_message')
            AND NOT EXISTS (SELECT 1 FROM reminders r WHERE r.clinic_id = a.clinic_id
                AND r.appointment_id = a.id AND r.appointment_version = a.version
                AND r.purpose = 'reminder'
                AND r.channel = CASE k.purpose WHEN 'reminder_call' THEN 'call'
                    ELSE 'message' END))
        ORDER BY a.starts_at, a.id LIMIT %(limit)s""",
        {"now": now, "until": now + BACKFILL_WINDOW, "limit": limit},
    ).fetchall()
    conn.commit()
    created = 0
    for candidate in candidates:
        ctx = conn.execute(
            CONTEXT_SQL + " FOR SHARE OF a SKIP LOCKED", (candidate["clinic_id"], candidate["id"])
        ).fetchone()
        if _plannable(ctx):
            created += plan_reminders(conn, ctx, now)
        conn.commit()
    if created:
        log.info("reminders backfilled count=%d", created)
    return created


def on_cancelled(conn, clinic_id, appointment_id, version) -> None:
    """In the cancelling transaction (appointment row locked): cancel the old version's unsent
    contacts and enqueue reminder.stop when that version has any contact, so a placed call
    that is still queued is stopped. Does not commit."""
    conn.execute(
        "UPDATE reminders SET status='cancelled', last_error='appointment_changed',"
        " updated_at=now() WHERE clinic_id=%s AND appointment_id=%s AND appointment_version=%s"
        " AND status='scheduled'",
        (clinic_id, appointment_id, version),
    )
    exists = conn.execute(
        "SELECT 1 FROM reminders WHERE clinic_id=%s AND appointment_id=%s"
        " AND appointment_version=%s LIMIT 1",
        (clinic_id, appointment_id, version),
    ).fetchone()
    if exists:
        conn.execute(
            "INSERT INTO outbox (id,clinic_id,appointment_id,kind,appointment_version)"
            " VALUES (%s,%s,%s,'reminder.stop',%s)",
            (uuid4(), clinic_id, appointment_id, version),
        )


# Opt-out


def opt_out(conn, clinic_id, patient_id, purpose, channel, source, staff_user_id=None) -> int:
    """Record an opt-out for one patient and suppress that patient's matching unsent contacts
    at once, in the caller's transaction. Idempotent. Returns how many were suppressed. A send
    already under way when this commits completes; every later attempt sees the opt-out."""
    if purpose not in OPT_OUT_PURPOSES or channel not in OPT_OUT_CHANNELS:
        raise ValueError("unknown opt-out scope")
    conn.execute(
        """INSERT INTO contact_preferences (id,clinic_id,patient_id,purpose,channel,source,
        staff_user_id) VALUES (%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (clinic_id, patient_id, purpose, channel) DO NOTHING""",
        (uuid4(), clinic_id, patient_id, purpose, channel, source, staff_user_id),
    )
    return conn.execute(
        """UPDATE reminders r SET status='suppressed', last_error='opted_out', updated_at=now()
        FROM appointments a
        WHERE a.clinic_id = r.clinic_id AND a.id = r.appointment_id
        AND r.clinic_id=%(clinic)s AND a.patient_id=%(patient)s AND r.status='scheduled'
        AND (%(purpose)s = 'all' OR r.purpose = %(purpose)s)
        AND (%(channel)s = 'all' OR r.channel = %(channel)s)""",
        {"clinic": clinic_id, "patient": patient_id, "purpose": purpose, "channel": channel},
    ).rowcount


REMINDER_COLUMNS = (
    "id,appointment_id,appointment_version,channel,purpose,scheduled_for,status,attempts,"
    "last_error,provider_execution_id,stop_outcome,sent_at,created_at,updated_at"
)


def list_reminders(conn, clinic_id, status, limit):
    """The clinic's contacts, most recently changed first. No provider message ids or
    references; nothing about the patient."""
    return conn.execute(
        f"SELECT {REMINDER_COLUMNS} FROM reminders WHERE clinic_id=%s"  # noqa: S608
        " AND (%s::text IS NULL OR status=%s) ORDER BY updated_at DESC, id LIMIT %s",
        (clinic_id, status, status, limit),
    ).fetchall()
