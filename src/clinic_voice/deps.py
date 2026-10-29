from typing import Annotated, NamedTuple
from uuid import UUID

from fastapi import Depends, Header, HTTPException

from clinic_voice import staff as staff_service
from clinic_voice.config import get_settings
from clinic_voice.credentials import GATEWAY_SCOPES, authenticate_credential, parse_any
from clinic_voice.db import connect


def unauthorized():
    # One response for malformed, unknown, revoked and expired credentials.
    return HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": "Bearer"})


def database():
    return connect(get_settings().database_url.get_secret_value())


def staff_session(authorization: Annotated[str | None, Header()] = None):
    scheme, _, token = (authorization or "").partition(" ")
    if scheme != "Bearer" or staff_service.parse_session(token) is None:
        raise unauthorized()  # Tool credentials never parse as staff sessions.
    with database() as conn:
        staff = staff_service.authenticate_session(conn, token)
    if staff is None:
        raise unauthorized()
    return staff


def require_role(*roles):
    def check(staff=Depends(staff_session)):
        if staff["role"] not in roles:
            raise HTTPException(403, "Insufficient role")
        return staff

    return check


class ToolCaller(NamedTuple):
    clinic_id: UUID
    credential_id: UUID


def _credential(authorization, scopes) -> ToolCaller:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme != "Bearer" or parse_any(token, scopes) is None:
        raise unauthorized()  # Reject malformed tokens before touching the database.
    # The credential record alone selects the tenant. Never accept clinic_id from the LLM.
    with database() as conn:
        found = authenticate_credential(conn, token, scopes)
    if found is None:
        raise unauthorized()
    return ToolCaller(*found)


def tool_caller(authorization: Annotated[str | None, Header()] = None) -> ToolCaller:
    """The generic tool API (/v1/tools/*): tools-scope credentials only. A bolna-scope
    credential (the one provisioning hands to the clinic's agent) gets the same generic 401
    as an unknown token."""
    return _credential(authorization, ("tools",))


def tenant(authorization: Annotated[str | None, Header()] = None):
    """The Bolna gateway (/v1/bolna/*): the clinic of a bolna-scope credential, or of a
    tools-scope one (clinics configured by hand, docs/OPERATIONS.md)."""
    return _credential(authorization, GATEWAY_SCOPES).clinic_id
