from typing import Annotated

from fastapi import Depends, Header, HTTPException

from clinic_voice import staff as staff_service
from clinic_voice.config import get_settings
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
