/**
 * Types for the API contract in docs/plans/2026-10-31-platform.md ("API contract") and the
 * existing staff/admin routes in docs/openapi.json. Fields the contract leaves open are
 * marked "assumed" and listed in the P3 report.
 */

export type Role = "owner" | "admin" | "viewer";

export interface ErrorEnvelope {
  error: { code: string; message: string };
}

/* ---------- Public ---------- */

export interface ClinicProfile {
  name: string;
  tagline: string | null;
  timezone: string;
  address: string | null;
  map_url: string | null;
  phone_display: string | null;
  email: string | null;
  hours_note: string | null;
  about: string | null;
  voice_number: string | null;
  chat_enabled: boolean;
}

export interface PublicService {
  id: string;
  name: string;
  description: string | null;
  duration_minutes: number;
  colour: string | null;
}

export interface PublicDoctor {
  id: string;
  name: string;
  public_bio: string | null;
  photo_url: string | null;
  colour: string | null;
  service_ids: string[];
}

export interface Slot {
  doctor_id: string;
  starts_at: string;
  ends_at: string;
}

export interface SlotsResponse {
  timezone: string;
  slots: Slot[];
}

export type BookingStatus = "pending_sync" | "confirmed" | "needs_review" | "cancelled";

export interface PublicBookingRequest {
  service_id: string;
  doctor_id: string;
  starts_at: string;
  name: string;
  phone: string;
  consent_contact: boolean;
  notes?: string;
}

export interface BookingResult {
  reference: string;
  status: BookingStatus;
  starts_at: string;
  doctor_name: string;
  service_name: string;
}

export interface CallbackRequestBody {
  name: string;
  phone: string;
  reason?: string;
}

export interface CallbackCreated {
  id: string;
  status: CallbackStatus;
}

/* ---------- Chat ---------- */

export interface ChatSessionCreated {
  session_id: string;
  greeting: string;
  quick_replies: string[];
}

export interface ChatProposal {
  id: string;
  doctor_name: string;
  service_name: string;
  starts_at: string;
  expires_at: string;
}

export interface ChatMessage {
  role: "user" | "assistant";
  text: string;
}

export interface ChatReply {
  messages: ChatMessage[];
  proposal?: ChatProposal | null;
  quick_replies: string[];
}

/** GET /v1/chat/sessions/{id}: the whole transcript so far, for a reload. */
export type ChatTranscript = ChatReply;

export interface ProposalConfirmBody {
  name: string;
  phone: string;
  consent_contact: boolean;
}

/* ---------- Staff ---------- */

export interface StaffUser {
  id: string;
  email: string;
  role: Role;
}

export interface LoginResult {
  user: StaffUser;
  expires_at: string;
}

export type Channel = "voice" | "web" | "chat" | "staff";

export interface CalendarDoctor {
  id: string;
  name: string;
  colour: string | null;
}

export interface CalendarAppointment {
  id: string;
  doctor_id: string;
  service_id: string;
  service_name: string;
  colour: string | null;
  starts_at: string;
  ends_at: string;
  status: BookingStatus;
  /** Assumed values: "pending" | "synced" | "failed" | "not_applicable" (open string). */
  sync_state: string;
  channel: Channel;
  patient_name?: string | null;
  reference?: string | null;
}

/** Assumed to match `ExceptionOut` of /v1/admin/exceptions. */
export interface ScheduleException {
  id: string;
  doctor_id: string | null;
  kind: "holiday" | "leave" | "break";
  starts_at: string;
  ends_at: string;
  label: string;
}

export interface CalendarResponse {
  timezone: string;
  doctors: CalendarDoctor[];
  appointments: CalendarAppointment[];
  exceptions: ScheduleException[];
}

export interface StaffBookingRequest {
  service_id: string;
  doctor_id: string;
  starts_at: string;
  name: string;
  phone: string;
  notes?: string;
}

export type CallbackStatus = "open" | "calling" | "done" | "dismissed";

export interface CallbackItem {
  id: string;
  name: string;
  phone_masked: string;
  reason: string | null;
  source: "web" | "chat" | "voice";
  status: CallbackStatus;
  created_at: string;
}

export interface CallStarted {
  status: "calling";
  execution_id: string;
}

export interface ConversationSummary {
  id: string;
  channel: "chat" | "voice";
  started_at: string;
  status: string;
  turns: number;
  patient_name?: string | null;
  outcome: string | null;
}

/** Assumed shape of GET /v1/staff/conversations/{id}. */
export interface ConversationDetail extends ConversationSummary {
  messages?: { role: "user" | "assistant" | "tool"; text: string; created_at?: string }[];
  summary?: string | null;
}

export interface StaffStats {
  today: number;
  week: number;
  needs_review: number;
  open_callbacks: number;
  by_channel: Record<Channel, number>;
}

/** GET /v1/admin/appointments item (StaffAppointmentOut). */
export interface StaffAppointment {
  id: string;
  booking_reference: string;
  doctor_id: string;
  service_id: string;
  starts_at: string;
  ends_at: string;
  status: BookingStatus;
  rescheduled_from: string | null;
  created_at: string;
  patient_name: string | null;
  phone_last2: string | null;
}

/** GET /v1/admin/clinic (ClinicConfig). */
export interface ClinicConfig {
  id: string;
  name: string;
  timezone: string;
  lead_time_minutes: number;
  horizon_days: number;
  outbound_enabled: boolean;
  quiet_hours_start: string;
  quiet_hours_end: string;
  reminder_lead_minutes: number;
  daily_contact_budget: number;
  emergency_text: string;
  handoff_text: string;
  scripts_approved: boolean;
  test_calls_enabled: boolean;
  include_reference_in_messages: boolean;
  /*
   * Public profile fields (the GET /v1/public/clinics/{slug} shape), assumed to be read and
   * written through GET/PATCH /v1/admin/clinic. Optional until the backend adds them.
   */
  tagline?: string | null;
  address?: string | null;
  map_url?: string | null;
  phone_display?: string | null;
  email?: string | null;
  hours_note?: string | null;
  about?: string | null;
  voice_number?: string | null;
  chat_enabled?: boolean;
}

/** GET /v1/staff/outbound-status. */
export interface OutboundStatus {
  enabled: boolean;
  has_from_number: boolean;
  has_outbound_agent: boolean;
}

export interface AdminDoctor {
  id: string;
  name: string;
  active: boolean;
  booking_calendar_id: string | null;
  busy_calendar_ids: string[];
  calendar_account_id: string | null;
}

export interface AdminService {
  id: string;
  doctor_id: string;
  name: string;
  duration_minutes: number;
  buffer_after_minutes: number;
  active: boolean;
}

export interface HoursWindow {
  weekday: number;
  opens: string;
  closes: string;
}

export interface ExceptionCreated extends ScheduleException {
  conflicting_appointment_ids: string[];
  conflict_count: number;
}
