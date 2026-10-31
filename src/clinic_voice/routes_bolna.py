"""Bolna trusted gateway routes. Bolna calls these as custom function tools during a call.

Authentication is the tenant gateway credential that the compiled agent sends as its tool
`api_token` ("Bearer <token>", which Bolna documents as the Authorization header value): a
bolna-scope `cvb_` token (issued by provisioning, `credential issue --scope bolna`), or a
tools-scope `cvt_` token for a clinic configured by hand before the bolna scope existed. The
credential alone selects the clinic; call-context fields in the body never do. The generic
/v1/tools/* API refuses bolna-scope tokens (deps.tool_caller).
Responses are private (no-store) and carry no patient data or caller number.

Unusable call context: a body whose execution_id, from_number or agent_id still holds its
template ("{from_number}", "%(agent_id)s", "<agent_id>", a bare variable name) fails
validation (schemas.BolnaCallContext) and gets 422 code context_unsubstituted with a
speakable handoff sentence and the field names, before the line gate and before any call
session; an absent, null or blank execution_id gets the same 422 with code context_missing.
An empty or malformed from_number/agent_id is accepted and ignored as before, with one log
line naming the fields (gateway.note_context).

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


def authenticated(request: Request, clinic_id=Depends(tenant)):
    """Router-wide authentication. Records the clinic on the request so the validation
    handler can name it when it refuses an unsubstituted call context (api.py)."""
    request.state.bolna_clinic_id = clinic_id


# Ordering, as FastAPI actually runs it: the request body is read and JSON-decoded first, so
# a body that is not valid JSON gets FastAPI's generic 422 (type json_invalid) before any
# dependency, even without a credential. That 422 names locations and types only (api.py) and
# reveals nothing about the clinic. For a body that parses, the dependencies are solved next
# (authentication, then the duplicate-key check) and only then is the body validated against
# the route's model, so an unauthenticated request gets 401 before the call-context 422s
# (context_unsubstituted, context_missing) and every other model check. The endpoints' own
# Depends(tenant) reuses this result (FastAPI caches it per request).
router = APIRouter(
    prefix="/v1/bolna",
    tags=["bolna"],
    dependencies=[Depends(authenticated), Depends(reject_duplicate_keys)],
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
            gateway.upsert_session(conn, clinic_id, call, crypto, tool="context")
        return gateway.clinic_context(conn, clinic_id)


@router.post("/tools/check-slots", response_model=BolnaSlotsOut)
def check_slots(query: BolnaSlotQuery, response: Response, clinic_id=Depends(tenant)):
    response.headers.update(NO_STORE)
    crypto = gateway.session_crypto(get_settings())
    with database() as conn:
        gateway.require_line(conn, clinic_id, changes=False)
        gateway.upsert_session(conn, clinic_id, query, crypto, tool="check-slots")
        return gateway.check_slots(conn, clinic_id, query)


@router.post("/tools/reserve-slot", response_model=BolnaReservationOut, status_code=202)
def reserve_slot(request: BolnaReservation, response: Response, clinic_id=Depends(tenant)):
    response.headers.update(NO_STORE)
    settings = get_settings()
    crypto = gateway.session_crypto(settings)
    with database() as conn:
        gateway.require_line(conn, clinic_id, changes=True)
        session_id = gateway.upsert_session(conn, clinic_id, request, crypto, tool="reserve-slot")
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
        gateway.upsert_session(conn, clinic_id, query, crypto, tool="appointment-status")
        return gateway.appointment_status(conn, clinic_id, query)


@router.post("/tools/verify-booking", response_model=BolnaVerifyOut)
def verify_booking(query: BolnaVerify, response: Response, clinic_id=Depends(tenant)):
    response.headers.update(NO_STORE)
    crypto = gateway.session_crypto(get_settings())
    with database() as conn:
        gateway.require_line(conn, clinic_id, changes=False)
        session_id = gateway.upsert_session(conn, clinic_id, query, crypto, tool="verify-booking")
        gateway.refuse_reminder_call(conn, clinic_id, query)
        return bookings.verify(conn, clinic_id, session_id, query, crypto[1])


@router.post("/tools/cancel-booking", response_model=BolnaChangeOut)
def cancel_booking(request: BolnaCancel, response: Response, clinic_id=Depends(tenant)):
    response.headers.update(NO_STORE)
    crypto = gateway.session_crypto(get_settings())
    with database() as conn:
        gateway.require_line(conn, clinic_id, changes=True)
        session_id = gateway.upsert_session(conn, clinic_id, request, crypto, tool="cancel-booking")
        gateway.refuse_reminder_call(conn, clinic_id, request)
        return bookings.cancel(conn, clinic_id, session_id, request)


@router.post("/tools/reschedule-booking", response_model=BolnaChangeOut)
def reschedule_booking(request: BolnaReschedule, response: Response, clinic_id=Depends(tenant)):
    response.headers.update(NO_STORE)
    settings = get_settings()
    crypto = gateway.session_crypto(settings)
    with database() as conn:
        gateway.require_line(conn, clinic_id, changes=True)
        session_id = gateway.upsert_session(
            conn, clinic_id, request, crypto, tool="reschedule-booking"
        )
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
        session_id = gateway.upsert_session(conn, clinic_id, request, crypto, tool="opt-out")
        gateway.refuse_reminder_call(conn, clinic_id, request)
        return bookings.opt_out(conn, clinic_id, session_id, request)
