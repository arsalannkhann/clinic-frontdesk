"""Bolna execution webhooks: POST /v1/webhooks/bolna/{webhook_token} (T8).

Bolna posts the execution payload (the GET /executions shape) to the agent's webhook_url on
every status change. The docs describe no signature, secret or API-settable webhook header,
so the clinic's webhook credential (cvw_<id>.<secret>, scope 'webhook') is the last path
segment; access logs redact it (api.StripQuery). Order of checks, each refusing with nothing
stored: optional source IP allowlist (403), credential (401), body size (413), JSON shape
(422), configured agent id: inbound or outbound reminder agent (403). The minimized event and its webhook.process job commit
together before the 2xx; a duplicate delivery also gets 2xx and creates nothing.
"""

import ipaddress
import json
import logging

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from clinic_voice import credentials
from clinic_voice.bolna import webhooks
from clinic_voice.config import get_settings
from clinic_voice.deps import database
from clinic_voice.providers.bolna import minimize_execution
from clinic_voice.schemas import WebhookReceipt

log = logging.getLogger("clinic_voice.bolna")

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])
NO_STORE = {"Cache-Control": "no-store"}
TOO_LARGE = "Request body too large"
UNRECOGNIZED = "Unrecognized webhook event"


class _DuplicateKey(Exception):
    pass


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def unauthorized():
    # One response for malformed, unknown, revoked, expired and wrong-scope credentials.
    return HTTPException(401, "Invalid credentials")


def client_address(request: Request, hops: int):
    """The client IP: the socket peer, or with `hops` trusted proxies the hops-th address
    from the right of X-Forwarded-For. Every X-Forwarded-For header line counts (lines are
    joined in order, as RFC 9110 combines repeated fields), so a second line cannot hide the
    proxy's append. IPv4-mapped IPv6 addresses (::ffff:a.b.c.d) are compared as IPv4. None
    when it cannot be determined."""
    if hops > 0:
        forwarded = ",".join(request.headers.getlist("x-forwarded-for"))
        parts = [part.strip() for part in forwarded.split(",") if part.strip()]
        value = parts[-hops] if len(parts) >= hops else None
    else:
        value = request.client.host if request.client else None
    try:
        address = ipaddress.ip_address(value) if value else None
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def source_allowed(request: Request, settings) -> bool:
    networks = settings.webhook_networks()
    if networks is None:
        return True
    address = client_address(request, settings.trusted_proxy_hops)
    return address is not None and any(address in network for network in networks)


async def read_limited(request: Request, limit: int) -> bytes:
    """The body, refusing (413) as soon as it exceeds `limit` bytes; a declared
    Content-Length above the limit is refused before anything is read."""
    declared = request.headers.get("content-length")
    if declared is not None:
        if not declared.isascii() or not declared.isdigit():
            raise HTTPException(400, "Invalid Content-Length")
        if int(declared) > limit:
            raise HTTPException(413, TOO_LARGE)
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, TOO_LARGE)
        chunks.append(chunk)
    return b"".join(chunks)


def parse_event(body: bytes) -> dict:
    """The minimized event, or 422 with a fixed message (no part of the body is echoed)."""
    try:
        data = json.loads(body, object_pairs_hook=_unique_pairs)
    except (_DuplicateKey, ValueError, RecursionError):
        raise HTTPException(422, UNRECOGNIZED) from None
    minimized = minimize_execution(data)
    if minimized is None:
        raise HTTPException(422, UNRECOGNIZED)
    return minimized


def _authenticate(token):
    with database() as conn:
        return credentials.authenticate(conn, token, scope="webhook")


def _store(clinic_id, minimized) -> bool:
    with database() as conn:
        if webhooks.agent_mismatch(conn, clinic_id, minimized):
            log.warning("webhook refused code=agent_mismatch")
            raise HTTPException(403, "Agent not configured for this clinic")
        return webhooks.record(conn, clinic_id, minimized)


@router.post(
    "/bolna/{webhook_token}",
    response_model=WebhookReceipt,
    responses={
        401: {"description": "Invalid credentials"},
        403: {"description": "Source address or agent not allowed"},
        413: {"description": "Request body too large"},
        422: {"description": "Unrecognized webhook event"},
    },
)
async def bolna_webhook(webhook_token: str, request: Request, response: Response):
    """Receive a Bolna execution status update. The path token is the clinic's webhook
    credential; nothing in the body selects the clinic."""
    response.headers.update(NO_STORE)
    settings = get_settings()
    if not source_allowed(request, settings):
        raise HTTPException(403, "Forbidden")
    if credentials.parse(webhook_token, scope="webhook") is None:
        raise unauthorized()  # Malformed: refused before touching the database.
    clinic_id = await run_in_threadpool(_authenticate, webhook_token)
    if clinic_id is None:
        raise unauthorized()
    body = await read_limited(request, settings.webhook_max_body_bytes)
    minimized = parse_event(body)
    created = await run_in_threadpool(_store, clinic_id, minimized)
    return {"status": "accepted" if created else "duplicate"}
