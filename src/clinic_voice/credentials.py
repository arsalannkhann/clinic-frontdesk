import hashlib
import re
import secrets
from datetime import timedelta
from uuid import UUID, uuid4

# Format: <prefix>_<credential id hex>.<43-char urlsafe secret>. The id is a lookup handle,
# not a secret. The prefix names the scope:
# - cvt_ tools: the generic tool API (/v1/tools/*); also accepted by /v1/bolna/* for clinics
#   configured by hand before the bolna scope existed.
# - cvb_ bolna: the Bolna gateway (/v1/bolna/*) only. Provisioning issues these for the
#   agent it creates; /v1/tools/* refuses them, so the agent's credential cannot reach the
#   generic tool API.
# - cvw_ webhook: path segment of /v1/webhooks/bolna/{token}.
PREFIXES = {"tools": "cvt", "bolna": "cvb", "webhook": "cvw"}
PATTERNS = {
    scope: re.compile(rf"^{prefix}_([0-9a-f]{{32}})\.([A-Za-z0-9_-]{{43}})$")
    for scope, prefix in PREFIXES.items()
}
TOKEN_PATTERN = PATTERNS["tools"]
# Scopes /v1/bolna/* accepts, and an agent's api_token may carry: bolna (provisioned), tools
# (a clinic configured by hand with a tools credential).
GATEWAY_SCOPES = ("bolna", "tools")
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


def parse_any(token: str, scopes: tuple[str, ...]):
    """parse() against each scope in turn; the first match, else None."""
    for scope in scopes:
        parsed = parse(token, scope)
        if parsed:
            return parsed
    return None


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
    found = authenticate_credential(conn, token, (scope,))
    return None if found is None else found[0]


def authenticate_credential(conn, token: str, scopes: tuple[str, ...]):
    """(clinic_id, credential_id) for an active credential of one of `scopes`, else None.
    The token's prefix selects the scope it is checked against; the stored scope must match
    it, so a token never authenticates for a scope it was not issued for."""
    parsed = scope = None
    for candidate in scopes:
        parsed = parse(token, candidate)
        if parsed:
            scope = candidate
            break
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
    return row["clinic_id"], credential_id


def list_for_clinic(conn, clinic_id):
    return conn.execute(
        """SELECT id, label, scope, rotated_from, created_at, expires_at, revoked_at, last_used_at
        FROM tenant_credentials WHERE clinic_id=%s ORDER BY created_at""",
        (clinic_id,),
    ).fetchall()
