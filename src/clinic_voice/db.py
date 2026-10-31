"""Database connections and the migration runner.

Transaction pooling (Supabase Supavisor on port 6543, PgBouncer in transaction mode) hands
each transaction to whichever server connection is free, so nothing may outlive a
transaction. The code relies on none of that: locks are transaction-scoped
(pg_advisory_xact_lock*, row locks), no SET without LOCAL, no LISTEN/NOTIFY, no temporary
tables and no named (server-side) cursors. The one session feature psycopg uses by default
is prepared statements (after `prepare_threshold` executions of a query), which break when
the next execution lands on another server connection; connect() turns them off for a
pooled URL.

Pooled: DATABASE_POOLER=transaction in the process environment, or, when it is unset, a URL
whose port is 6543. DATABASE_POOLER=session (or none) keeps prepared statements even on
6543. It is read from the environment directly (not .env) because every connect() needs it,
including the CLI, tests and the worker, which do not all load Settings first.

Migrations take a transaction-scoped advisory lock and run in one transaction, so they are
pooler-safe too; run them through the direct or session connection anyway (operations docs),
since a long DDL transaction holds a pooled server connection for its whole duration.
"""

import os
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

TRANSACTION_POOLER_PORT = "6543"
POOLER_MODES = ("transaction", "session", "none")


def transaction_pooled(url: str) -> bool:
    """Whether connections to url go through a transaction-mode pooler."""
    mode = (os.environ.get("DATABASE_POOLER") or "").strip().lower()
    if mode:
        if mode not in POOLER_MODES:
            raise ValueError("DATABASE_POOLER must be transaction, session or none")
        return mode == "transaction"
    port = str(conninfo_to_dict(url).get("port") or "")
    # A multi-host URL lists ports comma-separated; any pooled host makes it pooled.
    return TRANSACTION_POOLER_PORT in port.split(",")


def connect(url: str):
    options = {"row_factory": dict_row, "connect_timeout": 5}
    if transaction_pooled(url):
        options["prepare_threshold"] = None  # Never prepare: the next server may differ.
    return psycopg.connect(url, **options)


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
