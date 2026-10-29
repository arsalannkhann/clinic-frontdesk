import base64
import hashlib
import re
import secrets
from datetime import timedelta
from functools import cache
from uuid import UUID, uuid4

from fastapi import HTTPException
from psycopg.errors import UniqueViolation

from clinic_voice.audit import audit
from clinic_voice.credentials import digest

ROLES = ("owner", "admin", "viewer")
SESSION_LIFETIME = timedelta(hours=12)
MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)
# scrypt cost; parameters are stored per hash so they can be raised later (rehash on login).
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
SESSION_PATTERN = re.compile(r"^cvs_([0-9a-f]{32})\.([A-Za-z0-9_-]{43})$")


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _scrypt(password, salt, n, r, p):
    return hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, maxmem=256 * 1024 * 1024, dklen=32
    )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(derived)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        derived = _scrypt(password, _unb64(salt), int(n), int(r), int(p))
    except ValueError:
        return False
    return secrets.compare_digest(derived, _unb64(expected))


def needs_rehash(stored: str) -> bool:
    return stored.split("$")[1:4] != [str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P)]


@cache
def _dummy_hash():
    return hash_password(secrets.token_urlsafe(16))


def normalize_email(email: str) -> str:
    return email.strip().lower()


def parse_session(token: str):
    match = SESSION_PATTERN.fullmatch(token)
    if not match:
        return None
    return UUID(match.group(1)), match.group(2)


def public(user):
    return {"id": user["id"], "email": user["email"], "role": user["role"]}


def create_user(conn, clinic_id, email, role, password, actor_id=None):
    if role not in ROLES:
        raise ValueError("Unknown role")
    user_id = uuid4()
    try:
        row = conn.execute(
            """INSERT INTO staff_users (id,clinic_id,email,role,password_hash)
            VALUES (%s,%s,%s,%s,%s) RETURNING *""",
            (user_id, clinic_id, normalize_email(email), role, hash_password(password)),
        ).fetchone()
    except UniqueViolation:
        raise HTTPException(409, "Email already registered") from None
    audit(conn, clinic_id, actor_id, "staff.user_created", user_id)
    return public(row)


def login(conn, email, password):
    """Return a session dict or None. Unknown, disabled, locked and wrong-password look alike."""
    user = conn.execute(
        """SELECT *, locked_until > now() AS locked FROM staff_users
        WHERE email=%s AND disabled_at IS NULL""",
        (normalize_email(email),),
    ).fetchone()
    # Always derive a hash so response time does not reveal whether the account exists.
    valid = verify_password(password, user["password_hash"] if user else _dummy_hash())
    if not user:
        return None
    if user["locked"] or not valid:
        if not user["locked"]:
            # An expired lock restarts the count instead of relocking on the next failure.
            conn.execute(
                """UPDATE staff_users SET
                failed_logins = CASE WHEN locked_until IS NOT NULL THEN 1 ELSE failed_logins + 1 END,
                locked_until = CASE
                    WHEN locked_until IS NULL AND failed_logins + 1 >= %s THEN now() + %s END
                WHERE id=%s""",
                (MAX_FAILED_LOGINS, LOCKOUT, user["id"]),
            )
        audit(conn, user["clinic_id"], user["id"], "staff.login_failed", user["id"])
        return None
    conn.execute(
        "UPDATE staff_users SET failed_logins=0, locked_until=NULL WHERE id=%s", (user["id"],)
    )
    if needs_rehash(user["password_hash"]):
        conn.execute(
            "UPDATE staff_users SET password_hash=%s WHERE id=%s",
            (hash_password(password), user["id"]),
        )
    session = _new_session(conn, user)
    audit(conn, user["clinic_id"], user["id"], "staff.login", user["id"])
    return session


def _new_session(conn, user):
    session_id, secret = uuid4(), secrets.token_urlsafe(32)
    row = conn.execute(
        """INSERT INTO staff_sessions (id,clinic_id,user_id,secret_hash,expires_at)
        VALUES (%s,%s,%s,%s,now() + %s) RETURNING expires_at""",
        (session_id, user["clinic_id"], user["id"], digest(secret), SESSION_LIFETIME),
    ).fetchone()
    return {
        "token": f"cvs_{session_id.hex}.{secret}",
        "expires_at": row["expires_at"],
        "user": public(user),
    }


