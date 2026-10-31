"""Staff platform reads and writes (P2): calendar, staff bookings, conversations, stats.

The clinic always comes from the staff session. Viewers read everything except decrypted
patient details (names, transcripts); owners and admins see them and every view of decrypted
details is audited. Every write is audited with the staff member as actor.

Calendar sync_state is the status of the appointment's latest calendar.create /
calendar.cancel outbox job: done -> synced, pending or processing -> pending, dead -> failed,
no such job -> not_applicable.
"""

import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from clinic_voice import chat, public, service
from clinic_voice.audit import audit_many
from clinic_voice.db import connect
from clinic_voice.patients import decrypt_field

log = logging.getLogger("clinic_voice.staff_platform")

NOT_FOUND = "Not found"
MAX_CALENDAR_DAYS = 31
# Appointments returned by one calendar read; more sets truncated (the earliest are kept).
MAX_CALENDAR_APPOINTMENTS = 2000
STAFF_KEY_PREFIX = "staff:"
SYNC_STATE = {"done": "synced", "pending": "pending", "processing": "pending", "dead": "failed"}
ACTIVE = ("pending_sync", "confirmed", "needs_review")


def _patient_name(keyring, row) -> str | None:
    if keyring is None or row.get("patient_id") is None:
        return None
    return decrypt_field(
        keyring,
        row["clinic_id"],
        row["patient_id"],
        "name",
        row["name_key_id"],
        row["name_ciphertext"],
    )


def _details(staff, keyring):
    """The keyring when this staff member may see decrypted patient details, else None."""
    return keyring if staff["role"] in ("owner", "admin") else None


# --- Me ----------------------------------------------------------------------------------------


def me(conn, staff) -> dict:
    clinic = conn.execute(
        "SELECT id, name, timezone, public_slug FROM clinics WHERE id=%s", (staff["clinic_id"],)
    ).fetchone()
    return {"id": staff["id"], "email": staff["email"], "role": staff["role"], "clinic": clinic}


# --- Calendar ----------------------------------------------------------------------------------


