"""Internal routes: POST /v1/internal/worker/tick (P1, ADR 0010).

The scheduler (Supabase pg_cron through pg_net) calls the tick every minute with
`Authorization: Bearer <WORKER_TICK_SECRET>`. Unset secret: the route answers 404 as if it
did not exist. A missing or wrong secret gets 401 (constant-time comparison). The response
carries counts only.
"""

import hmac
import logging
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Response

from clinic_voice.config import get_settings
from clinic_voice.schemas import WorkerTickOut
from clinic_voice.tick import ports_for, run_tick

log = logging.getLogger("clinic_voice.worker")

router = APIRouter(prefix="/v1/internal", tags=["internal"])


def _authorized(authorization: str | None, secret: str) -> bool:
    scheme, _, token = (authorization or "").partition(" ")
    # Compare even for a wrong scheme, so timing does not tell the two apart.
    matches = hmac.compare_digest(token.encode(), secret.encode())
    return scheme == "Bearer" and matches


@router.post(
    "/worker/tick",
    response_model=WorkerTickOut,
    responses={401: {"description": "Invalid credentials"}, 404: {"description": "Disabled"}},
)
def worker_tick(response: Response, authorization: Annotated[str | None, Header()] = None):
    """Run due outbox jobs for at most WORKER_TICK_BUDGET_SECONDS, then housekeeping."""
    response.headers["Cache-Control"] = "no-store"
    settings = get_settings()
    secret = settings.worker_tick_secret
    if secret is None:
        raise HTTPException(404, "Not Found")
    if not _authorized(authorization, secret.get_secret_value()):
        log.warning("worker tick refused code=unauthorized")
        raise HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": "Bearer"})
    result = run_tick(
        settings.database_url.get_secret_value(),
        ports_for(settings),
        settings.worker_tick_budget_seconds,
    )
    log.info("worker tick claimed=%d housekeeping=%s", result["claimed"], result["housekeeping"])
    return result
