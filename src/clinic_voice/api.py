from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg import Error as DatabaseError

from clinic_voice.config import get_settings
from clinic_voice.credentials import authenticate, parse
from clinic_voice.db import connect
from clinic_voice.schemas import ReservationRequest, ReservationResponse, SlotQuery, SlotResponse
from clinic_voice.service import reserve, slots


@asynccontextmanager
async def lifespan(app):
    get_settings()  # Fail startup on missing configuration.
    yield


app = FastAPI(title="Clinic Voice AI", version="0.1.0", lifespan=lifespan)


@app.exception_handler(DatabaseError)
async def database_error(request, exc):
    return JSONResponse(
        status_code=503, content={"detail": "Scheduling unavailable; retry the same action later"}
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Pydantic errors can echo sensitive submitted values. Return locations/types only.
    return JSONResponse(
        status_code=422,
        content={
            "detail": [{"loc": list(error["loc"]), "type": error["type"]} for error in exc.errors()]
        },
    )


REQUIRED_MIGRATION = "002_tenant_credentials.sql"


def unauthorized():
    # One response for malformed, unknown, revoked and expired credentials.
    return HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": "Bearer"})


def tenant(authorization: Annotated[str | None, Header()] = None):
    scheme, _, token = (authorization or "").partition(" ")
    if scheme != "Bearer" or parse(token) is None:
        raise unauthorized()  # Reject malformed tokens before touching the database.
    # The credential record alone selects the tenant. Never accept clinic_id from the LLM.
    with connect(get_settings().database_url.get_secret_value()) as conn:
        clinic_id = authenticate(conn, token)
    if clinic_id is None:
        raise unauthorized()
    return clinic_id


@app.get("/health/live")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready():
    try:
        with connect(get_settings().database_url.get_secret_value()) as conn:
            applied = conn.execute("SELECT name FROM schema_migrations ORDER BY name").fetchall()
            if not any(row["name"] == REQUIRED_MIGRATION for row in applied):
                raise HTTPException(503, "Schema not ready")
    except DatabaseError:
        raise HTTPException(503, "Database unavailable") from None
    return {"status": "ready", "integrations": "not_configured"}


@app.post("/v1/tools/check-slots", response_model=SlotResponse)
def check_slots(query: SlotQuery, clinic_id=Depends(tenant)):
    with connect(get_settings().database_url.get_secret_value()) as conn:
        available = slots(conn, clinic_id, query.doctor_id, query.service_id, query.day)
    return {"slots": available[:3], "source": "local_reservations_only"}


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
