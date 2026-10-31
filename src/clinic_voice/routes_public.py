"""Public clinic API routes (P1): /v1/public/clinics/{slug}/... for the clinic website.

Unauthenticated. The clinic comes from the slug (public clinics only; anything else is the
same 404 not_found). Errors use the envelope {"error": {"code", "message"}} (api.py maps
validation, HTTP and database errors on these paths to it). The client address for rate
limits is the socket peer, or the TRUSTED_PROXY_HOPS-th X-Forwarded-For entry from the
right (routes_webhooks.client_address): the web app's server forwards the browser address.

Catalogue reads may be cached briefly by shared caches; slots and every POST are no-store.
"""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response

from clinic_voice import public
from clinic_voice.config import get_settings
from clinic_voice.deps import database
from clinic_voice.routes_webhooks import client_address
from clinic_voice.schemas import (
    PublicBookingOut,
    PublicBookingRequest,
    PublicCallbackOut,
    PublicCallbackRequest,
    PublicClinicOut,
    PublicDoctorOut,
    PublicErrorEnvelope,
    PublicLookupRequest,
    PublicServiceOut,
    PublicSlotsOut,
)
from clinic_voice.tick import ports_for

ERRORS = {status: {"model": PublicErrorEnvelope} for status in (404, 409, 422, 429, 503)}
router = APIRouter(prefix="/v1/public/clinics/{slug}", tags=["public"], responses=ERRORS)

CATALOGUE_CACHE = "public, max-age=60"
NO_STORE = "no-store"


def _url():
    return get_settings().database_url.get_secret_value()


def _clinic(slug):
    with database() as conn:
        return public.clinic_by_slug(conn, slug)


@router.get("", response_model=PublicClinicOut)
def clinic_profile(slug: str, response: Response):
    response.headers["Cache-Control"] = CATALOGUE_CACHE
    return public.profile(_clinic(slug))


@router.get("/services", response_model=list[PublicServiceOut])
def clinic_services(slug: str, response: Response):
    response.headers["Cache-Control"] = CATALOGUE_CACHE
    with database() as conn:
        clinic = public.clinic_by_slug(conn, slug)
        return public.list_services(conn, clinic["id"])


@router.get("/doctors", response_model=list[PublicDoctorOut])
def clinic_doctors(slug: str, response: Response):
    response.headers["Cache-Control"] = CATALOGUE_CACHE
    with database() as conn:
        clinic = public.clinic_by_slug(conn, slug)
        return public.list_doctors(conn, clinic["id"])


@router.get("/slots", response_model=PublicSlotsOut)
def clinic_slots(
    slug: str,
    response: Response,
    service_id: UUID,
    day: Annotated[date, Query(alias="date")],
    doctor_id: UUID | None = None,
    days: Annotated[int, Query(ge=1, le=7)] = 1,
):
    """Free starts of a public service on `days` clinic-local dates from `date`, for one
    doctor or every doctor offering it. Times are clinic-local ISO 8601 with offset."""
    response.headers["Cache-Control"] = NO_STORE
    with database() as conn:
        clinic = public.clinic_by_slug(conn, slug)
        return public.list_slots(conn, clinic, service_id, doctor_id, day, days)


@router.post("/bookings", response_model=PublicBookingOut, status_code=202)
def clinic_booking(
    slug: str,
    body: PublicBookingRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str | None, Header(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    ] = None,
):
    """Book an offered slot (channel web). 202 with status confirmed once the calendar of
    record accepted it (ledger doctors: normally before this returns), else pending_sync.
    An optional Idempotency-Key makes a retried submission return the same booking."""
    response.headers["Cache-Control"] = NO_STORE
    settings = get_settings()
    clinic = _clinic(slug)
    return public.book(
        _url(),
        settings,
        clinic,
        body,
        client_address(request, settings.trusted_proxy_hops),
        idempotency_key,
        ports=ports_for(settings),
    )


@router.post("/bookings/lookup", response_model=PublicBookingOut)
def clinic_booking_lookup(
    slug: str, body: PublicLookupRequest, request: Request, response: Response
):
    """A booking by reference + phone. Any mismatch is the same 404 not_found."""
    response.headers["Cache-Control"] = NO_STORE
    settings = get_settings()
    clinic = _clinic(slug)
    return public.lookup(
        _url(), settings, clinic, body, client_address(request, settings.trusted_proxy_hops)
    )


@router.post("/callbacks", response_model=PublicCallbackOut, status_code=201)
def clinic_callback(slug: str, body: PublicCallbackRequest, request: Request, response: Response):
    """Ask the clinic to call back (source web). Staff see it in the callback queue."""
    response.headers["Cache-Control"] = NO_STORE
    settings = get_settings()
    clinic = _clinic(slug)
    return public.request_callback(
        _url(), settings, clinic, body, client_address(request, settings.trusted_proxy_hops)
    )
