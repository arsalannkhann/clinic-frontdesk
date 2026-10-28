"""Google Calendar account connection (OAuth authorization code + PKCE) and per-clinic
calendar port resolution for the worker.

Secrets: the OAuth state is stored only as a SHA-256 digest; the PKCE verifier and refresh
token are AES-256-GCM ciphertext (crypto.Keyring) with associated data binding them to their
row. Nothing here logs or returns tokens, codes, client secrets, provider bodies or emails;
audit rows carry ids only.
"""

import base64
import hashlib
import logging
import secrets
import string
from datetime import timedelta
from threading import Lock
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from clinic_voice.audit import audit
from clinic_voice.crypto import CryptoError, Keyring
from clinic_voice.providers import google_calendar as google
from clinic_voice.providers.calendar import ProviderPermanent, ProviderRetryable

log = logging.getLogger("clinic_voice.calendar_accounts")

STATE_TTL = timedelta(minutes=10)
VERIFIER_ALPHABET = string.ascii_letters + string.digits
VERIFIER_LENGTH = 64  # RFC 7636 / Google: 43 to 128 characters.
NOT_CONFIGURED = "Calendar integration not configured"
INVALID_STATE = "Invalid or expired authorization request"
ACCOUNT_COLUMNS = "id,provider,status,scopes,connected_by,created_at,revoked_at"


def not_configured():
    return HTTPException(503, NOT_CONFIGURED)


def keyring_from(settings) -> Keyring:
    """The token keyring, or 503 if Google or the keyring is not configured."""
    if not settings.google_configured():
        raise not_configured()
    try:
        return Keyring.from_setting(settings.token_encryption_keys.get_secret_value())
    except CryptoError:
        log.error("token encryption keyring is invalid")
        raise not_configured() from None


def account_aad(clinic_id: UUID, account_id: UUID) -> bytes:
    return b"calendar_account:" + clinic_id.bytes + account_id.bytes


def state_aad(digest: bytes, clinic_id: UUID, user_id: UUID) -> bytes:
    return b"oauth_state:" + digest + clinic_id.bytes + user_id.bytes


def digest(state: str) -> bytes:
    return hashlib.sha256(state.encode()).digest()


def challenge(verifier: str) -> str:
    raw = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def active_accounts(conn, clinic_id) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM calendar_accounts WHERE clinic_id=%s AND status='active'",
        (clinic_id,),
    ).fetchone()["n"]


def start(conn, staff, settings, replace=False) -> str:
    """Create a single-use state and return the Google authorization URL.

    If the clinic already has an active account, 409 unless replace=True; then the callback
    revokes the previously active account(s) after storing the new one, so the "clinic's
    only active account" fallback stays deterministic."""
    keyring = keyring_from(settings)
    clinic_id, user_id = staff["clinic_id"], staff["id"]
    if not replace and active_accounts(conn, clinic_id):
        raise HTTPException(409, "The clinic already has an active calendar account")
    state = secrets.token_urlsafe(32)
    verifier = "".join(secrets.choice(VERIFIER_ALPHABET) for _ in range(VERIFIER_LENGTH))
    state_digest = digest(state)
    key_id, blob = keyring.encrypt(verifier.encode(), state_aad(state_digest, clinic_id, user_id))
    conn.execute("DELETE FROM oauth_states WHERE clinic_id=%s AND expires_at < now()", (clinic_id,))
    conn.execute(
        "INSERT INTO oauth_states (state_digest,clinic_id,user_id,code_verifier_ciphertext,"
        "key_id,expires_at,replace_existing) VALUES (%s,%s,%s,%s,%s,now() + %s,%s)",
        (state_digest, clinic_id, user_id, blob, key_id, STATE_TTL, replace),
    )
    audit(conn, clinic_id, user_id, "calendar.authorization_started")
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": " ".join(google.SCOPES),
        "state": state,
        "code_challenge": challenge(verifier),
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
    }
    return google.AUTH_ENDPOINT + "?" + urlencode(params)


def prune_oauth_states(conn) -> int:
    """Delete expired OAuth states of every clinic (worker housekeeping) and commit. An
    expired state is useless; a replay of a pruned state is rejected as unknown."""
    count = conn.execute("DELETE FROM oauth_states WHERE expires_at < now()").rowcount
    conn.commit()
    return count


def _consume_state(conn, state):
    """Mark the state used atomically; None if unknown, expired, used, or its user can no
    longer administer the clinic."""
    row = conn.execute(
        "UPDATE oauth_states s SET used_at=now()"
        " FROM staff_users u"
        " WHERE s.state_digest=%s AND s.used_at IS NULL AND s.expires_at > now()"
        " AND u.clinic_id=s.clinic_id AND u.id=s.user_id"
        " RETURNING s.state_digest, s.clinic_id, s.user_id, s.code_verifier_ciphertext,"
        " s.key_id, s.replace_existing, u.role, u.disabled_at",
        (digest(state),),
    ).fetchone()
    conn.commit()  # Used even if everything below fails: a state never works twice.
    if row is None or row["disabled_at"] is not None or row["role"] not in ("owner", "admin"):
        return None
    return row


