"""Staff appointment routes (T7). The clinic comes from the staff session only."""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import AwareDatetime

from clinic_voice import appointments
from clinic_voice.config import get_settings
from clinic_voice.crypto import CryptoError, Keyring
from clinic_voice.deps import database, require_role
from clinic_voice.schemas import AppointmentResolve, AppointmentState, StaffAppointmentOut

log = logging.getLogger("clinic_voice.appointments")

router = APIRouter(prefix="/v1/admin/appointments", tags=["admin"])
reader = require_role("owner", "admin", "viewer")
writer = require_role("owner", "admin")


def _keyring() -> Keyring | None:
    """The application keyring for patient fields, or None (names are then omitted)."""
    keys = get_settings().token_encryption_keys
    if not keys or not keys.get_secret_value():
        return None
    try:
        return Keyring.from_setting(keys.get_secret_value())
    except CryptoError:
        log.error("patient keyring is invalid; patient details omitted")
        return None


@router.get("", response_model=list[StaffAppointmentOut])
def list_appointments(
    starts: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    ends: Annotated[AwareDatetime | None, Query(alias="to")] = None,
    status: AppointmentState | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    staff=Depends(reader),
):
    if starts and ends and ends <= starts:
        raise HTTPException(422, "to must be after from")
    keyring = _keyring() if staff["role"] in ("owner", "admin") else None
    with database() as conn:
        return appointments.list_appointments(conn, staff, keyring, starts, ends, status, limit)


@router.post("/{appointment_id}/cancel", response_model=StaffAppointmentOut)
def cancel_appointment(appointment_id: UUID, staff=Depends(writer)):
    with database() as conn:
        return appointments.staff_cancel(conn, staff, appointment_id)


@router.post("/{appointment_id}/resolve", response_model=StaffAppointmentOut)
def resolve_appointment(appointment_id: UUID, request: AppointmentResolve, staff=Depends(writer)):
    with database() as conn:
        return appointments.resolve(conn, staff, appointment_id, request.action)
