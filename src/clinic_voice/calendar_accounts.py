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
from clinic_voice.locks import lock_calendar_accounts
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
    retires the previously active account(s) after storing the new one (token forgotten
    locally, not revoked at Google; see _supersede), so the "clinic's
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


def _supersede(conn, clinic_id, actor_id, account_id):
    """Retire an account replaced by a new connection: forget its token locally and do NOT
    revoke it at Google. Google's revocation is grant-wide (all tokens of that user for this
    project), so revoking the old token would also kill the new account when the same
    Google user reconnected. Caller commits."""
    conn.execute(
        "UPDATE calendar_accounts SET status='revoked', revoked_at=now(),"
        " refresh_token_ciphertext=NULL, key_id=NULL"
        " WHERE clinic_id=%s AND id=%s AND status <> 'revoked'",
        (clinic_id, account_id),
    )
    audit(conn, clinic_id, actor_id, "calendar.account_replaced", account_id)


ALREADY_CONNECTED = "The clinic already has an active calendar account"


def _lock_accounts(conn, clinic_id):
    lock_calendar_accounts(conn, clinic_id)


def _cancel_pending_revocations(conn, clinic_id, actor_id):
    """A new connection may be the same Google user as an account disconnected earlier whose
    Google revocation has not run yet. That revocation is grant-wide and would kill the new
    account, so before storing it (caller holds _lock_accounts) forget those tokens locally
    and cancel pending calendar.revoke_grant jobs (status done, last_error
    'revoke_superseded'). A job already processing is fenced by the same lock and the
    newer-account check in GoogleCalendarResolver.revoke_grant. Caller commits."""
    stale = [
        r["id"]
        for r in conn.execute(
            "SELECT a.id FROM calendar_accounts a WHERE a.clinic_id=%s AND a.status='revoked'"
            " AND (a.refresh_token_ciphertext IS NOT NULL OR EXISTS ("
            " SELECT 1 FROM outbox o WHERE o.clinic_id=a.clinic_id"
            " AND o.kind='calendar.revoke_grant' AND o.status IN ('pending','processing')"
            " AND o.payload->>'account_id' = a.id::text)) FOR UPDATE",
            (clinic_id,),
        ).fetchall()
    ]
    if not stale:
        return
    conn.execute(
        "UPDATE calendar_accounts SET refresh_token_ciphertext=NULL, key_id=NULL"
        " WHERE clinic_id=%s AND id = ANY(%s)",
        (clinic_id, stale),
    )
    conn.execute(
        "UPDATE outbox SET status='done', last_error='revoke_superseded', updated_at=now()"
        " WHERE clinic_id=%s AND kind='calendar.revoke_grant' AND status='pending'"
        " AND payload->>'account_id' = ANY(%s)",
        (clinic_id, [str(a) for a in stale]),
    )
    for account_id in stale:
        audit(conn, clinic_id, actor_id, "calendar.revoke_superseded", account_id)


