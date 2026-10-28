"""Google Calendar adapter (CalendarPort) and Google OAuth token endpoint helpers.

All HTTP goes through an injected httpx.Client, so tests use httpx.MockTransport and never
touch the network. Exceptions carry fixed short codes only: no response bodies, tokens,
calendar ids or URLs (the request URL contains the calendar id, so httpx errors are never
chained). Endpoints and semantics follow the official documentation recorded in
docs/SOURCE_REVIEW.md.

Error mapping (see the CalendarPort obligations in providers/calendar.py):
- 401: the cached access token is dropped, refreshed once and the request retried once.
- 429, 5xx and 403 with reason rateLimitExceeded/userRateLimitExceeded: ProviderRetryable,
  with Retry-After (seconds or HTTP date) when present.
- other 4xx: ProviderPermanent. insert 409 is EventAlreadyExists (Google returns it for an
  id already used, including by a deleted event).
- get: 404 -> None; 410 -> a cancelled event. A deleted event fetched with 200 is mapped to
  status 'cancelled'; Google only guarantees its id, so missing times are filled with a fixed
  placeholder (1970-01-01T00:00Z plus one minute) that callers must not interpret.
  'tentative' (only possible after a human edit) maps to 'confirmed': it still blocks time.
- delete: 410 is success; 404 is success only after an events list (maxResults=1) on the
  same calendar returns 200, i.e. the calendar is reachable and the event is really gone.
  An unreachable calendar is ProviderPermanent('calendar_not_found').
- freeBusy: a per-calendar `errors` entry is ProviderPermanent for reason notFound and
  ProviderRetryable otherwise; a requested calendar missing from the response is retryable.
- Timeouts: for writes (insert, delete) a timeout after the request may have been sent is
  ProviderTimeout (outcome unknown; callers recover by fetching the deterministic id). For
  reads (get, freeBusy, token refresh) and for connection failures before anything was sent,
  ProviderRetryable('timeout'/'network'), since nothing can have changed.
"""

import logging
import re
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from threading import Lock
from urllib.parse import quote

import httpx

from clinic_voice.providers.calendar import (
    CalendarEvent,
    EventAlreadyExists,
    ProviderError,
    ProviderPermanent,
    ProviderRetryable,
    ProviderTimeout,
    merge_intervals,
)

log = logging.getLogger("clinic_voice.google")

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
API_BASE = "https://www.googleapis.com/calendar/v3"
# Minimum scopes: events insert/get/delete on the booking calendar (calendar.events) and
# freeBusy.query on busy calendars (calendar.freebusy). See docs/SOURCE_REVIEW.md.
SCOPES = (
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
)
DEFAULT_TIMEOUT = 10.0
TOKEN_EXPIRY_MARGIN = 60.0
MAX_RETRY_AFTER = 3600.0
RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
PLACEHOLDER_START = datetime(1970, 1, 1, tzinfo=UTC)


