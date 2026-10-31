from uuid import uuid4


def audit(conn, clinic_id, actor_id, action, target_id=None):
    conn.execute(
        "INSERT INTO audit_events (id,clinic_id,actor_id,action,target_id) VALUES (%s,%s,%s,%s,%s)",
        (uuid4(), clinic_id, actor_id, action, target_id),
    )


def audit_many(conn, clinic_id, actor_id, action, target_ids) -> None:
    """One audit row per target, like audit(), written by a single multi-row INSERT (a list
    view of decrypted details audits every row it shows without one round trip each)."""
    targets = list(target_ids)
    if not targets:
        return
    conn.execute(
        """INSERT INTO audit_events (id,clinic_id,actor_id,action,target_id)
        SELECT t.id, %s, %s, %s, t.target FROM unnest(%s::uuid[], %s::uuid[]) AS t(id, target)""",
        (clinic_id, actor_id, action, [uuid4() for _ in targets], targets),
    )
