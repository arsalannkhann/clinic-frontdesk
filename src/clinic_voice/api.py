import secrets
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg import Error as DatabaseError

from clinic_voice.config import get_settings
from clinic_voice.db import connect
from clinic_voice.schemas import ReservationRequest, ReservationResponse, SlotQuery, SlotResponse
from clinic_voice.service import reserve, slots


@asynccontextmanager
async def lifespan(app):
    get_settings()  # Fail startup on missing/placeholder credentials.
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


def tenant(authorization: Annotated[str | None, Header()] = None):
    settings = get_settings()
    expected = "Bearer " + settings.tool_token.get_secret_value()
    if not authorization or not secrets.compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(401, "Invalid credentials")
    # Bootstrap: one credential maps to exactly one tenant. Never accept clinic_id from LLM.
    return settings.clinic_id


@app.get("/health/live")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready():
    try:
        with connect(get_settings().database_url.get_secret_value()) as conn:
            applied = conn.execute("SELECT name FROM schema_migrations ORDER BY name").fetchall()
            if not any(row["name"] == "001_initial.sql" for row in applied):
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
