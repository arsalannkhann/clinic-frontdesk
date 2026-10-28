from uuid import UUID

from fastapi import APIRouter, Depends

from clinic_voice import admin
from clinic_voice.deps import database, require_role
from clinic_voice.schemas import (
    ClinicConfig,
    ClinicUpdate,
    DoctorCreate,
    DoctorOut,
    DoctorUpdate,
    HoursReplace,
    HoursWindow,
    ServiceCreate,
    ServiceOut,
    ServiceUpdate,
)

router = APIRouter(prefix="/v1/admin")
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
