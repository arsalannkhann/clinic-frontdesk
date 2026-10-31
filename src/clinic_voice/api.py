import logging
import re
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg import Error as DatabaseError
from starlette.exceptions import HTTPException as StarletteHTTPException

from clinic_voice import staff as staff_service
from clinic_voice.bolna.gateway import KEY_PREFIX as BOLNA_KEY_PREFIX
from clinic_voice.bolna.gateway import refuse_context, require_line
from clinic_voice.chat import KEY_PREFIX as CHAT_KEY_PREFIX
from clinic_voice.config import get_settings
from clinic_voice.deps import database, require_role, staff_session, tool_caller
from clinic_voice.errors import NotFound, PublicError
from clinic_voice.public import KEY_PREFIX as WEB_KEY_PREFIX
from clinic_voice.routes_admin import router as admin_router
from clinic_voice.routes_appointments import router as appointments_router
from clinic_voice.routes_bolna import router as bolna_router
from clinic_voice.routes_calendar import admin_router as calendar_admin_router
from clinic_voice.routes_calendar import oauth_router
from clinic_voice.routes_chat import router as chat_router
from clinic_voice.routes_internal import router as internal_router
from clinic_voice.routes_public import router as public_router
from clinic_voice.routes_webhooks import router as webhooks_router
from clinic_voice.schemas import (
    CONTEXT_CODES,
    SLOT_SOURCE,
    AppointmentStatusRequest,
    AppointmentStatusResponse,
    LoginRequest,
    PasswordChange,
    ReservationRequest,
    ReservationResponse,
    SessionResponse,
    SlotQuery,
    SlotResponse,
    StaffUser,
    StaffUserCreate,
    StaffUserDetail,
    StaffUserUpdate,
)
from clinic_voice.service import appointment_status, reserve, slots

# Credentials that can appear in a request path: any token of the credential shape (tool
# cvt_, Bolna gateway cvb_, webhook cvw_, staff session cvs_) anywhere in it, and any segment after
# /webhooks/<provider>/. Neither pattern is anchored, so root_path prefixes, repeated
# slashes and unknown (404) paths are redacted too.
TOKEN_SHAPE = re.compile(r"cv[tbws]_[0-9a-fA-F]{32}\.[A-Za-z0-9_-]{43}")
WEBHOOK_SEGMENT = re.compile(r"(/webhooks/+[^/?#]+/+)[^/?#]+")
# A chat session id is a bearer secret in the path (/v1/chat/sessions/<id>/...).
CHAT_SEGMENT = re.compile(r"(/chat/+sessions/+)[^/?#]+")
REDACTED_SEGMENT = "<redacted>"


def redact_path(path: str) -> str:
    """The request path without its query string and with credential segments redacted."""
    path = path.split("?", 1)[0]
    path = TOKEN_SHAPE.sub(REDACTED_SEGMENT, path)
    path = CHAT_SEGMENT.sub(lambda m: m.group(1) + REDACTED_SEGMENT, path)
    return WEBHOOK_SEGMENT.sub(lambda m: m.group(1) + REDACTED_SEGMENT, path)


class StripQuery(logging.Filter):
    """Drop query strings from uvicorn access log lines (the OAuth callback's query holds an
    authorization code, and no query string here needs to be logged) and redact the webhook
    credential path segment."""

    def filter(self, record):
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], redact_path(args[2]), *args[3:])
        return True


def quiet_logs():
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, StripQuery) for f in access.filters):
        access.addFilter(StripQuery())
    for noisy in ("httpx", "httpcore"):  # Request lines carry calendar ids.
        logging.getLogger(noisy).setLevel(logging.WARNING)


quiet_logs()  # At import, so the filter applies however the app is served.


@asynccontextmanager
async def lifespan(app):
    get_settings()  # Fail startup on missing configuration.
    quiet_logs()  # Idempotent; repeated in case logging was reconfigured after import.
    yield


DASHBOARD_PATHS = ("/v1/staff/", "/v1/admin/")
CORS_METHODS = ["GET", "POST", "PATCH", "PUT", "DELETE"]
CORS_HEADERS = ["Authorization", "Content-Type"]
INTERNAL_ERROR = b'{"detail":"Internal server error"}'
log = logging.getLogger("clinic_voice.api")


def _is_dashboard_path(path: str) -> bool:
    return path.startswith(DASHBOARD_PATHS)


