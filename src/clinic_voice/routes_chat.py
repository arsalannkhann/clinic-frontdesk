"""Chat assistant routes (P2, ADR 0011): /v1/chat/*.

Unauthenticated like the public API (the BFF proxy secret applies, edge.py); a session is
addressed by its bearer id in the path. Errors use the public envelope
{"error": {"code", "message"}}. Every response is no-store.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response

from clinic_voice import chat, public
from clinic_voice.config import get_settings
from clinic_voice.deps import database
from clinic_voice.edge import client_ip, require_proxy
from clinic_voice.providers import chat_llm
from clinic_voice.schemas import (
    ChatConfirmRequest,
    ChatMessageRequest,
    ChatSessionOut,
    ChatTurnOut,
    PublicBookingOut,
    PublicErrorEnvelope,
)
from clinic_voice.tick import ports_for

ERRORS = {status: {"model": PublicErrorEnvelope} for status in (403, 404, 409, 410, 422, 429, 503)}
router = APIRouter(
    prefix="/v1/chat", tags=["chat"], responses=ERRORS, dependencies=[Depends(require_proxy)]
)

_llm_cache: dict[int, object] = {}


def llm_for(settings):
    """The configured chat model adapter (cached per Settings object), or None."""
    key = id(settings)
    if key not in _llm_cache:
        _llm_cache.clear()
        _llm_cache[key] = chat_llm.build(settings)
    return _llm_cache[key]


def _url():
    return get_settings().database_url.get_secret_value()


@router.post("/clinics/{slug}/sessions", response_model=ChatSessionOut, status_code=201)
def create_session(slug: str, request: Request, response: Response):
    """Start a conversation with the clinic's virtual assistant."""
    response.headers["Cache-Control"] = "no-store"
    with database() as conn:
        clinic = public.clinic_by_slug(conn, slug)
    return chat.create_session(_url(), get_settings(), clinic, client_ip(request))


@router.get("/sessions/{session_id}", response_model=ChatTurnOut)
def get_session(session_id: str, response: Response):
    """The visible transcript (user and assistant text), the open proposal, quick replies."""
    response.headers["Cache-Control"] = "no-store"
    return chat.history(_url(), get_settings(), session_id)


@router.post("/sessions/{session_id}/messages", response_model=ChatTurnOut)
def send_message(session_id: str, body: ChatMessageRequest, request: Request, response: Response):
    """Send the patient's next message; the history stays on the server."""
    response.headers["Cache-Control"] = "no-store"
    settings = get_settings()
    return chat.send(_url(), settings, session_id, body.text, client_ip(request), llm_for(settings))


@router.post(
    "/sessions/{session_id}/proposals/{proposal_id}/confirm",
    response_model=PublicBookingOut,
    status_code=202,
)
def confirm_proposal(
    session_id: str,
    proposal_id: UUID,
    body: ChatConfirmRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str | None, Header(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    ] = None,
):
    """Book the proposed time (channel chat) with the name, phone and consent from the
    confirmation card. Same response as a web booking; repeated confirms replay it."""
    response.headers["Cache-Control"] = "no-store"
    settings = get_settings()
    return chat.confirm(
        _url(),
        settings,
        session_id,
        proposal_id,
        body,
        client_ip(request),
        idempotency_key,
        ports=ports_for(settings),
    )
