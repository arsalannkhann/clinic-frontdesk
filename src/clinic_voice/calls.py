"""Real-time calls (ADR 0014): a LiveKit participant token for a chat session.

POST /v1/chat/sessions/{id}/call checks the session (the same 404 as every chat route) and a
per-address hourly limit, then mints a token for a new room. The token's room configuration
dispatches the call agent (call_agent.py) with metadata holding the session's bearer id and the
caller's address, which the agent needs to run the session's tools; the token is signed with
the LiveKit API secret, so the browser that holds it can read but not change it. The caller may
only publish their microphone, and no data.

The token is a standard LiveKit access token (HS256 JWT, claims as livekit-api writes them),
built here with the standard library so the API needs no LiveKit dependency.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time

from clinic_voice import chat, public
from clinic_voice.db import connect

TOKEN_TTL_SECONDS = 600  # To join; the call itself is limited by CALL_MAX_MINUTES.


def unavailable():
    return chat.fail("call_unavailable", 503, "Calling is unavailable; please type instead")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def access_token(api_key: str, api_secret: str, claims: dict, ttl: int, now=None) -> str:
    """An HS256 LiveKit access token: `claims` plus iss, nbf and exp."""
    issued = int(now if now is not None else time.time())
    body = {**claims, "iss": api_key, "nbf": issued, "exp": issued + ttl}
    signing = (
        _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
        + "."
        + _b64(json.dumps(body, separators=(",", ":")).encode())
    )
    signature = hmac.new(api_secret.encode(), signing.encode(), hashlib.sha256).digest()
    return signing + "." + _b64(signature)


def dispatch_metadata(token: str, address) -> str:
    return json.dumps({"session": token, "address": str(address) if address else None})


def start(url, settings, token, address) -> dict:
    """{server_url, participant_token, room} for a call on the session, or 404 / 429 / 503."""
    if not settings.call_configured():
        raise unavailable()
    _keyring, hash_key = public.crypto(settings)
    with connect(url) as conn:
        chat.find_session(conn, token)
        public.limit(
            conn,
            hash_key,
            "chat-call-ip",
            settings.call_ip_per_hour,
            public.HOUR,
            public.client_value(address),
        )
    room = f"call-{secrets.token_hex(12)}"
    claims = {
        "sub": f"caller-{secrets.token_hex(6)}",
        "name": "Caller",
        "video": {
            "roomJoin": True,
            "room": room,
            "canPublish": True,
            "canSubscribe": True,
            "canPublishData": False,
            "canPublishSources": ["microphone"],
        },
        "roomConfig": {
            "agents": [
                {
                    "agentName": settings.call_agent_name,
                    "metadata": dispatch_metadata(token, address),
                }
            ]
        },
    }
    jwt = access_token(
        settings.livekit_api_key,
        settings.livekit_api_secret.get_secret_value(),
        claims,
        TOKEN_TTL_SECONDS,
    )
    return {"server_url": settings.livekit_url, "participant_token": jwt, "room": room}