class DashboardMiddleware:
    """Response policy for the staff and admin paths (/v1/staff/*, /v1/admin/*).

    - Cache-Control: no-store on every response (session-scoped data; a route that sets its
      own Cache-Control keeps it). Matched on the path with any root_path prefix removed.
    - An unhandled exception becomes a generic 500 *inside* the CORS layer, so an allowed
      dashboard origin can still read it (Starlette's ServerErrorMiddleware sits outside all
      user middleware and would answer without CORS headers). The exception is re-raised
      afterwards so the server still logs it.
    - CORS for the origins in DASHBOARD_ORIGINS (none by default), without credentials
      (bearer tokens, never cookies). Matched on the raw request path only: behind a
      deployment path prefix (/api/v1/admin/...) no CORS headers are sent, failing closed.
      Settings are read per request; the Starlette middleware is rebuilt only when the
      origin list changes.
    """

    def __init__(self, app):
        self.app = app
        self._origins: tuple[str, ...] = ()
        self._cors = None

    async def _guarded(self, scope, receive, send):
        started = False

        async def send_wrapper(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = list(message.get("headers", []))
                if not any(name.lower() == b"cache-control" for name, _ in headers):
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            if started:
                raise
            log.error("unhandled error on a staff/admin route")  # No request data logged.
            await send_wrapper(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(INTERNAL_ERROR)).encode()),
                    ],
                }
            )
            await send_wrapper({"type": "http.response.body", "body": INTERNAL_ERROR})
            raise

    def _cors_for(self, origins: tuple[str, ...]):
        if origins != self._origins or self._cors is None:
            self._cors = CORSMiddleware(
                self._guarded,
                allow_origins=list(origins),
                allow_methods=CORS_METHODS,
                allow_headers=CORS_HEADERS,
                allow_credentials=False,
                max_age=600,
            )
            self._origins = origins
        return self._cors

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        raw = scope["path"]
        root = scope.get("root_path") or ""
        route = raw[len(root) :] if root and raw.startswith(root) else raw
        if not _is_dashboard_path(route):
            return await self.app(scope, receive, send)
        origins = tuple(get_settings().dashboard_origin_list()) if _is_dashboard_path(raw) else ()
        if origins:
            return await self._cors_for(origins)(scope, receive, send)
        return await self._guarded(scope, receive, send)


app = FastAPI(title="Clinic Voice AI", version="0.1.0", lifespan=lifespan)
app.add_middleware(DashboardMiddleware)

app.include_router(admin_router)
app.include_router(appointments_router)
app.include_router(calendar_admin_router)
app.include_router(oauth_router)
app.include_router(bolna_router)
app.include_router(webhooks_router)
app.include_router(public_router)
app.include_router(chat_router)
app.include_router(internal_router)

PUBLIC_PATHS = ("/v1/public/", "/v1/chat/")
PUBLIC_CODES = {
    400: "invalid_request",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "too_large",
    422: "invalid_request",
    429: "rate_limited",
    503: "unavailable",
}


def _is_public(request) -> bool:
    path = request.scope["path"]
    root = request.scope.get("root_path") or ""
    if root and path.startswith(root):
        path = path[len(root) :]
    return path.startswith(PUBLIC_PATHS)


def envelope(status: int, code: str, message: str, headers=None) -> JSONResponse:
    """The public API error body: {"error": {"code", "message"}}, never cached."""
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
        headers={**(headers or {}), "Cache-Control": "no-store"},
    )


@app.exception_handler(PublicError)
async def public_error(request, exc):
    return envelope(exc.status, exc.code, exc.message)


@app.exception_handler(StarletteHTTPException)
async def http_error(request, exc):
    """HTTP errors on public paths (including unknown routes under them) use the
    envelope with a code per status; every other path keeps FastAPI's {"detail"} body."""
    if not _is_public(request):
        return await http_exception_handler(request, exc)
    code = PUBLIC_CODES.get(exc.status_code, "error")
    message = exc.detail if isinstance(exc.detail, str) else code
    return envelope(exc.status_code, code, message, getattr(exc, "headers", None))


@app.exception_handler(DatabaseError)
async def database_error(request, exc):
    if _is_public(request):
        return envelope(503, "unavailable", "Booking is unavailable; please try again later")
    return JSONResponse(
        status_code=503, content={"detail": "Scheduling unavailable; retry the same action later"}
    )


@app.exception_handler(NotFound)
async def not_found(request, exc):
    return JSONResponse(status_code=404, content={"detail": "Not found"})


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    errors = exc.errors()
    for code in CONTEXT_CODES:  # Unsubstituted first: a template is the likelier root cause.
        unusable = [error for error in errors if error["type"] == code]
        if unusable:
            # A Bolna tool body with literal call-context templates, or without an
            # execution_id (schemas.BolnaCallContext). Only reachable after authentication
            # (routes_bolna.authenticated), which named the clinic. The tool is the last
            # path segment (context, check-slots, ...).
            fields = list(dict.fromkeys(name for e in unusable for name in e["ctx"]["fields"]))
            tool = request.url.path.rstrip("/").rsplit("/", 1)[-1]
            clinic_id = getattr(request.state, "bolna_clinic_id", None)
            return refuse_context(clinic_id, tool, code, fields)
    # Pydantic errors can echo sensitive submitted values. Return locations/types only.
    if _is_public(request):
        fields = ", ".join(
            dict.fromkeys(".".join(str(part) for part in e["loc"][1:]) or "body" for e in errors)
        )
        return envelope(422, "invalid_request", f"Invalid request: {fields}"[:300])
    return JSONResponse(
        status_code=422,
        content={
            "detail": [{"loc": list(error["loc"]), "type": error["type"]} for error in errors]
        },
    )


