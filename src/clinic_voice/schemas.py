import re
from datetime import date, timedelta
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import available_timezones

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    RootModel,
    StringConstraints,
    model_validator,
)


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


# Clinic configuration (staff admin)
ZONES = frozenset(available_timezones())


def _iana_zone(value: str) -> str:
    # Names such as "local" or "" are not canonical IANA keys; require a known key.
    if value not in ZONES:
        raise ValueError("unknown IANA timezone")
    return value


Timezone = Annotated[str, Field(min_length=1, max_length=64), AfterValidator(_iana_zone)]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
LeadTime = Annotated[int, Field(ge=0, le=10080)]
HorizonDays = Annotated[int, Field(ge=1, le=365)]
Duration = Annotated[int, Field(ge=5, le=240)]
Buffer = Annotated[int, Field(ge=0, le=120)]
ClockTime = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]


class PatchModel(StrictModel):
    """Partial update: at least one field, and no explicit nulls."""

    @model_validator(mode="after")
    def _present(self):
        if not self.model_fields_set:
            raise ValueError("no fields to update")
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("null is not allowed")
        return self


class ClinicConfig(StrictModel):
    id: UUID
    name: str
    timezone: str
    lead_time_minutes: int
    horizon_days: int


class ClinicUpdate(PatchModel):
    name: DisplayName | None = None
    timezone: Timezone | None = None
    lead_time_minutes: LeadTime | None = None
    horizon_days: HorizonDays | None = None


class DoctorCreate(StrictModel):
    name: DisplayName


CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


def _no_control_characters(value):
    # Checked before trimming so a trailing newline is rejected rather than silently dropped.
    if isinstance(value, str) and CONTROL_CHARACTERS.search(value):
        raise ValueError("control characters are not allowed")
    return value


# Opaque provider calendar id (for Google, an email-like string); never patient data.
CalendarId = Annotated[
    str,
    BeforeValidator(_no_control_characters),
    StringConstraints(min_length=1, max_length=255),
]


class DoctorUpdate(PatchModel):
    name: DisplayName | None = None
    active: bool | None = None
    booking_calendar_id: CalendarId | None = None  # null unsets the mapping
    busy_calendar_ids: Annotated[list[CalendarId], Field(max_length=10)] | None = None

    @model_validator(mode="after")
    def _present(self):
        # Overrides PatchModel: booking_calendar_id may be an explicit null.
        if not self.model_fields_set:
            raise ValueError("no fields to update")
        if any(
            getattr(self, name) is None
            for name in self.model_fields_set
            if name != "booking_calendar_id"
        ):
            raise ValueError("null is not allowed")
        return self


class DoctorOut(StrictModel):
    id: UUID
    name: str
    active: bool
    booking_calendar_id: str | None
    busy_calendar_ids: list[str]


class ServiceCreate(StrictModel):
    doctor_id: UUID
    name: DisplayName
    duration_minutes: Duration
    buffer_after_minutes: Buffer = 0


class ServiceUpdate(PatchModel):
    name: DisplayName | None = None
    duration_minutes: Duration | None = None
    buffer_after_minutes: Buffer | None = None
    active: bool | None = None


class ServiceOut(StrictModel):
    id: UUID
    doctor_id: UUID
    name: str
    duration_minutes: int
    buffer_after_minutes: int
    active: bool


class HoursWindow(StrictModel):
    weekday: int = Field(ge=0, le=6)
    opens: ClockTime
    closes: ClockTime

    @model_validator(mode="after")
    def _ordered(self):
        if self.opens >= self.closes:  # Zero-padded HH:MM compares chronologically.
            raise ValueError("opens must be before closes")
        return self


class HoursReplace(RootModel[Annotated[list[HoursWindow], Field(max_length=50)]]):
    @model_validator(mode="after")
    def _no_overlap(self):
        by_day = {}
        for window in self.root:
            by_day.setdefault(window.weekday, []).append((window.opens, window.closes))
        for windows in by_day.values():
            windows.sort()
            for (_, end), (start, _) in zip(windows, windows[1:], strict=False):
                if start < end:
                    raise ValueError("overlapping windows")
        return self


# Schedule exceptions (holidays, leave, breaks)
ExceptionKind = Literal["holiday", "leave", "break"]
# Operational note such as "Diwali" or "Lunch"; never patient details.
ExceptionLabel = Annotated[
    str,
    BeforeValidator(_no_control_characters),
    StringConstraints(strip_whitespace=True, max_length=80),
]
MAX_EXCEPTION = timedelta(days=366)


class ExceptionCreate(StrictModel):
    doctor_id: UUID | None = None  # None applies to every doctor in the clinic.
    kind: ExceptionKind
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    label: ExceptionLabel = ""

    @model_validator(mode="after")
    def _range(self):
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        if self.ends_at - self.starts_at > MAX_EXCEPTION:
            raise ValueError("exception longer than 366 days")
        return self


class ExceptionOut(StrictModel):
    id: UUID
    doctor_id: UUID | None
    kind: ExceptionKind
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    label: str


class ExceptionCreated(ExceptionOut):
    # Active appointments overlapping the exception; they are not cancelled automatically.
    # At most 100 ids; conflict_count is the true total.
    conflicting_appointment_ids: list[UUID]
    conflict_count: int
