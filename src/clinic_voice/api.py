import logging
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg import Error as DatabaseError

from clinic_voice import staff as staff_service
from clinic_voice.config import get_settings
from clinic_voice.deps import database, require_role, staff_session, tenant
from clinic_voice.errors import NotFound
from clinic_voice.routes_admin import router as admin_router
from clinic_voice.routes_calendar import admin_router as calendar_admin_router
from clinic_voice.routes_calendar import oauth_router
from clinic_voice.schemas import (
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


class StripQuery(logging.Filter):
    """Drop query strings from uvicorn access log lines: the OAuth callback's query holds an
    authorization code, and no query string here needs to be logged."""

    def filter(self, record):
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], args[2].split("?", 1)[0], *args[3:])
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


app = FastAPI(title="Clinic Voice AI", version="0.1.0", lifespan=lifespan)

app.include_router(admin_router)
app.include_router(calendar_admin_router)
app.include_router(oauth_router)


@app.exception_handler(DatabaseError)
async def database_error(request, exc):
    return JSONResponse(
        status_code=503, content={"detail": "Scheduling unavailable; retry the same action later"}
    )


@app.exception_handler(NotFound)
async def not_found(request, exc):
    return JSONResponse(status_code=404, content={"detail": "Not found"})


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Pydantic errors can echo sensitive submitted values. Return locations/types only.
    return JSONResponse(
        status_code=422,
        content={
            "detail": [{"loc": list(error["loc"]), "type": error["type"]} for error in exc.errors()]
        },
    )


REQUIRED_MIGRATION = "010_appointment_calendar_account.sql"


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


@app.post("/v1/tools/check-slots", response_model=SlotResponse)
def check_slots(query: SlotQuery, clinic_id=Depends(tenant)):
    with database() as conn:
        available = slots(conn, clinic_id, query.doctor_id, query.service_id, query.day)
    return {"slots": available[:3], "source": SLOT_SOURCE}


@app.post("/v1/tools/reserve-slot", response_model=ReservationResponse, status_code=202)
def reserve_slot(
    request: ReservationRequest,
    idempotency_key: Annotated[
        str, Header(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    ],
    clinic_id=Depends(tenant),
):
    return reserve(
        get_settings().database_url.get_secret_value(), clinic_id, request, idempotency_key
    )


@app.post("/v1/tools/appointment-status", response_model=AppointmentStatusResponse)
def get_appointment_status(request: AppointmentStatusRequest, clinic_id=Depends(tenant)):
    with database() as conn:
        return appointment_status(conn, clinic_id, request.appointment_id)


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
