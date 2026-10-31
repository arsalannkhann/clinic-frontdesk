"""The trusted edge of the patient-facing APIs (/v1/public/*, /v1/chat/*).

In production the browser never calls the API directly: the web app's server (the Vercel
BFF) proxies to it. Vercel overwrites X-Forwarded-For with its own view, so the API would
only ever see the BFF's egress address and every visitor would share one rate-limit bucket.
With PUBLIC_PROXY_SECRET set:

- every request on these paths must carry `X-Proxy-Secret: <secret>` (constant-time
  comparison); anything else is 403 forbidden, so internet callers cannot bypass the BFF
  and its own limits;
- the client address for rate limits is the BFF-supplied `X-Client-IP`, trusted only on a
  request that proved the secret, and only when it parses as one IP address (otherwise the
  request falls into the shared "unknown" bucket, failing closed).

With the secret unset (local development) nothing is required and the address is the
socket peer, or the TRUSTED_PROXY_HOPS-th X-Forwarded-For entry from the right
(routes_webhooks.client_address), as before.
"""

import hmac
import ipaddress
import logging

from fastapi import Request

from clinic_voice.config import get_settings
from clinic_voice.errors import PublicError
from clinic_voice.routes_webhooks import client_address

log = logging.getLogger("clinic_voice.public")

PROXY_HEADER = "x-proxy-secret"
CLIENT_IP_HEADER = "x-client-ip"


def forbidden() -> PublicError:
    return PublicError(403, "forbidden", "Forbidden")


def _parse_ip(value: str | None):
    if not value:
        return None
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def require_proxy(request: Request) -> None:
    """Router dependency: with PUBLIC_PROXY_SECRET set, refuse requests without the secret.
    Several header lines are refused too (an attacker-added line must not pass)."""
    secret = get_settings().public_proxy_secret
    if secret is None:
        return
    values = request.headers.getlist(PROXY_HEADER)
    supplied = values[0] if len(values) == 1 else ""
    if not hmac.compare_digest(supplied.encode(), secret.get_secret_value().encode()):
        log.warning("public request refused code=forbidden")
        raise forbidden()


def client_ip(request: Request):
    """The rate-limit address of a request that passed require_proxy (see the module
    docstring), or None when it cannot be determined."""
    settings = get_settings()
    if settings.public_proxy_secret is not None:
        values = request.headers.getlist(CLIENT_IP_HEADER)
        return _parse_ip(values[0]) if len(values) == 1 else None
    return client_address(request, settings.trusted_proxy_hops)