def authenticate_session(conn, token):
    """Return the staff context for an active session, else None."""
    parsed = parse_session(token)
    if not parsed:
        return None
    session_id, secret = parsed
    row = conn.execute(
        """SELECT s.id AS session_id, s.secret_hash, u.id, u.clinic_id, u.email, u.role
        FROM staff_sessions s JOIN staff_users u ON u.clinic_id=s.clinic_id AND u.id=s.user_id
        WHERE s.id=%s AND s.revoked_at IS NULL AND s.expires_at > now()
        AND u.disabled_at IS NULL""",
        (session_id,),
    ).fetchone()
    expected = row["secret_hash"] if row else bytes(32)
    if not secrets.compare_digest(digest(secret), bytes(expected)) or not row:
        return None
    return {key: row[key] for key in ("session_id", "id", "clinic_id", "email", "role")}


def logout(conn, staff):
    conn.execute(
        "UPDATE staff_sessions SET revoked_at=now() WHERE id=%s AND revoked_at IS NULL",
        (staff["session_id"],),
    )
    audit(conn, staff["clinic_id"], staff["id"], "staff.logout", staff["id"])


def _revoke_sessions(conn, user_id, keep=None):
    conn.execute(
        """UPDATE staff_sessions SET revoked_at=now()
        WHERE user_id=%s AND revoked_at IS NULL AND id IS DISTINCT FROM %s""",
        (user_id, keep),
    )


def change_password(conn, staff, current, new):
    row = conn.execute(
        "SELECT password_hash FROM staff_users WHERE id=%s FOR UPDATE", (staff["id"],)
    ).fetchone()
    if not verify_password(current, row["password_hash"]):
        raise HTTPException(403, "Current password is incorrect")
    conn.execute(
        "UPDATE staff_users SET password_hash=%s, password_changed_at=now() WHERE id=%s",
        (hash_password(new), staff["id"]),
    )
    _revoke_sessions(conn, staff["id"], keep=staff["session_id"])
    audit(conn, staff["clinic_id"], staff["id"], "staff.password_changed", staff["id"])


def set_password(conn, email, password):
    """Operator recovery: reset a password, clear lockout and end every session."""
    row = conn.execute(
        """UPDATE staff_users SET password_hash=%s, password_changed_at=now(), failed_logins=0,
        locked_until=NULL WHERE email=%s RETURNING id, clinic_id""",
        (hash_password(password), normalize_email(email)),
    ).fetchone()
    if not row:
        raise LookupError("Staff user not found")
    _revoke_sessions(conn, row["id"])
    audit(conn, row["clinic_id"], None, "staff.password_reset", row["id"])


def list_users(conn, clinic_id):
    return conn.execute(
        """SELECT id, email, role, disabled_at IS NOT NULL AS disabled
        FROM staff_users WHERE clinic_id=%s ORDER BY created_at, email""",
        (clinic_id,),
    ).fetchall()


def update_user(conn, staff, user_id, role=None, disabled=None):
    clinic_id = staff["clinic_id"]
    # Lock the clinic's active owners in a fixed order so concurrent demotions serialize.
    owners = conn.execute(
        """SELECT id FROM staff_users WHERE clinic_id=%s AND role='owner' AND disabled_at IS NULL
        ORDER BY id FOR UPDATE""",
        (clinic_id,),
    ).fetchall()
    owner_ids = {row["id"] for row in owners}
    if staff["id"] not in owner_ids:
        raise HTTPException(403, "Owner role required")  # Actor changed since authentication.
    target = conn.execute(
        "SELECT * FROM staff_users WHERE clinic_id=%s AND id=%s FOR UPDATE", (clinic_id, user_id)
    ).fetchone()
    if not target:
        raise HTTPException(404, "Staff user not found")
    new_role = role or target["role"]
    stays_owner = new_role == "owner" and not disabled
    if target["id"] in owner_ids and not stays_owner and len(owner_ids) == 1:
        raise HTTPException(409, "A clinic must keep at least one active owner")
    row = conn.execute(
        """UPDATE staff_users SET role=%s,
        disabled_at = CASE WHEN %s::boolean IS NULL THEN disabled_at
            WHEN %s::boolean THEN COALESCE(disabled_at, now()) ELSE NULL END
        WHERE id=%s RETURNING *""",
        (new_role, disabled, disabled, user_id),
    ).fetchone()
    if disabled:
        _revoke_sessions(conn, user_id)
    audit(conn, clinic_id, staff["id"], "staff.user_updated", user_id)
    return public(row) | {"disabled": row["disabled_at"] is not None}