def calendar(conn, staff, keyring, starts, ends, include_cancelled=False) -> dict:
    """Doctors, appointments overlapping [starts, ends) and schedule exceptions overlapping
    it. At most MAX_CALENDAR_DAYS (422 otherwise) and MAX_CALENDAR_APPOINTMENTS appointments
    (the earliest; truncated is true when more exist). Patient names for owner/admin only,
    each audited as 'appointment.patient_details_viewed' (one row per appointment, written
    in one INSERT)."""
    if ends <= starts:
        raise HTTPException(422, "to must be after from")
    if ends - starts > timedelta(days=MAX_CALENDAR_DAYS):
        raise HTTPException(422, f"the range must be at most {MAX_CALENDAR_DAYS} days")
    clinic_id = staff["clinic_id"]
    keyring = _details(staff, keyring)
    clinic = conn.execute("SELECT timezone FROM clinics WHERE id=%s", (clinic_id,)).fetchone()
    doctors = conn.execute(
        "SELECT id, name, colour FROM doctors WHERE clinic_id=%s AND active ORDER BY name, id",
        (clinic_id,),
    ).fetchall()
    rows = conn.execute(
        """SELECT a.id, a.clinic_id, a.doctor_id, a.service_id, s.name AS service_name,
        COALESCE(s.colour, d.colour) AS colour, a.starts_at, a.ends_at, a.status, a.channel,
        a.booking_reference, a.patient_id, p.name_ciphertext, p.name_key_id, j.status AS job
        FROM appointments a
        JOIN services s ON s.clinic_id=a.clinic_id AND s.id=a.service_id
        JOIN doctors d ON d.clinic_id=a.clinic_id AND d.id=a.doctor_id
        LEFT JOIN patients p ON p.clinic_id=a.clinic_id AND p.id=a.patient_id
        LEFT JOIN LATERAL (SELECT o.status FROM outbox o WHERE o.clinic_id=a.clinic_id
            AND o.appointment_id=a.id AND o.kind IN ('calendar.create', 'calendar.cancel')
            ORDER BY o.created_at DESC, o.appointment_version DESC LIMIT 1) j ON true
        WHERE a.clinic_id=%(clinic)s AND a.starts_at < %(ends)s AND a.ends_at > %(starts)s
        AND (%(cancelled)s OR a.status <> 'cancelled')
        ORDER BY a.starts_at, a.id LIMIT %(limit)s""",
        {
            "clinic": clinic_id,
            "starts": starts,
            "ends": ends,
            "cancelled": include_cancelled,
            "limit": MAX_CALENDAR_APPOINTMENTS + 1,
        },
    ).fetchall()
    truncated = len(rows) > MAX_CALENDAR_APPOINTMENTS
    rows = rows[:MAX_CALENDAR_APPOINTMENTS]
    appointments = []
    viewed = []
    for row in rows:
        name = _patient_name(keyring, row)
        if keyring is not None and row["patient_id"] is not None:
            viewed.append(row["id"])
        appointments.append(
            {
                **{
                    k: row[k]
                    for k in (
                        "id",
                        "doctor_id",
                        "service_id",
                        "service_name",
                        "colour",
                        "starts_at",
                        "ends_at",
                        "status",
                        "channel",
                    )
                },
                "sync_state": SYNC_STATE.get(row["job"], "not_applicable"),
                "patient_name": name,
                "reference": row["booking_reference"],
            }
        )
    exceptions = conn.execute(
        """SELECT id, doctor_id, kind, starts_at, ends_at, label FROM schedule_exceptions
        WHERE clinic_id=%s AND starts_at < %s AND ends_at > %s ORDER BY starts_at, id""",
        (clinic_id, ends, starts),
    ).fetchall()
    audit_many(conn, clinic_id, staff["id"], "appointment.patient_details_viewed", viewed)
    return {
        "timezone": clinic["timezone"],
        "doctors": doctors,
        "appointments": appointments,
        "exceptions": exceptions,
        "truncated": truncated,
    }


# --- Staff bookings ----------------------------------------------------------------------------


def book(url, settings, staff, request, idempotency_key=None, ports=None) -> dict:
    """A booking made by staff (channel staff) for any active service of an active doctor,
    named by catalogue id or service row id (404 not_found otherwise). The patient is a new
    row from the typed name and phone; consents (source staff) only when consent_contact.
    Audited as 'staff.booking_created' with the staff member as actor. Same response as a
    web booking; the same Idempotency-Key replays it."""
    keyring, hash_key = public.crypto(settings)
    clinic_id = staff["clinic_id"]
    with connect(url) as conn:
        clinic = conn.execute(
            "SELECT id, name, timezone FROM clinics WHERE id=%s", (clinic_id,)
        ).fetchone()
        found = public.offerings(
            conn, clinic_id, request.service_id, request.doctor_id, public_only=False
        )
    if not found:
        raise public.fail(public.NOT_FOUND)
    result = public.create_booking(
        url,
        settings,
        clinic,
        found[0],
        starts_at=request.starts_at,
        name=request.name,
        phone=request.phone,
        notes=request.notes,
        channel="staff",
        consent=request.consent_contact,
        key=public.booking_key(STAFF_KEY_PREFIX, idempotency_key),
        keyring=keyring,
        hash_key=hash_key,
        actor_id=staff["id"],
        audit_action="staff.booking_created",
        ports=ports,
    )
    return public.public_booking(result)


# --- Conversations -----------------------------------------------------------------------------


CHAT_SQL = """SELECT s.id, s.clinic_id, s.created_at AS started_at, s.last_active_at,
    s.status, s.turn_count AS turns, s.patient_id, p.name_ciphertext, p.name_key_id,
    EXISTS (SELECT 1 FROM chat_proposals cp WHERE cp.session_id=s.id
        AND cp.appointment_id IS NOT NULL) AS booked
    FROM chat_sessions s
    LEFT JOIN patients p ON p.clinic_id=s.clinic_id AND p.id=s.patient_id
    WHERE s.clinic_id=%(clinic)s"""