def _revoke_locally(conn, clinic_id, actor_id, account_id):
    """Stop using the account now and enqueue the durable Google revocation (the handler
    keeps the ciphertext until Google confirms). Caller commits."""
    conn.execute(
        "UPDATE calendar_accounts SET status='revoked', revoked_at=now()"
        " WHERE clinic_id=%s AND id=%s AND status <> 'revoked'",
        (clinic_id, account_id),
    )
    conn.execute(
        "INSERT INTO outbox (id,clinic_id,kind,payload) VALUES (%s,%s,'calendar.revoke_grant',%s)",
        (uuid4(), clinic_id, Jsonb({"account_id": str(account_id)})),
    )
    audit(conn, clinic_id, actor_id, "calendar.revoked", account_id)


def callback(conn, settings, http, state, code, error) -> dict:
    """Complete an authorization. Raises HTTPException(400) for any invalid request.
    Returns {account_id, active_accounts} (the clinic's active account count afterwards)."""
    keyring = keyring_from(settings)
    if not state or len(state) > 256:
        raise HTTPException(400, INVALID_STATE)
    row = _consume_state(conn, state)
    if row is None:
        raise HTTPException(400, INVALID_STATE)
    if error or not code or len(code) > 2048:
        raise HTTPException(400, "Calendar authorization was not granted")
    clinic_id, user_id = row["clinic_id"], row["user_id"]
    try:
        verifier = keyring.decrypt(
            row["key_id"],
            row["code_verifier_ciphertext"],
            state_aad(row["state_digest"], clinic_id, user_id),
        ).decode()
    except CryptoError:
        log.error("oauth state could not be decrypted")
        raise HTTPException(400, INVALID_STATE) from None
    try:
        tokens = google.exchange_code(
            http,
            settings.google_client_id,
            settings.google_client_secret.get_secret_value(),
            code,
            verifier,
            settings.google_redirect_uri,
        )
    except google.OAuthError as failure:
        log.warning("google code exchange failed code=%s", failure.code)
        if failure.retryable:
            raise HTTPException(502, "Calendar provider unavailable; try again") from None
        raise HTTPException(400, "Calendar authorization failed") from None
    refresh_token = tokens.get("refresh_token")
    scope = tokens.get("scope")
    granted = set(scope.split()) if isinstance(scope, str) else set()
    if not isinstance(refresh_token, str) or not refresh_token:
        google.revoke_token(http, tokens["access_token"])
        raise HTTPException(400, "Calendar authorization did not grant offline access")
    if not set(google.SCOPES) <= granted:
        google.revoke_token(http, refresh_token)
        raise HTTPException(400, "Calendar authorization did not grant the required access")
    account_id = uuid4()
    key_id, blob = keyring.encrypt(refresh_token.encode(), account_aad(clinic_id, account_id))
    previous = []
    if row["replace_existing"]:
        previous = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM calendar_accounts WHERE clinic_id=%s AND status='active'"
                " FOR UPDATE",
                (clinic_id,),
            ).fetchall()
        ]
    conn.execute(
        "INSERT INTO calendar_accounts (id,clinic_id,provider,refresh_token_ciphertext,key_id,"
        "scopes,status,connected_by) VALUES (%s,%s,'google',%s,%s,%s,'active',%s)",
        (account_id, clinic_id, blob, key_id, sorted(granted & set(google.SCOPES)), user_id),
    )
    audit(conn, clinic_id, user_id, "calendar.connected", account_id)
    for old in previous:
        _revoke_locally(conn, clinic_id, user_id, old)
    active = active_accounts(conn, clinic_id)
    conn.commit()
    return {"account_id": account_id, "active_accounts": active}


def list_accounts(conn, clinic_id):
    return conn.execute(
        f"SELECT {ACCOUNT_COLUMNS} FROM calendar_accounts WHERE clinic_id=%s"  # noqa: S608
        " ORDER BY created_at, id",
        (clinic_id,),
    ).fetchall()


