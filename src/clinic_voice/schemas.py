from datetime import date
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


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
