from uuid import uuid4


def audit(conn, clinic_id, actor_id, action, target_id=None):
    conn.execute(
        "INSERT INTO audit_events (id,clinic_id,actor_id,action,target_id) VALUES (%s,%s,%s,%s,%s)",
        (uuid4(), clinic_id, actor_id, action, target_id),
    )
