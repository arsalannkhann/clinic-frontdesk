from pathlib import Path

import psycopg
from psycopg.rows import dict_row


def connect(url: str):
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=5)


def migrate(url: str, directory: Path) -> None:
    """Apply ordered SQL files atomically; reject changed applied migrations."""
    import hashlib

    paths = sorted(directory.glob("*.sql"))
    if not paths:
        raise RuntimeError(f"No migrations found in {directory}; run from the repository root")
    with connect(url) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(729034120)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, checksum text NOT NULL)"
        )
        for path in paths:
            sql = path.read_text()
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            applied = conn.execute(
                "SELECT checksum FROM schema_migrations WHERE name = %s", (path.name,)
            ).fetchone()
            if applied:
                if applied["checksum"] != checksum:
                    raise RuntimeError(f"Applied migration was modified: {path.name}")
                continue
            conn.execute(sql)
            conn.execute("INSERT INTO schema_migrations VALUES (%s, %s)", (path.name, checksum))