VOICE_SQL = """SELECT cs.id, cs.started_at, cs.ended_at, cs.status, cs.provider_status,
    cs.call_type, cs.conversation_seconds, cs.hangup_by,
    EXISTS (SELECT 1 FROM appointments a WHERE a.clinic_id=cs.clinic_id
        AND a.call_session_id=cs.id) AS booked
    FROM call_sessions cs WHERE cs.clinic_id=%(clinic)s"""


def _chat_out(row, keyring) -> dict:
    return {
        "id": row["id"],
        "channel": "chat",
        "started_at": row["started_at"],
        "status": row["status"],
        "turns": row["turns"],
        "patient_name": _patient_name(keyring, row),
        "outcome": "booked" if row["booked"] else None,
    }


def _voice_out(row) -> dict:
    return {
        "id": row["id"],
        "channel": "voice",
        "started_at": row["started_at"],
        "status": row["status"],
        "turns": 0,  # No voice transcript is stored.
        "patient_name": None,
        "outcome": "booked" if row["booked"] else None,
    }


def conversations(conn, staff, keyring, channel=None, limit=50) -> list[dict]:
    """Chat sessions and voice call sessions, newest first (channel filters one kind)."""
    clinic_id = staff["clinic_id"]
    keyring = _details(staff, keyring)
    params = {"clinic": clinic_id, "limit": limit}
    result = []
    if channel in (None, "chat"):
        rows = conn.execute(
            CHAT_SQL + " ORDER BY s.created_at DESC, s.id LIMIT %(limit)s", params
        ).fetchall()
        viewed = []
        for row in rows:
            out = _chat_out(row, keyring)
            if out["patient_name"] is not None:
                viewed.append(row["id"])
            result.append(out)
        audit_many(conn, clinic_id, staff["id"], "conversation.patient_details_viewed", viewed)
    if channel in (None, "voice"):
        rows = conn.execute(
            VOICE_SQL + " ORDER BY cs.started_at DESC, cs.id LIMIT %(limit)s", params
        ).fetchall()
        result.extend(_voice_out(row) for row in rows)
    result.sort(key=lambda c: c["started_at"], reverse=True)
    return result[:limit]


def _summary(message) -> dict:
    """A staff-facing transcript line. Tool results are summarised (tool name and outcome
    only, never the raw result); assistant tool calls name the tools called."""
    document = message["document"]
    role, text, tool_name = message["role"], None, message["tool_name"]
    if document is not None:
        if role == "tool":
            result = document.get("result") or {}
            error = result.get("error") if isinstance(result, dict) else None
            text = f"{tool_name or 'tool'}: {error or 'ok'}"
        elif role == "assistant" and document.get("tool_calls"):
            names = [str(c.get("name")) for c in document["tool_calls"]]
            text = document.get("text") or None
            tool_name = ",".join(names)[:200]
        else:
            value = document.get("text")
            text = value if isinstance(value, str) else None
    return {
        "role": role,
        "text": text,
        "tool_name": tool_name,
        "created_at": message["created_at"],
    }


