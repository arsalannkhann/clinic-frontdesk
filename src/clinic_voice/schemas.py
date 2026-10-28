from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SlotQuery(StrictModel):
    doctor_id: UUID
    service_id: UUID
    day: date


class ReservationRequest(StrictModel):
    doctor_id: UUID
    service_id: UUID
    starts_at: AwareDatetime


class ReservationResponse(StrictModel):
    appointment_id: UUID
    status: str
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    spoken_confirmation: str


class SlotResponse(StrictModel):
    slots: list[AwareDatetime] = Field(default_factory=list)
    source: str = "local_reservations_only"


Email = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
]
NewPassword = Annotated[str, Field(min_length=12, max_length=256)]
Role = Literal["owner", "admin", "viewer"]


class LoginRequest(StrictModel):
    email: Email
    password: str = Field(min_length=1, max_length=256)


class StaffUser(StrictModel):
    id: UUID
    email: str
    role: Role


class StaffUserDetail(StaffUser):
    disabled: bool


class SessionResponse(StrictModel):
    token: str
    expires_at: AwareDatetime
    user: StaffUser


class PasswordChange(StrictModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: NewPassword


class StaffUserCreate(StrictModel):
    email: Email
    role: Role
    temporary_password: NewPassword


class StaffUserUpdate(StrictModel):
    role: Role | None = None
    disabled: bool | None = None