def revoke(conn, staff, account_id) -> None:
    """Mark the account revoked (it stops being used immediately) and enqueue a
    calendar.revoke_grant job that revokes the token at Google and only then forgets the
    ciphertext. Idempotent for an already revoked account. Doctors keep the id; the worker
    treats a revoked account as unmapped."""
    clinic_id = staff["clinic_id"]
    row = conn.execute(
        "SELECT status FROM calendar_accounts"
        " WHERE clinic_id=%s AND id=%s AND provider='google' FOR UPDATE",
        (clinic_id, account_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Not found")
    if row["status"] != "revoked":
        _revoke_locally(conn, clinic_id, staff["id"], account_id)
    conn.commit()


ACCOUNT_SELECT = (
    "SELECT id, status, refresh_token_ciphertext, key_id FROM calendar_accounts"
    " WHERE clinic_id=%s AND provider='google'"
)


class GoogleCalendarResolver:
    """Ports.calendar_for for the Google adapter.

    (conn, clinic_id, doctor_id, account_id) -> (port, account_id) | None. With account_id
    (the account recorded as having written an event) exactly that account is used. Without
    it, the doctor's calendar_account_id; if the doctor has none, the clinic's only active
    Google account (None if zero or several). Only status 'active' resolves: a revoked or
    errored account is unmapped and never falls back to another account.

    One adapter is cached per account (access tokens are reused); it is replaced when the
    stored ciphertext changes and evicted when the account stops resolving. Raises
    ProviderPermanent('credential_unavailable') if the token cannot be decrypted (for
    example its key was removed from the keyring).
    """

    def __init__(self, settings, http: httpx.Client, timeout_seconds=google.DEFAULT_TIMEOUT):
        self._keyring = keyring_from(settings)
        self._client_id = settings.google_client_id
        self._client_secret = settings.google_client_secret.get_secret_value()
        self._http = http
        self.timeout_seconds = timeout_seconds
        self._cache: dict[UUID, tuple[tuple[str, bytes], google.GoogleCalendar]] = {}
        self._lock = Lock()

    def __repr__(self):
        return "GoogleCalendarResolver()"

    def cached_accounts(self) -> set[UUID]:
        with self._lock:
            return set(self._cache)

    def _evict(self, account_id):
        with self._lock:
            self._cache.pop(account_id, None)

    def _account(self, conn, clinic_id, doctor_id, account_id):
        if account_id is None and doctor_id is not None:
            doctor = conn.execute(
                "SELECT calendar_account_id FROM doctors WHERE clinic_id=%s AND id=%s",
                (clinic_id, doctor_id),
            ).fetchone()
            if doctor is None:
                return None
            account_id = doctor["calendar_account_id"]
        if account_id is not None:
            return conn.execute(ACCOUNT_SELECT + " AND id=%s", (clinic_id, account_id)).fetchone()
        if doctor_id is None:
            return None
        rows = conn.execute(
            ACCOUNT_SELECT + " AND status='active' LIMIT 2", (clinic_id,)
        ).fetchall()
        return rows[0] if len(rows) == 1 else None

    def __call__(self, conn, clinic_id, doctor_id, account_id=None):
        try:
            account = self._account(conn, clinic_id, doctor_id, account_id)
        finally:
            conn.commit()  # Provider I/O follows; leave the connection idle.
        if account is None:
            if account_id is not None:
                self._evict(account_id)
            return None
        if account["status"] != "active":
            self._evict(account["id"])
            return None
        blob = bytes(account["refresh_token_ciphertext"])
        fingerprint = (account["key_id"], hashlib.sha256(blob).digest())
        with self._lock:
            cached = self._cache.get(account["id"])
            if cached is not None and cached[0] == fingerprint:
                return cached[1], account["id"]
            self._cache.pop(account["id"], None)
            token = self._decrypt(clinic_id, account["id"], account["key_id"], blob)
            port = google.GoogleCalendar(
                token, self._client_id, self._client_secret, self._http, self.timeout_seconds
            )
            self._cache[account["id"]] = (fingerprint, port)
        return port, account["id"]

    def _decrypt(self, clinic_id, account_id, key_id, blob) -> str:
        try:
            return self._keyring.decrypt(key_id, blob, account_aad(clinic_id, account_id)).decode()
        except CryptoError:
            log.error("calendar account token could not be decrypted")
            raise ProviderPermanent("credential_unavailable") from None

    def mark_error(self, conn, clinic_id, account_id):
        """Google rejected the refresh token (invalid_grant): stop using the account until
        staff reconnect it. Short transaction, committed."""
        updated = conn.execute(
            "UPDATE calendar_accounts SET status='error'"
            " WHERE clinic_id=%s AND id=%s AND status='active' RETURNING id",
            (clinic_id, account_id),
        ).fetchone()
        if updated:
            audit(conn, clinic_id, None, "calendar.credentials_rejected", account_id)
        conn.commit()
        self._evict(account_id)
        if updated:
            log.warning("calendar account credentials rejected account=%s", account_id)

    def revoke_grant(self, conn, clinic_id, account_id) -> bool:
        """Revoke a revoked account's token at Google. True once Google confirmed (or there
        is nothing left to revoke); the caller then clears the ciphertext. Raises
        ProviderRetryable/ProviderPermanent on failure. Does not commit writes."""
        account = conn.execute(ACCOUNT_SELECT + " AND id=%s", (clinic_id, account_id)).fetchone()
        conn.commit()
        if account is None or account["refresh_token_ciphertext"] is None:
            return True
        if account["status"] != "revoked":
            raise ProviderPermanent("account_not_revoked")
        token = self._decrypt(
            clinic_id, account_id, account["key_id"], bytes(account["refresh_token_ciphertext"])
        )
        try:
            google.revoke_grant(self._http, token, self.timeout_seconds)
        except google.OAuthError as error:
            if error.retryable:
                raise ProviderRetryable("revoke_unavailable") from None
            raise ProviderPermanent("revoke_rejected") from None
        return True
