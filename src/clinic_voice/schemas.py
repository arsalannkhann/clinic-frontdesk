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
    booking_reference: str
    status: str
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    spoken_confirmation: str


class AppointmentStatusRequest(StrictModel):
    appointment_id: UUID


class AppointmentStatusResponse(StrictModel):
    appointment_id: UUID
    status: str
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    spoken_status: str


# Slots come from the ledger plus the cached (advisory) external calendar busy time.
SLOT_SOURCE = "ledger_and_cached_calendar"


class SlotResponse(StrictModel):
    slots: list[AwareDatetime] = Field(default_factory=list)
    source: str = SLOT_SOURCE


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


ReminderLead = Annotated[int, Field(ge=60, le=10080)]
ContactBudget = Annotated[int, Field(ge=0, le=100000)]


def _plain_script(value):
    # Spoken scripts: no control characters (newlines included) and no braces or percent
    # signs, which Bolna would read as template variables or format specifiers.
    if isinstance(value, str) and re.search(r"[\x00-\x1f\x7f{}%]", value):
        raise ValueError("control characters, braces and percent signs are not allowed")
    return value


# Empty clears the text (and with it the approval).
ScriptText = Annotated[
    str,
    BeforeValidator(_plain_script),
    StringConstraints(strip_whitespace=True, max_length=1000),
]


class ClinicConfig(StrictModel):
    id: UUID
    name: str
    timezone: str
    lead_time_minutes: int
    horizon_days: int
    # Contact policy (T9). outbound_enabled is false by default: no confirmations, reminder
    # calls or messages are sent for the clinic until an owner or admin enables it.
    outbound_enabled: bool
    quiet_hours_start: str  # Clinic-local HH:MM; a window with start > end wraps midnight.
    quiet_hours_end: str
    reminder_lead_minutes: int
    daily_contact_budget: int
    # Clinic-approved scripts (T10). scripts_approved is cleared whenever a text changes
    # without being approved in the same request; activation requires both, approved.
    emergency_text: str
    handoff_text: str
    scripts_approved: bool
    # While provisioning is verified, also allow booking-changing tools (owner test calls).
    test_calls_enabled: bool


class ClinicUpdate(PatchModel):
    name: DisplayName | None = None
    timezone: Timezone | None = None
    lead_time_minutes: LeadTime | None = None
    horizon_days: HorizonDays | None = None
    outbound_enabled: bool | None = None
    quiet_hours_start: ClockTime | None = None
    quiet_hours_end: ClockTime | None = None
    reminder_lead_minutes: ReminderLead | None = None
    daily_contact_budget: ContactBudget | None = None
    emergency_text: ScriptText | None = None
    handoff_text: ScriptText | None = None
    scripts_approved: bool | None = None  # Owner only.
    test_calls_enabled: bool | None = None  # Owner only.

    @model_validator(mode="after")
    def _quiet_window(self):
        if self.quiet_hours_start is not None and self.quiet_hours_start == self.quiet_hours_end:
            raise ValueError("quiet hours start and end must differ")
        return self


class DoctorCreate(StrictModel):
    name: DisplayName


CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


def _no_control_characters(value):
    # Checked before trimming so a trailing newline is rejected rather than silently dropped.
    if isinstance(value, str) and CONTROL_CHARACTERS.search(value):
        raise ValueError("control characters are not allowed")
    return value


def _non_blank(value):
    # Re-checked after trimming: a whitespace-only id must not reach the database.
    if not value.strip():
        raise ValueError("calendar id must not be blank")
    return value


# Opaque provider calendar id (for Google, an email-like string); never patient data.
CalendarId = Annotated[
    str,
    BeforeValidator(_no_control_characters),
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
    AfterValidator(_non_blank),
]


def _unique_in_order(values):
    return list(dict.fromkeys(values))