def conversation(conn, staff, keyring, conversation_id) -> dict:
    """One conversation: a chat transcript (decrypted for owner/admin, audited as
    'conversation.transcript_viewed'; viewers get roles and times with text null) or a
    voice call summary with its linked appointments. 404 outside the clinic."""
    clinic_id = staff["clinic_id"]
    details = _details(staff, keyring)
    row = conn.execute(
        CHAT_SQL + " AND s.id=%(id)s", {"clinic": clinic_id, "id": conversation_id}
    ).fetchone()
    if row is not None:
        out = _chat_out(row, details)
        messages = []
        if details is not None:
            messages = [_summary(m) for m in chat.transcript(conn, details, clinic_id, row["id"])]
            chat.audit_staff_view(conn, staff, row["id"])
        else:
            messages = [
                {
                    "role": m["role"],
                    "text": None,
                    "tool_name": m["tool_name"],
                    "created_at": m["created_at"],
                }
                for m in conn.execute(
                    "SELECT role, tool_name, created_at FROM chat_messages"
                    " WHERE clinic_id=%s AND session_id=%s ORDER BY seq",
                    (clinic_id, row["id"]),
                ).fetchall()
            ]
        appointments = conn.execute(
            """SELECT a.id, a.booking_reference AS reference, a.status, a.starts_at, a.channel
            FROM chat_proposals cp JOIN appointments a ON a.clinic_id=cp.clinic_id
                AND a.id=cp.appointment_id
            WHERE cp.clinic_id=%s AND cp.session_id=%s ORDER BY a.starts_at""",
            (clinic_id, row["id"]),
        ).fetchall()
        return {
            **out,
            "last_active_at": row["last_active_at"],
            "messages": messages,
            "appointments": appointments,
        }
    row = conn.execute(
        VOICE_SQL + " AND cs.id=%(id)s", {"clinic": clinic_id, "id": conversation_id}
    ).fetchone()
    if row is None:
        raise HTTPException(404, NOT_FOUND)
    appointments = conn.execute(
        """SELECT id, booking_reference AS reference, status, starts_at, channel
        FROM appointments WHERE clinic_id=%s AND call_session_id=%s ORDER BY starts_at""",
        (clinic_id, row["id"]),
    ).fetchall()
    return {
        **_voice_out(row),
        "ended_at": row["ended_at"],
        "provider_status": row["provider_status"],
        "call_type": row["call_type"],
        "duration_seconds": row["conversation_seconds"],
        "hangup_by": row["hangup_by"],
        "appointments": appointments,
    }


# --- Stats -------------------------------------------------------------------------------------


def _midnight(day, zone) -> datetime:
    return min(datetime.combine(day, time(0)).replace(tzinfo=zone, fold=fold) for fold in (0, 1))


def stats(conn, staff) -> dict:
    """Counts for the dashboard (clinic-local days):
    today: active appointments starting today; week: active appointments starting in the 7
    days from today; needs_review: appointments in needs_review; open_callbacks: callback
    requests open; by_channel: appointments created in the last 30 days, by channel."""
    clinic_id = staff["clinic_id"]
    zone = ZoneInfo(
        conn.execute("SELECT timezone FROM clinics WHERE id=%s", (clinic_id,)).fetchone()[
            "timezone"
        ]
    )
    now = service.utc_now()
    today = now.astimezone(zone).date()
    start = _midnight(today, zone)
    tomorrow = _midnight(today + timedelta(days=1), zone)
    week_end = _midnight(today + timedelta(days=7), zone)
    row = conn.execute(
        """SELECT
        (SELECT count(*) FROM appointments WHERE clinic_id=%(c)s AND status = ANY(%(active)s)
            AND starts_at >= %(start)s AND starts_at < %(tomorrow)s) AS today,
        (SELECT count(*) FROM appointments WHERE clinic_id=%(c)s AND status = ANY(%(active)s)
            AND starts_at >= %(start)s AND starts_at < %(week_end)s) AS week,
        (SELECT count(*) FROM appointments WHERE clinic_id=%(c)s
            AND status='needs_review') AS needs_review,
        (SELECT count(*) FROM callback_requests WHERE clinic_id=%(c)s
            AND status='open') AS open_callbacks""",
        {
            "c": clinic_id,
            "active": list(ACTIVE),
            "start": start,
            "tomorrow": tomorrow,
            "week_end": week_end,
        },
    ).fetchone()
    channels = {"voice": 0, "web": 0, "chat": 0, "staff": 0}
    for item in conn.execute(
        # created_at is the database clock, so the window is too.
        """SELECT channel, count(*) AS n FROM appointments WHERE clinic_id=%s
        AND created_at >= now() - interval '30 days' GROUP BY channel""",
        (clinic_id,),
    ).fetchall():
        channels[item["channel"]] = item["n"]
    return {**row, "by_channel": channels}
