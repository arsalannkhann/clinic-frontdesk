"""Database-backed fixed-window rate limits for the public API (migration 018).

A counter row is (HMAC-SHA256(PHONE_HASH_KEY, "rate:<limit>:<value>"), window start). The
value is a client address or "<clinic>:<phone>"; neither is stored or logged. Windows are
aligned to the epoch on the database clock, so every serverless instance counts into the
same row. Each check counts the attempt (allowed or not) in its own committed transaction,
before any booking work, so refused attempts are counted too and a burst cannot slip
through between instances. Rows of old windows are pruned by worker housekeeping.
"""

import hashlib
import hmac
from dataclasses import dataclass
from datetime import timedelta

# Pruning keeps a margin over the longest window used (a day).
RETENTION = timedelta(days=2)


@dataclass(frozen=True)
class Limit:
    name: str
    limit: int
    window: timedelta


def key_hash(hash_key: bytes, name: str, value: str) -> bytes:
    return hmac.new(hash_key, f"rate:{name}:{value}".encode(), hashlib.sha256).digest()


def hit(conn, hash_key: bytes, limit: Limit, value: str) -> bool:
    """Count one attempt and commit; True while the window's count is within the limit."""
    seconds = int(limit.window.total_seconds())
    row = conn.execute(
        """INSERT INTO public_rate_limits (key_hash, window_start, count)
        VALUES (%s, to_timestamp(floor(extract(epoch FROM now()) / %s) * %s), 1)
        ON CONFLICT (key_hash, window_start)
        DO UPDATE SET count = public_rate_limits.count + 1
        RETURNING count""",
        (key_hash(hash_key, limit.name, value), seconds, seconds),
    ).fetchone()
    conn.commit()
    return row["count"] <= limit.limit


def prune(conn) -> int:
    """Delete counters of windows that ended long ago (housekeeping) and commit."""
    count = conn.execute(
        "DELETE FROM public_rate_limits WHERE window_start < now() - %s", (RETENTION,)
    ).rowcount
    conn.commit()
    return count