# Keys the Bolna gateway, the website, the chat confirm, staff bookings and the seed derive.
RESERVED_KEY_PREFIXES = (BOLNA_KEY_PREFIX, WEB_KEY_PREFIX, CHAT_KEY_PREFIX, "staff:", "seed:")
REQUIRED_MIGRATION = "019_chat.sql"


@app.get("/health/live")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready():
    try:
        with database() as conn:
            applied = conn.execute("SELECT name FROM schema_migrations ORDER BY name").fetchall()
            if not any(row["name"] == REQUIRED_MIGRATION for row in applied):
                raise HTTPException(503, "Schema not ready")
    except DatabaseError:
        raise HTTPException(503, "Database unavailable") from None
    return {"status": "ready", "integrations": "not_configured"}


# Generic tool API. Authenticated by a tools-scope credential only (a bolna-scope credential,
# which provisioning hands to the clinic's agent, gets 401). Line-gated like the Bolna gateway
# (T12): reads need provisioning verified or active; reserve needs active, or verified with
# test_calls_enabled, re-checked inside its write transaction.


@app.post("/v1/tools/check-slots", response_model=SlotResponse)
def check_slots(query: SlotQuery, caller=Depends(tool_caller)):
    with database() as conn:
        require_line(conn, caller.clinic_id, changes=False)
        available = slots(conn, caller.clinic_id, query.doctor_id, query.service_id, query.day)
    return {"slots": available[:3], "source": SLOT_SOURCE}


@app.post("/v1/tools/reserve-slot", response_model=ReservationResponse, status_code=202)
def reserve_slot(
    request: ReservationRequest,
    idempotency_key: Annotated[
        str, Header(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    ],
    caller=Depends(tool_caller),
):
    if idempotency_key.startswith(RESERVED_KEY_PREFIXES):
        # Reserved for keys the Bolna gateway and the public booking API derive (Bolna keys
        # bind appointments to a call session; web keys to a contact).
        raise HTTPException(422, "Idempotency key prefix is reserved")
    clinic_id = caller.clinic_id
    with database() as conn:
        require_line(conn, clinic_id, changes=True)  # Early refusal; re-checked under lock.
    return reserve(
        get_settings().database_url.get_secret_value(),
        clinic_id,
        request,
        idempotency_key,
        gate=lambda conn: require_line(conn, clinic_id, True, lock=True),
        tool_credential_id=caller.credential_id,
    )


@app.post("/v1/tools/appointment-status", response_model=AppointmentStatusResponse)
def get_appointment_status(request: AppointmentStatusRequest, caller=Depends(tool_caller)):
    """Only appointments this same credential reserved through /v1/tools/reserve-slot (ADR
    0005): never Bolna-gateway, staff or other-credential bookings, whatever the UUID."""
    with database() as conn:
        require_line(conn, caller.clinic_id, changes=False)
        return appointment_status(
            conn, caller.clinic_id, request.appointment_id, caller.credential_id
        )


@app.post("/v1/staff/login", response_model=SessionResponse)
def staff_login(request: LoginRequest):
    with database() as conn:
        session = staff_service.login(conn, request.email, request.password)
    if session is None:
        raise HTTPException(401, "Invalid email or password")
    return session


@app.post("/v1/staff/logout", status_code=204)
def staff_logout(staff=Depends(staff_session)):
    with database() as conn:
        staff_service.logout(conn, staff)
    return Response(status_code=204)


@app.get("/v1/staff/me", response_model=StaffUser)
def staff_me(staff=Depends(staff_session)):
    return staff_service.public(staff)


@app.post("/v1/staff/me/password", status_code=204)
def staff_change_password(request: PasswordChange, staff=Depends(staff_session)):
    with database() as conn:
        staff_service.change_password(conn, staff, request.current_password, request.new_password)
    return Response(status_code=204)


@app.get("/v1/staff/users", response_model=list[StaffUserDetail])
def staff_list_users(staff=Depends(require_role("owner", "admin"))):
    with database() as conn:
        return staff_service.list_users(conn, staff["clinic_id"])


@app.post("/v1/staff/users", response_model=StaffUser, status_code=201)
def staff_create_user(request: StaffUserCreate, staff=Depends(require_role("owner"))):
    with database() as conn:
        return staff_service.create_user(
            conn,
            staff["clinic_id"],
            request.email,
            request.role,
            request.temporary_password,
            actor_id=staff["id"],
        )


@app.patch("/v1/staff/users/{user_id}", response_model=StaffUserDetail)
def staff_update_user(
    user_id: UUID, request: StaffUserUpdate, staff=Depends(require_role("owner"))
):
    with database() as conn:
        return staff_service.update_user(conn, staff, user_id, request.role, request.disabled)
