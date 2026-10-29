from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import AwareDatetime

from clinic_voice import admin, provisioning, reminders, schedule_exceptions
from clinic_voice.deps import database, require_role
from clinic_voice.schemas import (
    ClinicConfig,
    ClinicUpdate,
    DoctorCreate,
    DoctorOut,
    DoctorUpdate,
    ExceptionCreate,
    ExceptionCreated,
    ExceptionOut,
    HoursReplace,
    HoursWindow,
    OutboxJobOut,
    OutboxStatus,
    ProvisioningAdvanced,
    ProvisioningNumber,
    ProvisioningOut,
    ReminderOut,
    ReminderStatus,
    ServiceCreate,
    ServiceOut,
    ServiceUpdate,
)

router = APIRouter(prefix="/v1/admin", tags=["admin"])
reader = require_role("owner", "admin", "viewer")
writer = require_role("owner", "admin")
owner = require_role("owner")


@router.get("/clinic", response_model=ClinicConfig)
def get_clinic(staff=Depends(reader)):
    with database() as conn:
        return admin.get_clinic(conn, staff["clinic_id"])


@router.patch("/clinic", response_model=ClinicConfig)
def patch_clinic(request: ClinicUpdate, staff=Depends(writer)):
    with database() as conn:
        return admin.update_clinic(conn, staff, request.model_dump(exclude_unset=True))


@router.get("/doctors", response_model=list[DoctorOut])
def list_doctors(staff=Depends(reader)):
    with database() as conn:
        return admin.list_doctors(conn, staff["clinic_id"])


@router.post("/doctors", response_model=DoctorOut, status_code=201)
def create_doctor(request: DoctorCreate, staff=Depends(writer)):
    with database() as conn:
        return admin.create_doctor(conn, staff, request.name)


@router.patch("/doctors/{doctor_id}", response_model=DoctorOut)
def patch_doctor(doctor_id: UUID, request: DoctorUpdate, staff=Depends(writer)):
    with database() as conn:
        return admin.update_doctor(conn, staff, doctor_id, request.model_dump(exclude_unset=True))


@router.get("/services", response_model=list[ServiceOut])
def list_services(staff=Depends(reader)):
    with database() as conn:
        return admin.list_services(conn, staff["clinic_id"])


@router.post("/services", response_model=ServiceOut, status_code=201)
def create_service(request: ServiceCreate, staff=Depends(writer)):
    with database() as conn:
        return admin.create_service(
            conn,
            staff,
            request.doctor_id,
            request.name,
            request.duration_minutes,
            request.buffer_after_minutes,
        )


@router.patch("/services/{service_id}", response_model=ServiceOut)
def patch_service(service_id: UUID, request: ServiceUpdate, staff=Depends(writer)):
    with database() as conn:
        return admin.update_service(conn, staff, service_id, request.model_dump(exclude_unset=True))


@router.get("/doctors/{doctor_id}/hours", response_model=list[HoursWindow])
def get_hours(doctor_id: UUID, staff=Depends(reader)):
    with database() as conn:
        return admin.get_hours(conn, staff["clinic_id"], doctor_id)


@router.put("/doctors/{doctor_id}/hours", response_model=list[HoursWindow])
def put_hours(doctor_id: UUID, request: HoursReplace, staff=Depends(writer)):
    with database() as conn:
        return admin.replace_hours(conn, staff, doctor_id, request.root)


@router.get("/exceptions", response_model=list[ExceptionOut])
def list_exceptions(
    starts: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    ends: Annotated[AwareDatetime | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    staff=Depends(reader),
):
    if starts and ends and ends <= starts:
        raise HTTPException(422, "to must be after from")
    with database() as conn:
        return schedule_exceptions.list_exceptions(conn, staff["clinic_id"], starts, ends, limit)


@router.post("/exceptions", response_model=ExceptionCreated, status_code=201)
def create_exception(request: ExceptionCreate, staff=Depends(writer)):
    with database() as conn:
        return schedule_exceptions.create_exception(
            conn,
            staff,
            request.doctor_id,
            request.kind,
            request.starts_at,
            request.ends_at,
            request.label,
        )


@router.delete("/exceptions/{exception_id}", status_code=204)
def delete_exception(exception_id: UUID, staff=Depends(writer)):
    with database() as conn:
        schedule_exceptions.delete_exception(conn, staff, exception_id)
    return Response(status_code=204)


@router.get("/outbox", response_model=list[OutboxJobOut])
def list_outbox(
    status: OutboxStatus = "dead",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    staff=Depends(writer),
):
    with database() as conn:
        return admin.list_outbox(conn, staff["clinic_id"], status, limit)


@router.post("/outbox/{job_id}/requeue", response_model=OutboxJobOut)
def requeue_outbox(job_id: UUID, staff=Depends(writer)):
    with database() as conn:
        return admin.requeue_outbox(conn, staff, job_id)


@router.get("/reminders", response_model=list[ReminderOut])
def list_reminders(
    status: ReminderStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    staff=Depends(writer),
):
    """Planned and attempted contacts (confirmations, reminder calls and messages), e.g.
    status=needs_review for contacts whose outcome is unknown."""
    with database() as conn:
        return reminders.list_reminders(conn, staff["clinic_id"], status, limit)


@router.get("/provisioning", response_model=ProvisioningOut)
def get_provisioning(staff=Depends(reader)):
    """Provisioning state, provider ids (no secrets, no phone numbers) and recent events."""
    with database() as conn:
        return provisioning.view(conn, staff["clinic_id"])


@router.post("/provisioning/advance", response_model=ProvisioningAdvanced, status_code=202)
def advance_provisioning(staff=Depends(owner)):
    """Enqueue the next provisioning step (owner). 409 if a step is already pending or the
    state has no next step. A worker with a Bolna port runs it."""
    with database() as conn:
        return provisioning.advance(conn, staff)


@router.post("/provisioning/number", response_model=ProvisioningOut)
def select_provisioning_number(request: ProvisioningNumber, staff=Depends(owner)):
    """Select an existing number of the Bolna account by id (owner). Nothing is purchased;
    the number step checks the id against the account's numbers."""
    with database() as conn:
        return provisioning.select_number(conn, staff, request.phone_number_id)


@router.post("/provisioning/activate", response_model=ProvisioningOut)
def activate_provisioning(staff=Depends(owner)):
    """verified -> active (owner). Does not enable outbound contact."""
    with database() as conn:
        return provisioning.activate(conn, staff)


@router.post("/provisioning/reset", response_model=ProvisioningOut)
def reset_provisioning(staff=Depends(owner)):
    """failed | manual_review -> the state whose step must run again (owner)."""
    with database() as conn:
        return provisioning.reset(conn, staff)
