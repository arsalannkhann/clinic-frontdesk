"""Staff platform routes (P2): /v1/staff/calendar, bookings, callbacks, outbound-status,
conversations, stats. The clinic comes from the staff session only. Viewers read; owners
and admins write. Authentication, role and 404 errors keep FastAPI's {"detail"} body;
refusals with a machine code (409 outbound_disabled, ...) use the
{"error": {"code", "message"}} envelope."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from pydantic import AwareDatetime

from clinic_voice import callbacks, staff_platform
from clinic_voice.config import get_settings
from clinic_voice.deps import database, require_role
from clinic_voice.routes_appointments import _keyring
from clinic_voice.schemas import (
    CallbackStatus,
    PublicBookingOut,
    PublicErrorEnvelope,
    StaffBookingRequest,
    StaffCalendarOut,
    StaffCallbackCallOut,
    StaffCallbackOut,
    StaffCallbackReopened,
    StaffCallbackResolve,
    StaffCallbackResolved,
    StaffConversationDetail,
    StaffConversationOut,
    StaffOutboundStatus,
    StaffStatsOut,
)
from clinic_voice.tick import ports_for

router = APIRouter(prefix="/v1/staff", tags=["staff"])
reader = require_role("owner", "admin", "viewer")
writer = require_role("owner", "admin")
CODED = {status: {"model": PublicErrorEnvelope} for status in (409, 502, 503)}
IdempotencyKey = Annotated[
    str | None, Header(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
]


def _url():
    return get_settings().database_url.get_secret_value()


def bolna_port(settings):
    """The Bolna port for staff callback calls, or None without BOLNA_API_KEY."""
    return ports_for(settings).bolna


@router.get("/calendar", response_model=StaffCalendarOut)
def get_calendar(
    starts: Annotated[AwareDatetime, Query(alias="from")],
    ends: Annotated[AwareDatetime, Query(alias="to")],
    include_cancelled: bool = False,
    staff=Depends(reader),
):
    """Doctors, appointments and schedule exceptions in [from, to) (at most 31 days)."""
    with database() as conn:
        return staff_platform.calendar(conn, staff, _keyring(), starts, ends, include_cancelled)


@router.post("/bookings", response_model=PublicBookingOut, status_code=201, responses=CODED)
def create_booking(
    body: StaffBookingRequest, idempotency_key: IdempotencyKey = None, staff=Depends(writer)
):
    """Book for a patient (channel staff); same response as a web booking."""
    settings = get_settings()
    return staff_platform.book(
        _url(), settings, staff, body, idempotency_key, ports=ports_for(settings)
    )


@router.get("/callbacks", response_model=list[StaffCallbackOut])
def list_callbacks(
    status: CallbackStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    staff=Depends(reader),
):
    with database() as conn:
        return callbacks.list_callbacks(conn, staff, _keyring(), status, limit)


@router.post(
    "/callbacks/{callback_id}/call",
    response_model=StaffCallbackCallOut,
    status_code=202,
    responses=CODED,
)
def call_callback(callback_id: UUID, staff=Depends(writer)):
    """Call the requester back now through the clinic's outbound voice agent."""
    return callbacks.call(_url(), staff, callback_id, _keyring(), bolna_port(get_settings()))


@router.post(
    "/callbacks/{callback_id}/resolve", response_model=StaffCallbackResolved, responses=CODED
)
def resolve_callback(callback_id: UUID, body: StaffCallbackResolve, staff=Depends(writer)):
    with database() as conn:
        return callbacks.resolve(conn, staff, callback_id, body.status)


@router.post(
    "/callbacks/{callback_id}/reopen", response_model=StaffCallbackReopened, responses=CODED
)
def reopen_callback(callback_id: UUID, staff=Depends(writer)):
    """Return a request whose call was placed or whose outcome is unknown to open, so a
    missed call can be retried deliberately with another "Call now". Never dials."""
    with database() as conn:
        return callbacks.reopen(conn, staff, callback_id)


@router.get("/outbound-status", response_model=StaffOutboundStatus)
def outbound_status(staff=Depends(reader)):
    with database() as conn:
        return callbacks.outbound_status(conn, staff["clinic_id"])


@router.get("/conversations", response_model=list[StaffConversationOut])
def list_conversations(
    channel: Literal["chat", "voice"] | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    staff=Depends(reader),
):
    with database() as conn:
        return staff_platform.conversations(conn, staff, _keyring(), channel, limit)


@router.get("/conversations/{conversation_id}", response_model=StaffConversationDetail)
def get_conversation(conversation_id: UUID, staff=Depends(reader)):
    with database() as conn:
        return staff_platform.conversation(conn, staff, _keyring(), conversation_id)


@router.get("/stats", response_model=StaffStatsOut)
def get_stats(staff=Depends(reader)):
    with database() as conn:
        return staff_platform.stats(conn, staff)
