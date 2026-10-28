from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import AwareDatetime

from clinic_voice import admin
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
    ServiceCreate,
    ServiceOut,
    ServiceUpdate,
)

router = APIRouter(prefix="/v1/admin", tags=["admin"])
reader = require_role("owner", "admin", "viewer")
writer = require_role("owner", "admin")


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
    staff=Depends(reader),
):
    if starts and ends and ends <= starts:
        raise HTTPException(422, "to must be after from")
    with database() as conn:
        return admin.list_exceptions(conn, staff["clinic_id"], starts, ends)


@router.post("/exceptions", response_model=ExceptionCreated, status_code=201)
def create_exception(request: ExceptionCreate, staff=Depends(writer)):
    with database() as conn:
        return admin.create_exception(
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
        admin.delete_exception(conn, staff, exception_id)
    return Response(status_code=204)