class OAuthError(Exception):
    """Token endpoint failure. `code` is a fixed short code; `retryable` marks 429/5xx and
    network failures."""

    def __init__(self, code: str, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    value = value.strip()
    # str.isdigit() accepts characters such as superscripts that float() rejects; ASCII only.
    if re.fullmatch(r"[0-9]{1,10}", value):
        seconds = float(value)
    else:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
    return min(max(seconds, 0.0), MAX_RETRY_AFTER)


def _reason(response: httpx.Response) -> str | None:
    """First error reason from a Google JSON error body (used for branching only)."""
    try:
        errors = response.json()["error"]["errors"]
        reason = errors[0]["reason"]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    return reason if isinstance(reason, str) else None


def _error_for(response: httpx.Response) -> ProviderError:
    status = response.status_code
    if status == 429 or status >= 500:
        return ProviderRetryable(f"http_{status}", retry_after(response))
    if status == 403 and _reason(response) in RATE_LIMIT_REASONS:
        return ProviderRetryable("rate_limited", retry_after(response))
    return ProviderPermanent(f"http_{status}")


def _token_post(http: httpx.Client, data: dict, timeout: float) -> dict:
    try:
        response = http.post(TOKEN_ENDPOINT, data=data, timeout=timeout)
    except httpx.TimeoutException:
        raise OAuthError("token_timeout", retryable=True) from None
    except httpx.HTTPError:
        raise OAuthError("token_network", retryable=True) from None
    if response.status_code == 429 or response.status_code >= 500:
        raise OAuthError(f"token_http_{response.status_code}", retryable=True)
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code != 200:
        error = body.get("error") if isinstance(body, dict) else None
        if error == "invalid_grant":
            raise OAuthError("invalid_grant")
        raise OAuthError(f"token_http_{response.status_code}")
    if not isinstance(body, dict) or not isinstance(body.get("access_token"), str):
        raise OAuthError("token_malformed")
    return body


def exchange_code(http, client_id, client_secret, code, code_verifier, redirect_uri, timeout=10.0):
    """Authorization code grant with PKCE. Returns the token response dict (caller extracts
    refresh_token and scope; never log it)."""
    return _token_post(
        http,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout,
    )


def revoke_grant(http, token, timeout=10.0) -> None:
    """Durable-path revocation: returns once Google confirms the token is revoked (200) or
    no longer valid (400 invalid_token, e.g. already revoked or expired). Raises OAuthError
    (retryable for network, 429 and 5xx) otherwise. No bodies are logged or raised."""
    try:
        response = http.post(REVOKE_ENDPOINT, data={"token": token}, timeout=timeout)
    except httpx.HTTPError:
        raise OAuthError("revoke_network", retryable=True) from None
    if response.status_code == 200:
        return
    if response.status_code == 429 or response.status_code >= 500:
        raise OAuthError(f"revoke_http_{response.status_code}", retryable=True)
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        error = None
    if response.status_code == 400 and error == "invalid_token":
        return
    raise OAuthError(f"revoke_http_{response.status_code}")


def revoke_token(http, token, timeout=10.0) -> bool:
    """Best-effort revocation of a refresh (or access) token. Never raises; logs no bodies."""
    try:
        response = http.post(REVOKE_ENDPOINT, data={"token": token}, timeout=timeout)
    except httpx.HTTPError as error:
        log.warning("google revoke failed error=%s", type(error).__name__)
        return False
    if response.status_code != 200:
        log.warning("google revoke failed status=%s", response.status_code)
        return False
    return True


def _parse_time(value) -> datetime:
    if not isinstance(value, str):
        raise ProviderPermanent("bad_event")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ProviderPermanent("bad_event") from None
    if parsed.tzinfo is None:
        raise ProviderPermanent("bad_event")
    return parsed


def _event_from(body, event_id) -> CalendarEvent:
    if not isinstance(body, dict):
        raise ProviderPermanent("bad_event")
    status = "cancelled" if body.get("status") == "cancelled" else "confirmed"
    start = (body.get("start") or {}).get("dateTime")
    end = (body.get("end") or {}).get("dateTime")
    if status == "cancelled" and (start is None or end is None):
        return CalendarEvent(
            event_id,
            PLACEHOLDER_START,
            PLACEHOLDER_START + timedelta(minutes=1),
            "",
            status="cancelled",
        )
    start, end = _parse_time(start), _parse_time(end)
    if start >= end:
        raise ProviderPermanent("bad_event")
    return CalendarEvent(
        event_id,
        start,
        end,
        str(body.get("summary") or ""),
        str(body.get("description") or ""),
        status=status,
    )


class GoogleCalendar:
    """CalendarPort over the Google Calendar API v3 using one account's refresh token.

    The access token is cached in memory until TOKEN_EXPIRY_MARGIN seconds before expiry.
    `timeout_seconds` bounds every HTTP call (outbox.check_ports compares it to the lease).
    Thread-safe for token caching; the httpx.Client is shared and thread-safe.
    """

    def __init__(
        self,
        refresh_token: str,
        client_id: str,
        client_secret: str,
        http: httpx.Client,
        timeout_seconds: float = DEFAULT_TIMEOUT,
        clock=time.monotonic,
    ):
        self._refresh_token = refresh_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http
        self.timeout_seconds = timeout_seconds
        self._clock = clock
        self._lock = Lock()
        self._token: str | None = None
        self._expires = 0.0

    def __repr__(self):
        return "GoogleCalendar()"

    # Tokens

    def _access_token(self) -> str:
        with self._lock:
            if self._token and self._clock() < self._expires - TOKEN_EXPIRY_MARGIN:
                return self._token
            try:
                body = _token_post(
                    self._http,
                    {
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "grant_type": "refresh_token",
                        "refresh_token": self._refresh_token,
                    },
                    self.timeout_seconds,
                )
            except OAuthError as error:
                if error.retryable:
                    raise ProviderRetryable("auth_unavailable") from None
                if error.code == "invalid_grant":
                    raise ProviderPermanent("auth_revoked") from None
                raise ProviderPermanent("auth_failed") from None
            expires_in = body.get("expires_in")
            if isinstance(expires_in, bool) or not isinstance(expires_in, int | float):
                expires_in = 0  # Unknown lifetime: use once, do not cache.
            self._token = body["access_token"]
            self._expires = self._clock() + float(expires_in)
            return self._token

    def _drop_token(self, token):
        with self._lock:
            if self._token == token:
                self._token = None

    def _send(self, method, url, *, write, json=None, params=None) -> httpx.Response:
        for attempt in (1, 2):
            token = self._access_token()
            try:
                response = self._http.request(
                    method,
                    url,
                    json=json,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=self.timeout_seconds,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                raise ProviderRetryable("network") from None  # Nothing was sent.
            except httpx.TimeoutException:
                if write:
                    raise ProviderTimeout() from None
                raise ProviderRetryable("timeout") from None
            except httpx.HTTPError:
                if write:
                    raise ProviderTimeout("transport") from None
                raise ProviderRetryable("network") from None
            if response.status_code == 401 and attempt == 1:
                # A 401 means the request was rejected before acting: retrying is safe.
                self._drop_token(token)
                continue
            if response.status_code == 401:
                raise ProviderPermanent("unauthorized")
            return response
        raise AssertionError("unreachable")

    @staticmethod
    def _events_url(calendar_id, event_id=None):
        url = f"{API_BASE}/calendars/{quote(calendar_id, safe='')}/events"
        if event_id is not None:
            url += "/" + quote(event_id, safe="")
        return url

    # CalendarPort

    def get_event(self, calendar_id, event_id):
        response = self._send("GET", self._events_url(calendar_id, event_id), write=False)
        if response.status_code == 404:
            return None
        if response.status_code == 410:
            return _event_from({"status": "cancelled"}, event_id)
        if response.status_code != 200:
            raise _error_for(response)
        try:
            body = response.json()
        except ValueError:
            raise ProviderRetryable("malformed") from None
        return _event_from(body, event_id)

    def insert_event(self, calendar_id, event):
        body = {
            "id": event.event_id,
            "start": {"dateTime": event.start.isoformat()},
            "end": {"dateTime": event.end.isoformat()},
            "summary": event.summary,
            "description": event.description,
            "transparency": "opaque",
            "extendedProperties": {"private": {"appointment_ref": event.event_id}},
        }
        response = self._send(
            "POST",
            self._events_url(calendar_id),
            write=True,
            json=body,
            params={"sendUpdates": "none"},
        )
        if response.status_code == 409:
            raise EventAlreadyExists()
        if response.status_code not in (200, 201):
            raise _error_for(response)
        try:
            return _event_from(response.json(), event.event_id)
        except (ValueError, ProviderError):
            # Written but unreadable: outcome as good as unknown; recover by fetching the id.
            raise ProviderTimeout("malformed") from None

    def cancel_event(self, calendar_id, event_id):
        response = self._send(
            "DELETE",
            self._events_url(calendar_id, event_id),
            write=True,
            params={"sendUpdates": "none"},
        )
        if response.status_code in (200, 204, 410):
            return
        if response.status_code == 404:
            # 404 means "no such event" only if the calendar itself is reachable; otherwise
            # the event may still exist in a calendar these credentials can no longer see.
            check = self._send(
                "GET", self._events_url(calendar_id), write=False, params={"maxResults": 1}
            )
            if check.status_code == 200:
                return
            if check.status_code == 404:
                raise ProviderPermanent("calendar_not_found")
            raise _error_for(check)
        raise _error_for(response)

    def free_busy(self, calendar_ids, start, end):
        if not calendar_ids:
            return []
        response = self._send(
            "POST",
            f"{API_BASE}/freeBusy",
            write=False,
            json={
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "items": [{"id": c} for c in calendar_ids],
            },
        )
        if response.status_code != 200:
            raise _error_for(response)
        try:
            calendars = response.json()["calendars"]
            if not isinstance(calendars, dict):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            raise ProviderRetryable("malformed") from None
        intervals = []
        for calendar_id in calendar_ids:
            entry = calendars.get(calendar_id)
            if not isinstance(entry, dict):
                raise ProviderRetryable("freebusy_missing")
            errors = entry.get("errors") or []
            if errors:
                reasons = {e.get("reason") for e in errors if isinstance(e, dict)}
                if "notFound" in reasons:
                    raise ProviderPermanent("calendar_not_found")
                raise ProviderRetryable("freebusy_error")
            for item in entry.get("busy") or []:
                if not isinstance(item, dict):
                    raise ProviderRetryable("malformed")
                try:
                    s, e = _parse_time(item.get("start")), _parse_time(item.get("end"))
                except ProviderPermanent:
                    raise ProviderRetryable("malformed") from None
                intervals.append((s, e))
        return merge_intervals(intervals, start, end)
