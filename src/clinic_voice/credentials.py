import hashlib
import re
import secrets
from datetime import timedelta
from uuid import UUID, uuid4

# Format: <prefix>_<credential id hex>.<43-char urlsafe secret>. The id is a lookup handle,
# not a secret. The prefix names the scope: cvt_ tool credentials (Authorization header of
# the tool APIs), cvw_ webhook credentials (path segment of /v1/webhooks/bolna/{token}).
PREFIXES = {"tools": "cvt", "webhook": "cvw"}
PATTERNS = {
    scope: re.compile(rf"^{prefix}_([0-9a-f]{{32}})\.([A-Za-z0-9_-]{{43}})$")
    for scope, prefix in PREFIXES.items()
}
TOKEN_PATTERN = PATTERNS["tools"]
LAST_USED_RESOLUTION = timedelta(minutes=5)


def digest(secret: str) -> bytes:
    # The secret carries 256 random bits, so a fast digest suffices; no password stretching needed.
    return hashlib.sha256(secret.encode()).digest()


def parse(token: str, scope: str = "tools"):
    if not isinstance(token, str):
        return None
    match = PATTERNS[scope].fullmatch(token)
    if not match:
        return None
    return UUID(match.group(1)), match.group(2)


def _insert(conn, clinic_id, label, rotated_from=None, expires_at=None, scope="tools"):
    prefix = PREFIXES[scope]
    credential_id, secret = uuid4(), secrets.token_urlsafe(32)
    conn.execute(
        """INSERT INTO tenant_credentials (id,clinic_id,scope,label,secret_hash,rotated_from,expires_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (credential_id, clinic_id, scope, label, digest(secret), rotated_from, expires_at),
    )
    return credential_id, f"{prefix}_{credential_id.hex}.{secret}"


def issue(conn, clinic_id, label, expires_at=None, scope="tools"):
    """Return (credential_id, token). The token is not recoverable after this call."""
    return _insert(conn, clinic_id, label, expires_at=expires_at, scope=scope)


def rotate(conn, credential_id, grace: timedelta):
    """Issue a replacement of the same scope; the old credential stays valid for at most
    `grace`."""
    old = conn.execute(
        """SELECT clinic_id, label, scope FROM tenant_credentials WHERE id=%s AND revoked_at IS NULL
        AND (expires_at IS NULL OR expires_at > now()) FOR UPDATE""",
        (credential_id,),
    ).fetchone()
    if not old:
        raise LookupError("Credential not found or no longer active")
    conn.execute(
        """UPDATE tenant_credentials SET expires_at=LEAST(COALESCE(expires_at, 'infinity'), now() + %s)
        WHERE id=%s""",
        (grace, credential_id),
    )
    return _insert(
        conn, old["clinic_id"], old["label"], rotated_from=credential_id, scope=old["scope"]
    )


def revoke(conn, credential_id) -> bool:
    row = conn.execute(
        "UPDATE tenant_credentials SET revoked_at=now() WHERE id=%s AND revoked_at IS NULL RETURNING id",
        (credential_id,),
    ).fetchone()
    return row is not None


def authenticate(conn, token: str, scope: str = "tools"):
    """Return the clinic for an active credential of `scope`, else None. Callers must not say
    why. A token of one scope never authenticates for another (prefix and stored scope)."""
    parsed = parse(token, scope)
    if not parsed:
        return None
    credential_id, secret = parsed
    row = conn.execute(
        """SELECT clinic_id, secret_hash, last_used_at < now() - %s AS stale_use,
        last_used_at IS NULL AS never_used FROM tenant_credentials
        WHERE id=%s AND scope=%s AND revoked_at IS NULL
        AND (expires_at IS NULL OR expires_at > now())""",
        (LAST_USED_RESOLUTION, credential_id, scope),
    ).fetchone()
    # Compare against a dummy digest on a miss so lookups and mismatches take similar work.
    expected = row["secret_hash"] if row else bytes(32)
    if not secrets.compare_digest(digest(secret), bytes(expected)) or not row:
        return None
    if row["never_used"] or row["stale_use"]:
        # Coarse usage tracking tells operators when a rotated credential can be revoked.
        conn.execute(
            "UPDATE tenant_credentials SET last_used_at=now() WHERE id=%s", (credential_id,)
        )
    return row["clinic_id"]


def list_for_clinic(conn, clinic_id):
    return conn.execute(
        """SELECT id, label, scope, rotated_from, created_at, expires_at, revoked_at, last_used_at
        FROM tenant_credentials WHERE clinic_id=%s ORDER BY created_at""",
        (clinic_id,),
    ).fetchall()