class DoctorUpdate(PatchModel):
    name: DisplayName | None = None
    active: bool | None = None
    booking_calendar_id: CalendarId | None = None  # null unsets the mapping
    # A calendar account of this clinic (null: use the clinic's only active account).
    calendar_account_id: UUID | None = None
    busy_calendar_ids: (
        Annotated[list[CalendarId], Field(max_length=10), AfterValidator(_unique_in_order)] | None
    ) = None

    @model_validator(mode="after")
    def _present(self):
        # Overrides PatchModel: booking_calendar_id may be an explicit null.
        if not self.model_fields_set:
            raise ValueError("no fields to update")
        if any(
            getattr(self, name) is None
            for name in self.model_fields_set
            if name not in ("booking_calendar_id", "calendar_account_id")
        ):
            raise ValueError("null is not allowed")
        return self


class DoctorOut(StrictModel):
    id: UUID
    name: str
    active: bool
    booking_calendar_id: str | None
    busy_calendar_ids: list[str]
    calendar_account_id: UUID | None


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


OutboxStatus = Literal["dead", "pending", "processing"]


class OutboxJobOut(StrictModel):
    """Operational view of a job: no payload contents."""

    id: UUID
    kind: str
    status: str
    attempts: int
    max_attempts: int
    last_error: str | None
    appointment_id: UUID | None
    available_at: AwareDatetime
    created_at: AwareDatetime
    updated_at: AwareDatetime


# Calendar accounts (Google OAuth)
class AuthorizationStart(StrictModel):
    authorization_url: str


class CalendarAccountOut(StrictModel):
    """No token material, ever."""

    id: UUID
    provider: str
    status: str
    scopes: list[str]
    connected_by: UUID
    created_at: AwareDatetime
    revoked_at: AwareDatetime | None


# Bolna trusted gateway (routes_bolna.py). Bolna renders each tool's `param` template with
# %(name)s specifiers, so every value arrives as a string. execution_id, from_number and
# agent_id are Bolna call-context variables (docs/SOURCE_REVIEW.md): provider-asserted call
# metadata, never tenant or caller identity. The tenant comes only from the tool credential.
# Unknown fields are rejected like everywhere else.
ProviderNumber = Annotated[str, BeforeValidator(_no_control_characters), Field(max_length=32)]
CatalogName = Annotated[
    str,
    BeforeValidator(_no_control_characters),
    StringConstraints(strip_whitespace=True, min_length=1, max_length=120),
]


class BolnaCallContext(StrictModel):
    execution_id: UUID
    # The number that initiated the call. Stored encrypted when it is E.164, else ignored
    # (withheld numbers, unrendered templates). Never echoed.
    from_number: ProviderNumber = ""
    # Recorded when it is a UUID (Bolna agent ids are), else ignored like from_number.
    agent_id: Annotated[str, BeforeValidator(_no_control_characters), Field(max_length=64)] = ""


class BolnaSlotQuery(BolnaCallContext):
    doctor: CatalogName
    service: CatalogName
    day: date


CONSENT_UNCLEAR = "unclear"
CONSENT_VALUES = {"true": True, "yes": True, "false": False, "no": False, "": None}


def _consent_flag(value):
    """Tri-state consent from a rendered template value: "true"/"yes" grants, "false"/"no"
    declines, empty means the caller was not asked. Anything else becomes CONSENT_UNCLEAR,
    which the gateway refuses with a speakable "please answer yes or no" before booking
    (never silently dropped, never guessed)."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, str):
        return CONSENT_VALUES.get(value.strip().casefold(), CONSENT_UNCLEAR)
    raise ValueError("consent must be true, false or empty")


def _read_back(value):
    """Only an explicit true (JSON true or the string "true") confirms a change; any other
    string means not confirmed."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() == "true"
    raise ValueError("confirmed must be true or false")


def _language(value):
    # The LLM fills this from the conversation; anything unsupported is ignored.
    if isinstance(value, str) and value.strip().casefold() in ("en", "hi"):
        return value.strip().casefold()
    if isinstance(value, str):
        return ""
    raise ValueError("language must be a string")


