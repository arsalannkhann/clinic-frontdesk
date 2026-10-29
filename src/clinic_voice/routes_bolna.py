"""Bolna trusted gateway routes. Bolna calls these as custom function tools during a call.

Authentication is the tenant tool credential (`cvt_` token) that the compiled agent sends as
its tool `api_token` ("Bearer <token>", which Bolna documents as the Authorization header
value). The credential alone selects the clinic; call-context fields in the body never do.
Responses are private (no-store) and carry no patient data or caller number.
"""

from fastapi import APIRouter, Body, Depends, Response

from clinic_voice.bolna import gateway
from clinic_voice.config import get_settings
from clinic_voice.deps import database, tenant
from clinic_voice.schemas import (
    BolnaCallContext,
    BolnaContextOut,
    BolnaReservation,
    BolnaReservationOut,
    BolnaSlotQuery,
    BolnaSlotsOut,
    BolnaStatusOut,
    BolnaStatusQuery,
)

router = APIRouter(prefix="/v1/bolna", tags=["bolna"])
NO_STORE = {"Cache-Control": "no-store"}


@router.post("/context", response_model=BolnaContextOut)
def context(
    response: Response,
    call: BolnaCallContext | None = Body(default=None),
    clinic_id=Depends(tenant),
):
    """Pre-call variables: clinic configuration only. With call context in the body (when
    used as a tool), the call session is recorded as well."""
    response.headers.update(NO_STORE)
    crypto = gateway.session_crypto(get_settings()) if call is not None else None
    with database() as conn:
        if call is not None:
            gateway.upsert_session(conn, clinic_id, call, crypto)
        return gateway.clinic_context(conn, clinic_id)


@router.post("/tools/check-slots", response_model=BolnaSlotsOut)
def check_slots(query: BolnaSlotQuery, response: Response, clinic_id=Depends(tenant)):
    response.headers.update(NO_STORE)
    crypto = gateway.session_crypto(get_settings())
    with database() as conn:
        gateway.upsert_session(conn, clinic_id, query, crypto)
        return gateway.check_slots(conn, clinic_id, query)


@router.post("/tools/reserve-slot", response_model=BolnaReservationOut, status_code=202)
def reserve_slot(request: BolnaReservation, response: Response, clinic_id=Depends(tenant)):
    response.headers.update(NO_STORE)
    settings = get_settings()
    crypto = gateway.session_crypto(settings)
    with database() as conn:
        session_id = gateway.upsert_session(conn, clinic_id, request, crypto)
        return gateway.reserve(
            settings.database_url.get_secret_value(), conn, clinic_id, session_id, request
        )


@router.post("/tools/appointment-status", response_model=BolnaStatusOut)
def appointment_status(query: BolnaStatusQuery, response: Response, clinic_id=Depends(tenant)):
    response.headers.update(NO_STORE)
    crypto = gateway.session_crypto(get_settings())
    with database() as conn:
        gateway.upsert_session(conn, clinic_id, query, crypto)
        return gateway.appointment_status(conn, clinic_id, query)
