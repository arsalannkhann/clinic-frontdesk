"""Retention purge (T7). Run by an operator (`clinic-voice retention purge [--dry-run]`);
nothing schedules it.

- Patients whose retention_until has passed, and who hold no active appointment that has not
  ended, are deleted with their consent and opt-out rows. Their appointments (and planned
  contacts) stay with patient_id cleared and their booking notes (encrypted, possibly
  health details, migration 018) erased, so they no longer carry patient data.
- Callback requests (migration 018) linked to a purged patient are unlinked. Callback
  requests created more than `session_days` ago are deleted (they hold an encrypted name,
  phone and reason, like a call session's caller number).
- Webhook inbox events received more than `session_days` ago are deleted (before call
  sessions are locked; lock order in locks.py). They hold minimized codes only.
- Call sessions that started more than `session_days` ago are deleted with their
  verification records (ON DELETE CASCADE). appointments.call_session_id and
  appointment_changes.call_session_id are cleared first: those foreign keys have no ON
  DELETE action.

Cross-tenant maintenance like the worker: every statement joins on (clinic_id, id). One
transaction; a dry run rolls back. Returns counts only.
"""

from clinic_voice.domain import ACTIVE_STATUSES

PAIRS = "(clinic_id, {column}) IN (SELECT * FROM unnest(%s::uuid[], %s::uuid[]))"


def _pairs(rows):
    return [r["clinic_id"] for r in rows], [r["id"] for r in rows]


def _count(conn, sql, params):
    return conn.execute(sql, params).rowcount


def purge(conn, session_days: int, dry_run: bool = False) -> dict[str, int]:
    patients = conn.execute(
        """SELECT p.clinic_id, p.id FROM patients p
        WHERE p.retention_until < now() AND NOT EXISTS (
            SELECT 1 FROM appointments a WHERE a.clinic_id=p.clinic_id AND a.patient_id=p.id
            AND a.status = ANY(%s) AND a.blocks_until > now())
        ORDER BY p.clinic_id, p.id FOR UPDATE OF p""",
        (list(ACTIVE_STATUSES),),
    ).fetchall()
    patient_pairs = _pairs(patients)
    result = {
        "patients": len(patients),
        "patient_appointments_unlinked": _count(
            conn,
            "UPDATE appointments SET patient_id=NULL, notes_ciphertext=NULL, notes_key_id=NULL,"
            " updated_at=now() WHERE " + PAIRS.format(column="patient_id"),
            patient_pairs,
        ),
        "consents": _count(
            conn, "DELETE FROM consents WHERE " + PAIRS.format(column="patient_id"), patient_pairs
        ),
        "contact_preferences": _count(
            conn,
            "DELETE FROM contact_preferences WHERE " + PAIRS.format(column="patient_id"),
            patient_pairs,
        ),
    }
    conn.execute(
        "UPDATE callback_requests SET patient_id=NULL WHERE " + PAIRS.format(column="patient_id"),
        patient_pairs,
    )
    conn.execute("DELETE FROM patients WHERE " + PAIRS.format(column="id"), patient_pairs)
    result["callback_requests"] = _count(
        conn,
        "DELETE FROM callback_requests WHERE created_at < now() - make_interval(days => %s)",
        (session_days,),
    )
    result["webhook_events"] = _count(
        conn,
        "DELETE FROM webhook_inbox WHERE received_at < now() - make_interval(days => %s)",
        (session_days,),
    )
    sessions = conn.execute(
        """SELECT clinic_id, id FROM call_sessions
        WHERE started_at < now() - make_interval(days => %s)
        ORDER BY clinic_id, id FOR UPDATE""",
        (session_days,),
    ).fetchall()
    session_pairs = _pairs(sessions)
    result["session_appointments_unlinked"] = _count(
        conn,
        "UPDATE appointments SET call_session_id=NULL, updated_at=now() WHERE "
        + PAIRS.format(column="call_session_id"),
        session_pairs,
    )
    conn.execute(
        "UPDATE appointment_changes SET call_session_id=NULL WHERE "
        + PAIRS.format(column="call_session_id"),
        session_pairs,
    )
    conn.execute("DELETE FROM call_sessions WHERE " + PAIRS.format(column="id"), session_pairs)
    result["call_sessions"] = len(sessions)
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    return result