ConsentFlag = Annotated[bool | Literal["unclear"] | None, BeforeValidator(_consent_flag)]
ReadBack = Annotated[bool, BeforeValidator(_read_back)]
PatientName = Annotated[
    str,
    BeforeValidator(_no_control_characters),
    StringConstraints(strip_whitespace=True, max_length=120),
]
# Free-form spoken reference; normalized and checked by the gateway, where a malformed
# value counts as a failed verification exactly like a wrong one.
SpokenReference = Annotated[
    str,
    BeforeValidator(_no_control_characters),
    StringConstraints(strip_whitespace=True, max_length=32),
]


class BolnaReservation(BolnaCallContext):
    doctor: CatalogName
    service: CatalogName
    starts_at: AwareDatetime
    # Optional patient details. The phone is never a parameter: it comes from the call
    # session. Empty strings mean "not given".
    patient_name: PatientName = ""
    preferred_language: Annotated[str, BeforeValidator(_language)] = ""
    consent_booking_confirmation: ConsentFlag = None
    consent_reminder_call: ConsentFlag = None
    consent_reminder_message: ConsentFlag = None


class BolnaVerify(BolnaCallContext):
    booking_reference: SpokenReference


class BolnaCancel(BolnaCallContext):
    booking_reference: SpokenReference
    confirmed: ReadBack = False


class BolnaReschedule(BolnaCallContext):
    booking_reference: SpokenReference
    starts_at: AwareDatetime
    confirmed: ReadBack = False


class BolnaStatusQuery(BolnaCallContext):
    appointment_id: UUID


def _opt_out_scope(value):
    # Rendered template value; empty means everything.
    if isinstance(value, str):
        value = value.strip().casefold() or "all"
    return value


class BolnaOptOut(BolnaCallContext):
    """Stop reminders and confirmations of one booking verified on this call, on the given
    channels ("all", "calls" or "messages"). Never covers the patient's other bookings.
    confirmed=false only reads the change back; call again with confirmed=true."""

    booking_reference: SpokenReference
    scope: Annotated[Literal["all", "calls", "messages"], BeforeValidator(_opt_out_scope)] = "all"
    confirmed: ReadBack = False


class BolnaSlotsOut(StrictModel):
    slots: list[AwareDatetime]
    spoken: str


class BolnaReservationOut(StrictModel):
    appointment_id: UUID
    booking_reference: str
    status: str
    starts_at: AwareDatetime
    spoken: str


class BolnaStatusOut(StrictModel):
    appointment_id: UUID
    status: str
    spoken: str


class BolnaVerifyOut(StrictModel):
    """A verified booking: no patient data, only what the caller needs read back."""

    booking_reference: str
    status: str
    starts_at: AwareDatetime
    doctor: str
    service: str
    spoken: str


class BolnaChangeOut(StrictModel):
    """changed=false: nothing was changed (read back and call again with confirmed=true).
    For a reschedule, booking_reference is the new booking's reference."""

    changed: bool
    booking_reference: str
    previous_booking_reference: str | None = None
    status: str
    starts_at: AwareDatetime
    spoken: str


class BolnaOptOutOut(StrictModel):
    """opted_out=false: nothing was changed (read back and call again with confirmed=true)."""

    opted_out: bool
    booking_reference: str
    scope: str
    spoken: str


class BolnaDoctorOut(StrictModel):
    name: str
    services: list[str]


class BolnaContextOut(StrictModel):
    """Pre-call variables. Clinic configuration only: no patient or booking data."""

    clinic_name: str
    timezone: str
    today: date
    doctors: list[BolnaDoctorOut]
    supported_languages: list[str]
    faq_text: str  # Always a placeholder: no clinic FAQ content exists yet.
    emergency_text: str
    handoff_text: str
    # True: emergency_text and handoff_text are the clinic's approved scripts. False: both
    # are placeholders.
    approved_text: bool


# Staff appointment views (routes_appointments.py)
AppointmentState = Literal["pending_sync", "confirmed", "needs_review", "cancelled"]


