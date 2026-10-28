"""Google Calendar connection routes.

The callback is reached by a browser redirect from Google, so it carries no bearer token; it
is bound instead to a single-use, ten-minute state created by an authenticated owner/admin.
Its query string holds the authorization code: api.py strips query strings from access logs
and responses are no-store with no referrer.
"""

from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import HTMLResponse

from clinic_voice import calendar_accounts
from clinic_voice.config import get_settings
from clinic_voice.deps import database, require_role
from clinic_voice.providers.google_calendar import DEFAULT_TIMEOUT
from clinic_voice.schemas import AuthorizationStart, CalendarAccountOut

admin_router = APIRouter(prefix="/v1/admin/calendar", tags=["calendar"])
oauth_router = APIRouter(prefix="/v1/oauth", tags=["calendar"])
reader = require_role("owner", "admin", "viewer")
writer = require_role("owner", "admin")

PRIVATE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
CONNECTED_PAGE = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Calendar connected</title></head>"
    "<body><p>Calendar connected; you can close this window.</p></body></html>"
)


def google_http():
    """Outbound HTTP client for Google (overridden in tests with an httpx.MockTransport)."""
    with httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=False) as client:
        yield client


@admin_router.post("/google/start", response_model=AuthorizationStart)
def google_start(response: Response, staff=Depends(writer)):
    settings = get_settings()
    calendar_accounts.keyring_from(settings)  # 503 before touching the database.
    response.headers.update(PRIVATE_HEADERS)
    with database() as conn:
        return {"authorization_url": calendar_accounts.start(conn, staff, settings)}


@admin_router.get("/accounts", response_model=list[CalendarAccountOut])
def list_accounts(staff=Depends(reader)):
    with database() as conn:
        return calendar_accounts.list_accounts(conn, staff["clinic_id"])


@admin_router.delete("/google/{account_id}", status_code=204)
def revoke_account(account_id: UUID, staff=Depends(writer), http=Depends(google_http)):
    with database() as conn:
        calendar_accounts.revoke(conn, staff, get_settings(), http, account_id)
    return Response(status_code=204)


@oauth_router.get("/google/callback", response_class=HTMLResponse)
def google_callback(
    state: Annotated[str | None, Query(max_length=256)] = None,
    code: Annotated[str | None, Query(max_length=2048)] = None,
    error: Annotated[str | None, Query(max_length=256)] = None,
    http=Depends(google_http),
):
    settings = get_settings()
    with database() as conn:
        calendar_accounts.callback(conn, settings, http, state, code, error)
    return HTMLResponse(CONNECTED_PAGE, headers=PRIVATE_HEADERS)