def callback(conn, settings, http, state, code, error) -> UUID:
    """Complete an authorization and return the new account id. Raises HTTPException(400)
    for any invalid request, 409 if the clinic already has an active account and the start
    did not ask to replace it."""
    keyring = keyring_from(settings)
    if not state or len(state) > 256:
        raise HTTPException(400, INVALID_STATE)
    row = _consume_state(conn, state)
    if row is None:
        raise HTTPException(400, INVALID_STATE)
    if error or not code or len(code) > 2048:
        raise HTTPException(400, "Calendar authorization was not granted")
    clinic_id, user_id = row["clinic_id"], row["user_id"]
    if not row["replace_existing"] and active_accounts(conn, clinic_id):
        conn.commit()
        raise HTTPException(409, ALREADY_CONNECTED)  # Before any token is obtained.
    conn.commit()
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
    # Rejected grants are discarded, not revoked at Google: revocation is grant-wide and
    # would also invalidate an account this Google user already connected (here or in
    # another clinic). The owner can remove a stale grant in the Google account settings.
    if not isinstance(refresh_token, str) or not refresh_token:
        raise HTTPException(400, "Calendar authorization did not grant offline access")
    if not set(google.SCOPES) <= granted:
        raise HTTPException(400, "Calendar authorization did not grant the required access")
    account_id = uuid4()
    key_id, blob = keyring.encrypt(refresh_token.encode(), account_aad(clinic_id, account_id))
    _lock_accounts(conn, clinic_id)  # Serializes concurrent callbacks of one clinic.
    previous = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM calendar_accounts WHERE clinic_id=%s AND status='active' FOR UPDATE",
            (clinic_id,),
        ).fetchall()
    ]
    if previous and not row["replace_existing"]:
        conn.rollback()
        raise HTTPException(409, ALREADY_CONNECTED)  # Lost a race with another callback.
    _cancel_pending_revocations(conn, clinic_id, user_id)
    conn.execute(
        "INSERT INTO calendar_accounts (id,clinic_id,provider,refresh_token_ciphertext,key_id,"
        "scopes,status,connected_by) VALUES (%s,%s,'google',%s,%s,%s,'active',%s)",
        (account_id, clinic_id, blob, key_id, sorted(granted & set(google.SCOPES)), user_id),
    )
    audit(conn, clinic_id, user_id, "calendar.connected", account_id)
    for old in previous:
        _supersede(conn, clinic_id, user_id, old)
    conn.commit()
    return account_id


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

    (conn, clinic_id, doctor_id, account_id) -> (port, account_id) | None.

    With account_id (the account recorded as having written an event) that account is used
    while active. If it was revoked or replaced, resolution falls back to the doctor's
    current mapping (below). Only a recorded account in status 'error' (or one that no
    longer exists) stays strict: nothing resolves and the job dead-letters.

    Without account_id: the doctor's calendar_account_id, else the clinic's only active
    Google account (None if zero or several). Only status 'active' resolves here; a doctor
    mapped to a revoked or errored account resolves to None and never falls back to
    another account.

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
        if account_id is not None:
            row = conn.execute(ACCOUNT_SELECT + " AND id=%s", (clinic_id, account_id)).fetchone()
            if row is None or row["status"] != "revoked" or doctor_id is None:
                return row  # Active, or 'error' (stays strict: the caller dead-letters).
            # The recorded writer was revoked or replaced: fall back to the doctor's current
            # mapping. Cancels stay safe because a DELETE 404 only counts as success when
            # the calendar is reachable with the new account.
            self._evict(account_id)
        if doctor_id is None:
            return None
        doctor = conn.execute(
            "SELECT calendar_account_id FROM doctors WHERE clinic_id=%s AND id=%s",
            (clinic_id, doctor_id),
        ).fetchone()
        if doctor is None:
            return None
        if doctor["calendar_account_id"] is not None:
            return conn.execute(
                ACCOUNT_SELECT + " AND id=%s", (clinic_id, doctor["calendar_account_id"])
            ).fetchone()
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
        ProviderRetryable/ProviderPermanent on failure.

        Google revocation is grant-wide for the Google user. If an active account of this
        clinic was connected after this one was revoked, it may be the same user, so the
        call is skipped and the token only forgotten locally (audit
        'calendar.revoke_superseded'). The check runs under the clinic's account lock
        (_lock_accounts, also held by callbacks) and commits before the Google call: no
        transaction stays open during I/O. A reconnect that completes between that commit
        and Google's response can still lose its grant; that small window is accepted."""
        _lock_accounts(conn, clinic_id)
        account = conn.execute(
            "SELECT id, status, refresh_token_ciphertext, key_id, revoked_at"
            " FROM calendar_accounts WHERE clinic_id=%s AND provider='google' AND id=%s",
            (clinic_id, account_id),
        ).fetchone()
        if account is None or account["refresh_token_ciphertext"] is None:
            conn.commit()
            return True
        if account["status"] != "revoked":
            conn.commit()
            raise ProviderPermanent("account_not_revoked")
        newer = conn.execute(
            "SELECT 1 FROM calendar_accounts WHERE clinic_id=%s AND status='active'"
            " AND created_at > %s LIMIT 1",
            (clinic_id, account["revoked_at"]),
        ).fetchone()
        if newer:
            conn.execute(
                "UPDATE calendar_accounts SET refresh_token_ciphertext=NULL, key_id=NULL"
                " WHERE clinic_id=%s AND id=%s",
                (clinic_id, account_id),
            )
            audit(conn, clinic_id, None, "calendar.revoke_superseded", account_id)
            conn.commit()
            return True
        conn.commit()
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