class StaffAppointmentOut(StrictModel):
    """Minimal fields. patient_name and phone_last2 are filled for owner/admin only (null for
    viewer, and when the patient record is missing or cannot be decrypted)."""

    id: UUID
    booking_reference: str
    doctor_id: UUID
    service_id: UUID
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    status: str
    rescheduled_from: UUID | None
    created_at: AwareDatetime
    patient_name: str | None
    phone_last2: str | None


class AppointmentResolve(StrictModel):
    action: Literal["requeue", "cancel"]


class StaffOptOut(StrictModel):
    """Opt the appointment's patient out; 'all' covers every purpose or channel."""

    purpose: Literal["all", "booking_confirmation", "reminder"] = "all"
    channel: Literal["all", "call", "message"] = "all"


class StaffOptOutOut(StrictModel):
    purpose: str
    channel: str
    suppressed: int  # Unsent contacts suppressed by this request.


class StaffOptOutRemoved(StrictModel):
    purpose: str
    channel: str
    removed: int  # Opt-outs deleted; contacts already suppressed stay suppressed.


ReminderStatus = Literal[
    "scheduled", "sending", "sent", "unknown", "needs_review", "cancelled", "suppressed", "failed"
]


class ReminderOut(StrictModel):
    """Operational view of a planned contact: ids, times and codes only. No patient data,
    message text or provider message id."""

    id: UUID
    appointment_id: UUID
    appointment_version: int
    channel: str
    purpose: str
    scheduled_for: AwareDatetime
    status: str
    attempts: int
    last_error: str | None
    provider_execution_id: str | None
    stop_outcome: str | None
    sent_at: AwareDatetime | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


# Bolna execution webhooks (routes_webhooks.py). The request body is read and minimized by
# the route itself (providers.bolna.minimize_execution), not modelled here: unknown fields
# are expected and dropped rather than rejected.
class WebhookReceipt(StrictModel):
    """accepted: stored with a processing job. duplicate: this event was already stored."""

    status: Literal["accepted", "duplicate"]


# Clinic provisioning (T10, routes_admin.py)
ProvisioningState = Literal[
    "draft",
    "calendar_connected",
    "agent_created",
    "number_assigned",
    "verified",
    "active",
    "failed",
    "manual_review",
]
# A Bolna phone number id from GET /phone-numbers/all (never the number itself).
PhoneNumberId = Annotated[str, Field(pattern=r"^[A-Za-z0-9-]{1,64}$")]


class ProvisioningEventOut(StrictModel):
    from_state: str
    to_state: str
    step: str
    outcome: str
    actor_id: UUID | None
    created_at: AwareDatetime


class ProvisioningAttempts(StrictModel):
    calendar: int
    agent: int
    number: int
    verify: int


class ProvisioningOut(StrictModel):
    state: ProvisioningState
    resume_state: ProvisioningState  # Where a reset returns.
    agent_id: str | None
    agent_name: str | None  # Deterministic provider reference used for lookups.
    agent_create_pending: bool  # An agent create may have reached Bolna unrecorded.
    tool_credential_id: UUID | None  # The agent's tool credential (id only).
    phone_number_id: str | None
    attempts: ProvisioningAttempts
    last_error: str | None
    version: int  # 0 until provisioning has started.
    updated_at: AwareDatetime | None
    step_pending: bool
    events: list[ProvisioningEventOut]  # Newest first, at most 50.


class ProvisioningNumber(StrictModel):
    phone_number_id: PhoneNumberId


class ProvisioningReset(StrictModel):
    # The owner checked the Bolna account: no agent named agent_name exists. Clears an
    # ambiguous create intent; accepted only after create_uncertain or lease_lost.
    confirm_no_stray_agent: bool = False


class ProvisioningAdvanced(StrictModel):
    job_id: UUID
    state: ProvisioningState
    step: Literal["calendar", "agent", "number", "verify"]
