"""Bolna trusted gateway routes. Bolna calls these as custom function tools during a call.

Authentication is the tenant tool credential (`cvt_` token) that the compiled agent sends as
its tool `api_token` ("Bearer <token>", which Bolna documents as the Authorization header
value). The credential alone selects the clinic; call-context fields in the body never do.
Responses are private (no-store) and carry no patient data or caller number.

Line gating (T10, gateway.require_line), before anything is written: booking-changing tools
(reserve-slot, cancel-booking, reschedule-booking, opt-out) need provisioning state active,
or verified with the owner's test_calls_enabled; context and read-only tools (check-slots,
appointment-status, verify-booking) need verified or active. Otherwise 403 with a speakable
"not accepting bookings yet" sentence.
"""

import json

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response

from clinic_voice.bolna import bookings, gateway
from clinic_voice.config import get_settings
from clinic_voice.deps import database, tenant
from clinic_voice.schemas import (
    BolnaCallContext,
    BolnaCancel,
    BolnaChangeOut,
    BolnaContextOut,
    BolnaOptOut,
    BolnaOptOutOut,
    BolnaReschedule,
    BolnaReservation,
    BolnaReservationOut,
    BolnaSlotQuery,
    BolnaSlotsOut,
    BolnaStatusOut,
    BolnaStatusQuery,
    BolnaVerify,
    BolnaVerifyOut,
)

DUPLICATE_KEYS = "Duplicate JSON keys are not allowed"


class _DuplicateKey(Exception):
    pass


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


async def reject_duplicate_keys(request: Request):
    """Bolna renders tool bodies by substituting %(name)s values into a JSON template; its
    escaping is unverified. A value containing a quote could then add a second
    "execution_id" (or any other) key, which json.loads would silently resolve to the last
    one. Refuse any body with a repeated key at any depth. The message is fixed and echoes
    nothing. Malformed JSON is left to FastAPI's own 422."""
    body = await request.body()  # Starlette caches it for the body parameter.
    if not body:
        return
    try:
        json.loads(body, object_pairs_hook=_unique_pairs)
    except _DuplicateKey:
        raise HTTPException(422, DUPLICATE_KEYS) from None
    except ValueError:
        return


# Authentication runs first (an unauthenticated request gets 401 before any body check); the
# endpoints' own Depends(tenant) reuses this result (FastAPI caches it per request).
router = APIRouter(
    prefix="/v1/bolna",
    tags=["bolna"],
    dependencies=[Depends(tenant), Depends(reject_duplicate_keys)],
)
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
        gateway.require_line(conn, clinic_id, changes=False)
        if call is not None:
            gateway.upsert_session(conn, clinic_id, call, crypto)
        return gateway.clinic_context(conn, clinic_id)


@router.post("/tools/check-slots", response_model=BolnaSlotsOut)
def check_slots(query: BolnaSlotQuery, response: Response, clinic_id=Depends(tenant)):
    response.headers.update(NO_STORE)
    crypto = gateway.session_crypto(get_settings())
    with database() as conn:
        gateway.require_line(conn, clinic_id, changes=False)
        gateway.upsert_session(conn, clinic_id, query, crypto)
        return gateway.check_slots(conn, clinic_id, query)


@router.post("/tools/reserve-slot", response_model=BolnaReservationOut, status_code=202)
def reserve_slot(request: BolnaReservation, response: Response, clinic_id=Depends(tenant)):
    response.headers.update(NO_STORE)
    settings = get_settings()
    crypto = gateway.session_crypto(settings)
    with database() as conn:
        gateway.require_line(conn, clinic_id, changes=True)
        session_id = gateway.upsert_session(conn, clinic_id, request, crypto)
        gateway.refuse_reminder_call(conn, clinic_id, request)
        doctor_id, service_id = gateway.resolve(conn, clinic_id, request.doctor, request.service)
    # The lookup connection is closed before service.reserve opens its own.
    return gateway.reserve(
        settings.database_url.get_secret_value(),
        clinic_id,
        session_id,
        request,
        doctor_id,
        service_id,
        crypto,
        settings.patient_retention_days,
    )


@router.post("/tools/appointment-status", response_model=BolnaStatusOut)
def appointment_status(query: BolnaStatusQuery, response: Response, clinic_id=Depends(tenant)):
    response.headers.update(NO_STORE)
    crypto = gateway.session_crypto(get_settings())
    with database() as conn:
        gateway.require_line(conn, clinic_id, changes=False)
        gateway.upsert_session(conn, clinic_id, query, crypto)
        return gateway.appointment_status(conn, clinic_id, query)


@router.post("/tools/verify-booking", response_model=BolnaVerifyOut)
def verify_booking(query: BolnaVerify, response: Response, clinic_id=Depends(tenant)):
    response.headers.update(NO_STORE)
    crypto = gateway.session_crypto(get_settings())
    with database() as conn:
        gateway.require_line(conn, clinic_id, changes=False)
        session_id = gateway.upsert_session(conn, clinic_id, query, crypto)
        gateway.refuse_reminder_call(conn, clinic_id, query)
        return bookings.verify(conn, clinic_id, session_id, query, crypto[1])


@router.post("/tools/cancel-booking", response_model=BolnaChangeOut)
def cancel_booking(request: BolnaCancel, response: Response, clinic_id=Depends(tenant)):
    response.headers.update(NO_STORE)
    crypto = gateway.session_crypto(get_settings())
    with database() as conn:
        gateway.require_line(conn, clinic_id, changes=True)
        session_id = gateway.upsert_session(conn, clinic_id, request, crypto)
        gateway.refuse_reminder_call(conn, clinic_id, request)
        return bookings.cancel(conn, clinic_id, session_id, request)


@router.post("/tools/reschedule-booking", response_model=BolnaChangeOut)
def reschedule_booking(request: BolnaReschedule, response: Response, clinic_id=Depends(tenant)):
    response.headers.update(NO_STORE)
    settings = get_settings()
    crypto = gateway.session_crypto(settings)
    with database() as conn:
        gateway.require_line(conn, clinic_id, changes=True)
        session_id = gateway.upsert_session(conn, clinic_id, request, crypto)
        gateway.refuse_reminder_call(conn, clinic_id, request)
    return bookings.reschedule(
        settings.database_url.get_secret_value(),
        clinic_id,
        session_id,
        request,
        settings.patient_retention_days,
    )


@router.post("/tools/opt-out", response_model=BolnaOptOutOut)
def opt_out(request: BolnaOptOut, response: Response, clinic_id=Depends(tenant)):
    """Stop confirmations and reminders for the patient of a booking verified on this call
    (verify-booking first). Never applies to an unverified caller or by caller number."""
    response.headers.update(NO_STORE)
    crypto = gateway.session_crypto(get_settings())
    with database() as conn:
        gateway.require_line(conn, clinic_id, changes=True)
        session_id = gateway.upsert_session(conn, clinic_id, request, crypto)
        gateway.refuse_reminder_call(conn, clinic_id, request)
        return bookings.opt_out(conn, clinic_id, session_id, request)
