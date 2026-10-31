import "server-only";

import type {
  AdminDoctor,
  AdminService,
  BookingResult,
  BookingStatus,
  SyncState,
  CalendarAppointment,
  CallbackItem,
  Channel,
  ClinicConfig,
  ConversationDetail,
  HoursWindow,
  Role,
  ScheduleException,
  Slot,
  StaffUser,
} from "@/lib/api/types";
import type { UpstreamRequest, UpstreamResult } from "../upstream";
import {
  CALLBACK_REASONS,
  CLINIC_ID,
  DOCTORS,
  IST_OFFSET_MINUTES,
  MOCK_PASSWORD,
  MOCK_TZ,
  MOCK_USERS,
  PATIENT_NAMES,
  PROFILE,
  SERVICES,
} from "./fixtures";

/**
 * An in-memory imitation of the API for development without a backend. It follows the
 * contract's shapes, status codes and error envelopes closely enough to exercise every
 * screen, including conflicts, expired proposals and the outbound-disabled refusal. It never
 * contacts anyone. State lives on globalThis so it survives hot reloads, and resets on a
 * server restart.
 */

const MINUTE = 60_000;
const DAY = 86_400_000;
const OFFSET = IST_OFFSET_MINUTES * MINUTE;

/* ---------- time helpers (Asia/Kolkata, fixed offset) ---------- */

export function istDate(ms: number): string {
  return new Date(ms + OFFSET).toISOString().slice(0, 10);
}
function dayStart(date: string): number {
  const [y, m, d] = date.split("-").map(Number) as [number, number, number];
  return Date.UTC(y, m - 1, d) - OFFSET;
}
function addDays(date: string, days: number): string {
  return istDate(dayStart(date) + days * DAY + 12 * 60 * MINUTE);
}
function weekday(date: string): number {
  return (new Date(`${date}T00:00:00Z`).getUTCDay() + 6) % 7;
}
function atLocal(date: string, hhmm: string): number {
  const [h, m] = hhmm.split(":").map(Number) as [number, number];
  return dayStart(date) + (h * 60 + m) * MINUTE;
}
export function isoIst(ms: number): string {
  return `${new Date(ms + OFFSET).toISOString().slice(0, 19)}+05:30`;
}

/* ---------- state ---------- */

interface Appt {
  id: string;
  doctor_id: string;
  service_id: string;
  starts: number;
  ends: number;
  blocks: number;
  status: BookingStatus;
  channel: Channel;
  patient_name: string;
  phone: string;
  reference: string;
  created: number;
  version: number;
}

interface Callback extends CallbackItem {
  phone: string;
}

interface Proposal {
  id: string;
  doctor_id: string;
  service_id: string;
  starts: number;
  expires: number;
  consumed: boolean;
}

interface ChatSession {
  id: string;
  created: number;
  proposals: Map<string, Proposal>;
  lastProposal: Proposal | null;
  messages: { role: "user" | "assistant" | "system"; text: string; created_at: string }[];
  /** The quick replies last offered, for restoring the conversation after a reload. */
  quick: string[];
  turns: number;
}

interface State {
  seed: string;
  appts: Appt[];
  callbacks: Callback[];
  sessions: Map<string, StaffUser>;
  chats: Map<string, ChatSession>;
  conversations: ConversationDetail[];
  exceptions: ScheduleException[];
  clinic: ClinicConfig;
  doctors: AdminDoctor[];
  services: AdminService[];
  hours: Map<string, HoursWindow[]>;
  counter: number;
}

const globalStore = globalThis as unknown as { __asterMockState?: State };

function rng(seed: string): () => number {
  let h = 1779033703 ^ seed.length;
  for (let i = 0; i < seed.length; i++) {
    h = Math.imul(h ^ seed.charCodeAt(i), 3432918353);
    h = (h << 13) | (h >>> 19);
  }
  let a = h >>> 0;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
function reference(random: () => number): string {
  let out = "";
  for (let i = 0; i < 8; i++) out += CROCKFORD[Math.floor(random() * 32)];
  return out;
}

function uuid(): string {
  return crypto.randomUUID();
}

function hex(bytes: number): string {
  const array = crypto.getRandomValues(new Uint8Array(bytes));
  return [...array].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function hoursFor(doctorId: string, date: string, state: State): [number, number][] {
  const windows = state.hours.get(doctorId) ?? [];
  const wd = weekday(date);
  return windows
    .filter((w) => w.weekday === wd)
    .map((w) => [atLocal(date, w.opens), atLocal(date, w.closes)] as [number, number]);
}

const ACTIVE: ReadonlySet<BookingStatus> = new Set(["pending_sync", "confirmed", "needs_review"]);

function seedState(): State {
  const today = istDate(Date.now());
  const random = rng(`aster:${today}`);
  const hours = new Map<string, HoursWindow[]>();
  for (const doctor of DOCTORS) {
    const list: HoursWindow[] = [];
    for (const [wd, windows] of Object.entries(doctor.hours)) {
      for (const [opens, closes] of windows) list.push({ weekday: Number(wd), opens, closes });
    }
    hours.set(doctor.id, list);
  }

  const exceptions: ScheduleException[] = [
    {
      id: "e0000000-0000-4000-8000-000000000001",
      doctor_id: null,
      kind: "holiday",
      starts_at: isoIst(dayStart("2026-11-08")),
      ends_at: isoIst(dayStart("2026-11-09")),
      label: "Diwali",
    },
    {
      id: "e0000000-0000-4000-8000-000000000002",
      doctor_id: DOCTORS[2]!.id,
      kind: "leave",
      starts_at: isoIst(dayStart(addDays(today, 3))),
      ends_at: isoIst(dayStart(addDays(today, 4))),
      label: "Conference",
    },
    {
      id: "e0000000-0000-4000-8000-000000000003",
      doctor_id: null,
      kind: "break",
      starts_at: isoIst(atLocal(addDays(today, 1), "13:30")),
      ends_at: isoIst(atLocal(addDays(today, 1), "14:30")),
      label: "Team meeting",
    },
  ];

  const state: State = {
    seed: today,
    appts: [],
    callbacks: [],
    sessions: new Map(),
    chats: new Map(),
    conversations: [],
    exceptions,
    clinic: {
      id: CLINIC_ID,
      name: PROFILE.name,
      timezone: MOCK_TZ,
      lead_time_minutes: 60,
      horizon_days: 90,
      outbound_enabled: false,
      quiet_hours_start: "21:00",
      quiet_hours_end: "09:00",
      reminder_lead_minutes: 1440,
      daily_contact_budget: 200,
      emergency_text:
        "If you have swelling that is spreading, trouble breathing or swallowing, or bleeding that will not stop, go to the nearest hospital emergency department or call 112 now.",
      handoff_text: "I'll ask a member of the front desk team to call you back during clinic hours.",
      scripts_approved: true,
      test_calls_enabled: false,
      include_reference_in_messages: false,
      public_slug: "aster-dental",
      public_enabled: true,
      public_tagline: PROFILE.tagline,
      public_address: PROFILE.address,
      public_map_url: PROFILE.map_url,
      public_phone_display: PROFILE.phone_display,
      public_email: PROFILE.email,
      public_hours_note: PROFILE.hours_note,
      public_about: PROFILE.about,
      public_voice_number: PROFILE.voice_number,
      public_chat_enabled: PROFILE.chat_enabled,
      bolna_outbound_from_number: "+919000000199",
    },
    doctors: DOCTORS.map((d) => ({
      id: d.id,
      name: d.name,
      active: true,
      booking_calendar_id: `ledger:${d.id}`,
      busy_calendar_ids: [],
      calendar_account_id: null,
    })),
    services: DOCTORS.flatMap((d) =>
      d.service_ids.map((sid) => {
        const s = SERVICES.find((x) => x.id === sid)!;
        return {
          id: `${sid.slice(0, 24)}${d.id.slice(-12)}`,
          doctor_id: d.id,
          name: s.name,
          duration_minutes: s.duration_minutes,
          buffer_after_minutes: s.buffer,
          active: true,
        };
      }),
    ),
    hours,
    counter: 0,
  };

  const channels: Channel[] = ["voice", "voice", "web", "web", "chat", "staff"];
  let nameIndex = 0;
  for (let offset = -3; offset <= 7; offset++) {
    const date = addDays(today, offset);
    for (const doctor of DOCTORS) {
      if (isBlocked(state, doctor.id, dayStart(date), dayStart(date) + DAY)) continue;
      for (const [open, close] of hoursFor(doctor.id, date, state)) {
        let cursor = open;
        while (cursor < close) {
          const serviceId = doctor.service_ids[Math.floor(random() * doctor.service_ids.length)];
          const service = SERVICES.find((s) => s.id === serviceId)!;
          const end = cursor + service.duration_minutes * MINUTE;
          if (end > close) break;
          if (random() < 0.42) {
            const past = end < Date.now();
            let status: BookingStatus = "confirmed";
            const roll = random();
            if (roll < 0.07) status = "cancelled";
            else if (!past && roll < 0.17) status = "pending_sync";
            const name = PATIENT_NAMES[nameIndex++ % PATIENT_NAMES.length]!;
            state.appts.push({
              id: uuid(),
              doctor_id: doctor.id,
              service_id: service.id,
              starts: cursor,
              ends: end,
              blocks: end + service.buffer * MINUTE,
              status,
              channel: channels[Math.floor(random() * channels.length)]!,
              patient_name: name,
              phone: `+91900000${String(1000 + nameIndex).slice(-4)}`,
              reference: reference(random),
              created: cursor - (2 + Math.floor(random() * 10)) * DAY,
              version: 1,
            });
            cursor = end + service.buffer * MINUTE;
          } else {
            cursor += 30 * MINUTE;
          }
        }
      }
    }
  }

  // Two appointments the calendar write could not confirm, for the review queue.
  const future = state.appts.filter((a) => a.starts > Date.now() && a.status === "confirmed");
  for (const appt of future.slice(1, 3)) appt.status = "needs_review";

  // A cancelled booking whose time was rebooked: the two blocks overlap in the column.
  const rebooked = future[4];
  if (rebooked) {
    state.appts.push({
      ...rebooked,
      id: uuid(),
      status: "cancelled",
      starts: rebooked.starts - 15 * MINUTE,
      ends: rebooked.ends - 15 * MINUTE,
      blocks: rebooked.ends - 15 * MINUTE,
      patient_name: PATIENT_NAMES[(nameIndex + 3) % PATIENT_NAMES.length]!,
      reference: reference(random),
      channel: "voice",
    });
  }

  const now = Date.now();
  state.callbacks = CALLBACK_REASONS.map((reason, i) => ({
    id: uuid(),
    name: PATIENT_NAMES[(i * 5 + 2) % PATIENT_NAMES.length]!,
    phone: `+9190000${String(20000 + i).slice(-5)}`,
    phone_masked: `****${String(20020 + i).slice(-4)}`,
    reason,
    source: (["web", "chat", "voice", "web"] as const)[i]!,
    status: i === 3 ? "done" : "open",
    created_at: isoIst(now - (i * 95 + 20) * MINUTE),
    resolved_at: i === 3 ? isoIst(now - (i * 95) * MINUTE) : null,
    execution_id: null,
    call_requested_at: null,
    // Seeded demo data, like the API's: never dialled. One open request is a live-looking
    // record so the Call now flow can be tried (mock mode still dials nothing).
    synthetic: i !== 0,
  }));

  state.conversations = seedConversations(state, now);
  return state;
}

/** The staff conversation shapes of GET /v1/staff/conversations/{id}. */
function seedConversations(state: State, now: number): ConversationDetail[] {
  const at = (minutesAgo: number) => isoIst(now - minutesAgo * MINUTE);
  const say = (role: "user" | "assistant", text: string, minutesAgo: number) => ({ role, text, tool_name: null, created_at: at(minutesAgo) });
  const tool = (name: string, result: string, minutesAgo: number) => ({ role: "tool" as const, text: `${name}: ${result}`, tool_name: name, created_at: at(minutesAgo) });
  const voiceAppt = state.appts.find((a) => a.channel === "voice");
  const chatAppt = state.appts.find((a) => a.channel === "chat");
  const link = (a: Appt | undefined) =>
    a ? [{ id: a.id, reference: a.reference, status: a.status, starts_at: isoIst(a.starts), channel: a.channel }] : [];
  const chat = { provider_status: null, call_type: null, duration_seconds: null, hangup_by: null };
  return [
    {
      id: "c0000000-0000-4000-8000-000000000001",
      channel: "chat",
      started_at: at(50),
      last_active_at: at(44),
      ended_at: null,
      status: "active",
      turns: 2,
      patient_name: "Ananya Krishnan",
      outcome: "booked",
      ...chat,
      messages: [
        say("assistant", "Hello, I'm Aster Dental Studio's virtual assistant. How can I help?", 50),
        say("user", "Can I get a cleaning on Monday evening?", 49),
        tool("check_slots", "ok", 49),
        tool("propose_booking", "ok", 49),
        say("assistant", "I can offer Check-up and cleaning with Dr. Rohan Deshpande on Monday at 5:30 pm. Add your details and press Confirm to book it; nothing is booked until you do.", 48),
        { role: "system", text: "Booking requested from this chat.", tool_name: null, created_at: at(45) },
        say("user", "Thank you!", 44),
        say("assistant", "You're welcome. See you on Monday.", 44),
      ],
      appointments: link(chatAppt),
    },
    {
      id: "c0000000-0000-4000-8000-000000000002",
      channel: "voice",
      started_at: at(180),
      last_active_at: at(174),
      ended_at: at(174),
      status: "completed",
      turns: 0,
      patient_name: "Vikram Hegde",
      outcome: "booked",
      provider_status: "completed",
      call_type: "inbound",
      duration_seconds: 342,
      hangup_by: "caller",
      messages: [],
      appointments: link(voiceAppt),
    },
    {
      id: "c0000000-0000-4000-8000-000000000003",
      channel: "chat",
      started_at: at(26 * 60),
      last_active_at: at(26 * 60 - 3),
      ended_at: null,
      status: "active",
      turns: 2,
      patient_name: null,
      outcome: null,
      ...chat,
      messages: [
        say("user", "Do you take Star Health insurance?", 26 * 60),
        say("assistant", "I can't confirm insurance cover here. I can ask the front desk to call you back with an answer. Would you like that?", 26 * 60 - 1),
        say("user", "Yes please", 26 * 60 - 2),
        tool("request_callback", "ok", 26 * 60 - 3),
        say("assistant", "Done. The front desk will ring you during clinic hours.", 26 * 60 - 3),
      ],
      appointments: [],
    },
    {
      id: "c0000000-0000-4000-8000-000000000004",
      channel: "voice",
      started_at: at(28 * 60),
      last_active_at: at(28 * 60 - 2),
      ended_at: at(28 * 60 - 2),
      status: "completed",
      turns: 0,
      patient_name: null,
      outcome: null,
      provider_status: "completed",
      call_type: "inbound",
      duration_seconds: 96,
      hangup_by: "agent",
      messages: [],
      appointments: [],
    },
  ];
}

function isBlocked(state: State, doctorId: string, from: number, to: number): boolean {
  return state.exceptions.some((e) => {
    if (e.doctor_id !== null && e.doctor_id !== doctorId) return false;
    return Date.parse(e.starts_at) < to && Date.parse(e.ends_at) > from;
  });
}

function getState(): State {
  const today = istDate(Date.now());
  if (!globalStore.__asterMockState || globalStore.__asterMockState.seed !== today) {
    const previous = globalStore.__asterMockState;
    globalStore.__asterMockState = seedState();
    // Keep sign-ins across a midnight reseed.
    if (previous) globalStore.__asterMockState.sessions = previous.sessions;
  }
  return globalStore.__asterMockState;
}

/** Reset for tests. */
export function resetMockState(): void {
  delete globalStore.__asterMockState;
}

/* ---------- responses ---------- */

const ok = (body: unknown, status = 200): UpstreamResult => ({ status, body, retryAfter: null });
const fail = (status: number, code: string, message: string): UpstreamResult =>
  ok({ error: { code, message } }, status);
const detail = (status: number, text: unknown): UpstreamResult => ok({ detail: text }, status);

function parseRaw(body: string | null): unknown {
  if (!body) return null;
  try {
    return JSON.parse(body);
  } catch {
    return null;
  }
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

const E164 = /^\+[1-9]\d{7,14}$/;

function serviceById(id: unknown) {
  return SERVICES.find((s) => s.id === id);
}
function doctorById(id: unknown) {
  return DOCTORS.find((d) => d.id === id);
}

/* ---------- slots ---------- */

function computeSlots(
  state: State,
  serviceId: string,
  doctorId: string | null,
  fromDate: string,
  days: number,
): Slot[] {
  const service = serviceById(serviceId)!;
  const earliest = Date.now() + state.clinic.lead_time_minutes * MINUTE;
  const out: Slot[] = [];
  for (let i = 0; i < days; i++) {
    const date = addDays(fromDate, i);
    for (const doctor of DOCTORS) {
      if (doctorId && doctor.id !== doctorId) continue;
      if (!doctor.service_ids.includes(serviceId)) continue;
      if (!state.doctors.find((d) => d.id === doctor.id)?.active) continue;
      for (const [open, close] of hoursFor(doctor.id, date, state)) {
        for (let start = open; start + service.duration_minutes * MINUTE <= close; start += service.duration_minutes * MINUTE) {
          const end = start + service.duration_minutes * MINUTE;
          const hold = end + service.buffer * MINUTE;
          if (start < earliest) continue;
          if (isBlocked(state, doctor.id, start, hold)) continue;
          const clash = state.appts.some(
            (a) => a.doctor_id === doctor.id && ACTIVE.has(a.status) && a.starts < hold && a.blocks > start,
          );
          if (clash) continue;
          out.push({ doctor_id: doctor.id, starts_at: isoIst(start), ends_at: isoIst(end) });
        }
      }
    }
  }
  return out.sort((a, b) => Date.parse(a.starts_at) - Date.parse(b.starts_at));
}

function slotOpen(state: State, serviceId: string, doctorId: string, starts: number): boolean {
  return computeSlots(state, serviceId, doctorId, istDate(starts), 1).some(
    (s) => Date.parse(s.starts_at) === starts,
  );
}

function bookingResult(appt: Appt): BookingResult {
  return {
    reference: appt.reference,
    status: appt.status,
    starts_at: isoIst(appt.starts),
    doctor_name: doctorById(appt.doctor_id)!.name,
    service_name: serviceById(appt.service_id)!.name,
  };
}

function createAppt(
  state: State,
  input: { service_id: string; doctor_id: string; starts: number; name: string; phone: string; channel: Channel },
): Appt {
  const service = serviceById(input.service_id)!;
  const ends = input.starts + service.duration_minutes * MINUTE;
  const appt: Appt = {
    id: uuid(),
    doctor_id: input.doctor_id,
    service_id: input.service_id,
    starts: input.starts,
    ends,
    blocks: ends + service.buffer * MINUTE,
    // The ledger provider confirms inline, as decision 3 describes; staff bookings stay
    // pending until the (mock) worker tick so both states are visible.
    status: input.channel === "staff" ? "pending_sync" : "confirmed",
    channel: input.channel,
    patient_name: input.name,
    phone: input.phone,
    reference: reference(Math.random),
    created: Date.now(),
    version: 1,
  };
  state.appts.push(appt);
  return appt;
}

interface PatientFields {
  name: string;
  phone: string;
  consent: boolean;
}

function patientFields(body: Record<string, unknown>, requireConsent: boolean): PatientFields | UpstreamResult {
  const name = typeof body.name === "string" ? body.name.trim() : "";
  const phone = typeof body.phone === "string" ? body.phone.trim() : "";
  if (!name || name.length > 120) return fail(422, "invalid_name", "Enter the patient's name (up to 120 characters).");
  if (!E164.test(phone)) return fail(422, "invalid_phone", "Enter a valid mobile number.");
  if (requireConsent && typeof body.consent_contact !== "boolean") {
    return fail(422, "invalid_consent", "Say whether we may contact you about this booking.");
  }
  return { name, phone, consent: body.consent_contact === true };
}

/* ---------- public ---------- */

function publicRoutes(state: State, method: string, rest: string[], url: URLSearchParams, body: Record<string, unknown>): UpstreamResult {
  const path = rest.join("/");
  if (method === "GET" && path === "") {
    // The public profile follows what the owner saves in Settings > Clinic profile.
    const profile: Record<string, unknown> = { ...PROFILE, name: state.clinic.name, timezone: state.clinic.timezone };
    for (const [field, out] of Object.entries(PROFILE_FIELDS)) {
      const value = state.clinic[field as keyof typeof PROFILE_FIELDS];
      // An emptied text reads as absent on the website.
      profile[out] = value === "" || value === undefined ? (out === "chat_enabled" ? false : null) : value;
    }
    return ok(profile);
  }
  if (method === "GET" && path === "services") {
    return ok(SERVICES.map(({ buffer: _buffer, ...s }) => s));
  }
  if (method === "GET" && path === "doctors") {
    return ok(DOCTORS.filter((d) => state.doctors.find((x) => x.id === d.id)?.active).map(({ hours: _hours, ...d }) => d));
  }
  if (method === "GET" && path === "slots") {
    const serviceId = url.get("service_id") ?? "";
    const doctorId = url.get("doctor_id");
    const date = url.get("date") ?? istDate(Date.now());
    const days = Number(url.get("days") ?? "1");
    if (!serviceById(serviceId)) return fail(404, "not_found", "That service is not available.");
    if (doctorId && !doctorById(doctorId)) return fail(404, "not_found", "That doctor is not available.");
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !Number.isInteger(days) || days < 1 || days > 7) {
      return fail(422, "validation_error", "Choose a date and between 1 and 7 days.");
    }
    if (date < istDate(Date.now())) return ok({ timezone: MOCK_TZ, slots: [] });
    return ok({ timezone: MOCK_TZ, slots: computeSlots(state, serviceId, doctorId, date, days) });
  }
  if (method === "POST" && path === "bookings") {
    const service = serviceById(body.service_id);
    const doctor = doctorById(body.doctor_id);
    if (!service || !doctor || !doctor.service_ids.includes(service.id)) {
      return fail(404, "not_found", "That doctor or service is not available.");
    }
    const patient = patientFields(body, true);
    if ("status" in patient) return patient;
    const starts = typeof body.starts_at === "string" ? Date.parse(body.starts_at) : NaN;
    if (!Number.isFinite(starts)) return fail(422, "invalid_time", "Choose a time.");
    if (!slotOpen(state, service.id, doctor.id, starts)) {
      return fail(409, "slot_unavailable", "That time has just been taken. Please choose another.");
    }
    const appt = createAppt(state, { service_id: service.id, doctor_id: doctor.id, starts, name: patient.name, phone: patient.phone, channel: "web" });
    return ok(bookingResult(appt), 202);
  }
  if (method === "POST" && path === "bookings/lookup") {
    const ref = typeof body.reference === "string" ? body.reference.toUpperCase().replace(/[\s-]/g, "").replace(/O/g, "0").replace(/[IL]/g, "1") : "";
    const phone = typeof body.phone === "string" ? body.phone : "";
    const appt = state.appts.find((a) => a.reference === ref && a.phone === phone);
    if (!appt) {
      return fail(404, "not_found", "We couldn't find a booking with that reference and phone number.");
    }
    return ok(bookingResult(appt));
  }
  if (method === "POST" && path === "callbacks") {
    const name = typeof body.name === "string" ? body.name.trim() : "";
    const phone = typeof body.phone === "string" ? body.phone.trim() : "";
    const reason = typeof body.reason === "string" ? body.reason.trim() : "";
    if (!name || name.length > 120) return fail(422, "invalid_name", "Enter your name.");
    if (!E164.test(phone)) return fail(422, "invalid_phone", "Enter a valid mobile number.");
    if (reason.length > 500) return fail(422, "invalid_reason", "Keep the reason under 500 characters.");
    const item: Callback = {
      id: uuid(),
      name,
      phone,
      phone_masked: `****${phone.slice(-4)}`,
      reason: reason || null,
      source: "web",
      status: "open",
      created_at: isoIst(Date.now()),
      resolved_at: null,
      execution_id: null,
      call_requested_at: null,
      synthetic: false,
    };
    state.callbacks.unshift(item);
    return ok({ id: item.id, status: item.status }, 201);
  }
  return fail(404, "not_found", "Not found.");
}

/* ---------- chat ---------- */

const GREETING =
  "Hello, I'm Aster Dental Studio's virtual assistant. I can find you an appointment, tell you about our services and hours, or ask the front desk to call you back. I can't give medical advice.";
const QUICK = ["Book a check-up", "Opening hours", "I have tooth pain", "Ask for a call back"];

const SERVICE_WORDS: [RegExp, number][] = [
  [/clean|check ?-?up|scal/i, 1],
  [/whiten/i, 2],
  [/fill|cavity/i, 3],
  [/root ?canal|rct/i, 4],
  [/extract|pull|wisdom/i, 5],
  [/implant/i, 6],
  [/align|brace|invisalign|crooked/i, 7],
  [/child|kid|son|daughter/i, 8],
  [/crown|cap/i, 10],
];

function formatSlot(ms: number): string {
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: MOCK_TZ,
    weekday: "long",
    day: "numeric",
    month: "long",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(ms));
}

function propose(state: State, session: ChatSession, serviceIndex: number, after: number): UpstreamResult {
  const service = SERVICES[serviceIndex - 1]!;
  const from = istDate(Math.max(after, Date.now()));
  const slots = computeSlots(state, service.id, null, from, 7).filter((s) => Date.parse(s.starts_at) > after);
  const slot = slots[0];
  if (!slot) {
    return ok({
      messages: [{ role: "assistant", text: `I couldn't find an opening for ${service.name} in the next week. I can ask the front desk to call you with more options.` }],
      proposal: null,
      quick_replies: ["Ask for a call back", "Opening hours"],
    });
  }
  const proposal: Proposal = {
    id: hex(16),
    doctor_id: slot.doctor_id,
    service_id: service.id,
    starts: Date.parse(slot.starts_at),
    expires: Date.now() + 10 * MINUTE,
    consumed: false,
  };
  session.proposals.set(proposal.id, proposal);
  session.lastProposal = proposal;
  const doctor = doctorById(slot.doctor_id)!;
  return ok({
    messages: [
      {
        role: "assistant",
        text: `The next opening for ${service.name} is with ${doctor.name} on ${formatSlot(proposal.starts)}. Add your details below and press Confirm to book it. Nothing is booked until you do.`,
      },
    ],
    proposal: {
      id: proposal.id,
      doctor_name: doctor.name,
      service_name: service.name,
      starts_at: isoIst(proposal.starts),
      expires_at: isoIst(proposal.expires),
    },
    quick_replies: ["Show a later time", "A different treatment"],
  });
}

function chatReply(state: State, session: ChatSession, text: string): UpstreamResult {
  const say = (reply: string, quick = QUICK) =>
    ok({ messages: [{ role: "assistant", text: reply }], proposal: null, quick_replies: quick });

  if (/swell|bleed|breath|swallow|severe|unbearable|pain|ache|hurt|broke|broken/i.test(text)) {
    const emergency = state.clinic.emergency_text;
    const result = propose(state, session, 9, Date.now());
    const body = result.body as { messages: { role: string; text: string }[] };
    body.messages.unshift({ role: "assistant", text: emergency });
    return result;
  }
  if (/later|another time|different time|other time|next/i.test(text) && session.lastProposal) {
    const index = SERVICES.findIndex((s) => s.id === session.lastProposal!.service_id) + 1;
    return propose(state, session, index, session.lastProposal.starts);
  }
  if (/different treatment|other treatment|services|what do you/i.test(text)) {
    return say(
      `We offer ${SERVICES.map((s) => s.name.toLowerCase()).join(", ")}. Which one would you like?`,
      ["Check-up and cleaning", "Tooth-coloured filling", "Clear aligner consultation", "Children's check-up"],
    );
  }
  if (/hour|open|timing|close|sunday|when are/i.test(text)) return say(PROFILE.hours_note!);
  if (/where|address|location|direction|park/i.test(text)) {
    return say(`We're at ${PROFILE.address}. The entrance is beside the pharmacy, and there is paid parking on 12th Main.`);
  }
  if (/cost|price|fee|charge|insurance|how much/i.test(text)) {
    return say(
      "Costs depend on what the dentist finds, so we give you a written estimate before any treatment starts. For insurance questions the front desk can call you back.",
      ["Book a check-up", "Ask for a call back"],
    );
  }
  if (/call ?back|call me|ring me|speak to|human|person/i.test(text)) {
    return say(
      "I can't place calls myself, but the front desk can. Leave your name and number on the call-back page and they will ring you during clinic hours.",
      ["Book a check-up", "Opening hours"],
    );
  }
  for (const [pattern, index] of SERVICE_WORDS) {
    if (pattern.test(text)) return propose(state, session, index, Date.now());
  }
  if (/book|appoint|slot|available|visit/i.test(text)) return propose(state, session, 1, Date.now());
  return say("I can help you book an appointment, check our hours and location, or arrange a call back. What would you like to do?");
}

function publicProposal(proposal: Proposal) {
  const doctor = doctorById(proposal.doctor_id)!;
  const service = serviceById(proposal.service_id)!;
  return {
    id: proposal.id,
    doctor_name: doctor.name,
    service_name: service.name,
    starts_at: isoIst(proposal.starts),
    expires_at: isoIst(proposal.expires),
  };
}

function chatRoutes(state: State, method: string, rest: string[], body: Record<string, unknown>): UpstreamResult {
  if (method === "POST" && rest[0] === "clinics" && rest[2] === "sessions" && rest.length === 3) {
    const session: ChatSession = {
      id: hex(16),
      created: Date.now(),
      proposals: new Map(),
      lastProposal: null,
      messages: [{ role: "assistant", text: GREETING, created_at: isoIst(Date.now()) }],
      quick: QUICK,
      turns: 0,
    };
    state.chats.set(session.id, session);
    return ok({ session_id: session.id, greeting: GREETING, quick_replies: QUICK }, 201);
  }
  if (rest[0] !== "sessions" || !rest[1]) return fail(404, "not_found", "Not found.");
  const session = state.chats.get(rest[1]);
  if (!session) return fail(404, "session_not_found", "This conversation has ended. Start a new one.");
  if (method === "GET" && rest.length === 2) {
    // The transcript for a reload: messages in order, the open proposal (if still bookable)
    // and the quick replies last offered.
    const open = session.lastProposal;
    const live = open && !open.consumed && open.expires > Date.now() ? publicProposal(open) : null;
    return ok({
      messages: session.messages.filter((m) => m.role !== "system").map(({ role, text }) => ({ role, text })),
      proposal: live,
      quick_replies: session.quick,
    });
  }
  if (method !== "POST") return fail(405, "method_not_allowed", "Method not allowed.");
  if (rest[2] === "messages" && rest.length === 3) {
    const text = typeof body.text === "string" ? body.text.trim() : "";
    if (!text || text.length > 1000) return fail(422, "invalid_text", "Messages can be up to 1000 characters.");
    if (session.turns >= 40) return fail(409, "conversation_limit", "This conversation is full; please start a new chat or call the clinic.");
    session.turns += 1;
    session.messages.push({ role: "user", text, created_at: isoIst(Date.now()) });
    const reply = chatReply(state, session, text);
    const replyBody = reply.body as { messages: { role: "assistant"; text: string }[]; quick_replies: string[] };
    for (const m of replyBody.messages) {
      session.messages.push({ ...m, created_at: isoIst(Date.now()) });
    }
    session.quick = replyBody.quick_replies;
    return reply;
  }
  if (rest[2] === "proposals" && rest[3] && rest[4] === "confirm" && rest.length === 5) {
    const proposal = session.proposals.get(rest[3]);
    if (!proposal) return fail(404, "not_found", "That suggestion is no longer available.");
    if (proposal.consumed) return fail(409, "proposal_used", "This time has already been booked from this conversation.");
    if (proposal.expires < Date.now()) return fail(410, "proposal_expired", "This offer has expired; please ask again.");
    if (body.consent_contact !== true) return fail(422, "consent_required", "Consent to be contacted is required to book.");
    const patient = patientFields(body, true);
    if ("status" in patient) return patient;
    if (!slotOpen(state, proposal.service_id, proposal.doctor_id, proposal.starts)) {
      return fail(409, "slot_unavailable", "That time has just been taken. Ask me for another.");
    }
    proposal.consumed = true;
    const appt = createAppt(state, {
      service_id: proposal.service_id,
      doctor_id: proposal.doctor_id,
      starts: proposal.starts,
      name: patient.name,
      phone: patient.phone,
      channel: "chat",
    });
    const result = bookingResult(appt);
    session.messages.push({
      // Like the API: a system note, not shown to the patient (GET leaves it out), and the
      // booked proposal is no longer offered.
      role: "system",
      text: `Booking requested from this chat: reference ${result.reference}, status ${result.status}.`,
      created_at: isoIst(Date.now()),
    });
    session.quick = QUICK;
    return ok(result, 202);
  }
  return fail(404, "not_found", "Not found.");
}

/* ---------- staff and admin ---------- */

const canWrite = (user: StaffUser) => user.role === "owner" || user.role === "admin";
const forbidden = () => detail(403, "Insufficient role");

function calendarAppointment(appt: Appt, role: Role): CalendarAppointment {
  const service = serviceById(appt.service_id)!;
  const sync: Record<BookingStatus, SyncState> = {
    confirmed: "synced",
    pending_sync: "pending",
    needs_review: "failed",
    cancelled: "not_applicable",
  };
  return {
    id: appt.id,
    doctor_id: appt.doctor_id,
    service_id: appt.service_id,
    service_name: service.name,
    colour: service.colour,
    starts_at: isoIst(appt.starts),
    ends_at: isoIst(appt.ends),
    status: appt.status,
    sync_state: sync[appt.status],
    channel: appt.channel,
    patient_name: role === "viewer" ? null : appt.patient_name,
    reference: appt.reference,
  };
}

function staffAppointment(appt: Appt, role: Role) {
  return {
    id: appt.id,
    booking_reference: appt.reference,
    doctor_id: appt.doctor_id,
    service_id: appt.service_id,
    starts_at: isoIst(appt.starts),
    ends_at: isoIst(appt.ends),
    status: appt.status,
    rescheduled_from: null,
    created_at: isoIst(appt.created),
    patient_name: role === "viewer" ? null : appt.patient_name,
    phone_last2: role === "viewer" ? null : appt.phone.slice(-2),
  };
}

function range(url: URLSearchParams): { from: number; to: number } | null {
  const from = Date.parse(url.get("from") ?? "");
  const to = Date.parse(url.get("to") ?? "");
  if (!Number.isFinite(from) || !Number.isFinite(to) || to <= from) return null;
  return { from, to };
}

function staffRoutes(state: State, user: StaffUser, method: string, rest: string[], url: URLSearchParams, body: Record<string, unknown>): UpstreamResult {
  const [first, id, action] = rest;
  if (first === "me" && rest.length === 1 && method === "GET") {
    return ok({ ...user, clinic: { id: state.clinic.id, name: state.clinic.name, timezone: state.clinic.timezone, public_slug: state.clinic.public_slug ?? null } });
  }
  if (first === "calendar" && method === "GET") {
    const r = range(url);
    if (!r || r.to - r.from > 31 * DAY) return fail(422, "invalid_range", "Choose a range of up to 31 days.");
    return ok({
      timezone: MOCK_TZ,
      doctors: DOCTORS.filter((d) => state.doctors.find((x) => x.id === d.id)?.active).map((d) => ({ id: d.id, name: d.name, colour: d.colour })),
      appointments: state.appts
        .filter((a) => a.starts < r.to && a.ends > r.from)
        .sort((a, b) => a.starts - b.starts)
        .map((a) => calendarAppointment(a, user.role)),
      exceptions: state.exceptions.filter((e) => Date.parse(e.starts_at) < r.to && Date.parse(e.ends_at) > r.from),
    });
  }
  if (first === "bookings" && rest.length === 1 && method === "POST") {
    if (!canWrite(user)) return forbidden();
    const service = serviceById(body.service_id);
    const doctor = doctorById(body.doctor_id);
    if (!service || !doctor || !doctor.service_ids.includes(service.id)) {
      return fail(404, "not_found", "That doctor does not offer this service.");
    }
    const patient = patientFields(body, false);
    if ("status" in patient) return patient;
    const starts = typeof body.starts_at === "string" ? Date.parse(body.starts_at) : NaN;
    if (!Number.isFinite(starts)) return fail(422, "invalid_time", "Choose a time.");
    if (starts < Date.now()) return fail(422, "in_past", "That time has already passed.");
    const hold = starts + (service.duration_minutes + service.buffer) * MINUTE;
    const end = starts + service.duration_minutes * MINUTE;
    const inHours = hoursFor(doctor.id, istDate(starts), state).some(([o, c]) => starts >= o && end <= c);
    if (!inHours) return fail(409, "outside_hours", `${doctor.name} is not working at that time.`);
    if (isBlocked(state, doctor.id, starts, hold)) return fail(409, "exception", "That time is blocked by a holiday, leave or break.");
    if (state.appts.some((a) => a.doctor_id === doctor.id && ACTIVE.has(a.status) && a.starts < hold && a.blocks > starts)) {
      return fail(409, "slot_unavailable", "That time overlaps another appointment.");
    }
    const appt = createAppt(state, { service_id: service.id, doctor_id: doctor.id, starts, name: patient.name, phone: patient.phone, channel: "staff" });
    return ok(bookingResult(appt), 201);
  }
  if (first === "callbacks") {
    if (rest.length === 1 && method === "GET") {
      const status = url.get("status");
      const viewer = user.role === "viewer";
      return ok(
        state.callbacks
          .filter((c) => !status || c.status === status)
          .map(({ phone: _phone, ...c }) => (viewer ? { ...c, name: null, phone_masked: null, reason: null } : c)),
      );
    }
    const item = state.callbacks.find((c) => c.id === id);
    if (!item) return detail(404, "Callback request not found");
    if (action === "call" && method === "POST") {
      if (!canWrite(user)) return forbidden();
      // The API's order of checks.
      if (item.status === "done" || item.status === "dismissed") return fail(409, "callback_resolved", "This request is already resolved.");
      if (item.status === "calling") return ok({ status: "calling", execution_id: item.execution_id }, 202);
      const outbound = outboundStatus(state);
      if (!outbound.enabled) return fail(409, "outbound_disabled", "Outbound calling is turned off for this clinic.");
      if (!outbound.has_from_number) return fail(409, "no_from_number", "No outbound caller number is configured.");
      if (!outbound.has_outbound_agent) return fail(409, "no_outbound_agent", "No outbound agent is configured.");
      if (item.synthetic) return fail(409, "synthetic_contact", "Demo records are never dialled.");
      // Mock mode never dials anyone: the request is only marked as calling.
      item.status = "calling";
      item.execution_id = `mock-${hex(8)}`;
      item.call_requested_at = isoIst(Date.now());
      return ok({ status: "calling", execution_id: item.execution_id }, 202);
    }
    if (action === "resolve" && method === "POST") {
      if (!canWrite(user)) return forbidden();
      if (body.status !== "done" && body.status !== "dismissed") return detail(422, [{ loc: ["body", "status"], msg: "Input should be 'done' or 'dismissed'", type: "literal_error" }]);
      if (item.status === "done" || item.status === "dismissed") {
        if (item.status !== body.status) return fail(409, "callback_resolved", "This request is already resolved.");
      } else {
        item.status = body.status;
        item.resolved_at = isoIst(Date.now());
      }
      return ok({ id: item.id, status: item.status, resolved_at: item.resolved_at });
    }
  }
  if (first === "conversations" && method === "GET") {
    if (rest.length === 1) {
      const channel = url.get("channel");
      return ok(
        state.conversations
          .filter((c) => !channel || c.channel === channel)
          .map((c) => ({
            id: c.id,
            channel: c.channel,
            started_at: c.started_at,
            status: c.status,
            turns: c.turns,
            patient_name: user.role === "viewer" ? null : c.patient_name,
            outcome: c.outcome,
          })),
      );
    }
    const conversation = state.conversations.find((c) => c.id === id);
    if (!conversation) return detail(404, "Conversation not found");
    if (user.role !== "viewer") return ok(conversation);
    // Viewers see the structure but no text or names.
    return ok({ ...conversation, patient_name: null, messages: conversation.messages.map((m) => ({ ...m, text: null })) });
  }
  if (first === "outbound-status" && rest.length === 1 && method === "GET") {
    return ok(outboundStatus(state));
  }
  if (first === "stats" && method === "GET") {
    const today = istDate(Date.now());
    const weekEnd = dayStart(addDays(today, 7));
    const active = state.appts.filter((a) => a.status !== "cancelled");
    // Appointments created in the last 30 days, by channel (cancelled ones included).
    const byChannel: Record<Channel, number> = { voice: 0, web: 0, chat: 0, staff: 0 };
    for (const a of state.appts.filter((a) => a.created >= Date.now() - 30 * DAY)) byChannel[a.channel] += 1;
    return ok({
      today: active.filter((a) => istDate(a.starts) === today).length,
      week: active.filter((a) => a.starts >= dayStart(today) && a.starts < weekEnd).length,
      needs_review: state.appts.filter((a) => a.status === "needs_review").length,
      open_callbacks: state.callbacks.filter((c) => c.status === "open").length,
      by_channel: byChannel,
    });
  }
  return detail(404, "Not Found");
}

/**
 * Mock outbound readiness: the outbound agent is "configured"; the calling number follows the
 * owner's bolna_outbound_from_number. Nothing is ever dialled.
 */
const MOCK_HAS_OUTBOUND_AGENT = true;
function outboundStatus(state: State) {
  return {
    enabled: state.clinic.outbound_enabled,
    has_from_number: Boolean(state.clinic.bolna_outbound_from_number),
    has_outbound_agent: MOCK_HAS_OUTBOUND_AGENT,
  };
}

const OWNER_ONLY = new Set([
  "outbound_enabled",
  "bolna_outbound_from_number",
  "scripts_approved",
  "test_calls_enabled",
  "include_reference_in_messages",
]);
/** admin/clinic profile field → public profile field. */
const PROFILE_FIELDS = {
  public_tagline: "tagline",
  public_address: "address",
  public_map_url: "map_url",
  public_phone_display: "phone_display",
  public_email: "email",
  public_hours_note: "hours_note",
  public_about: "about",
  public_voice_number: "voice_number",
  public_chat_enabled: "chat_enabled",
} as const;
const PROFILE_LIMITS: Record<string, number> = {
  public_tagline: 160,
  public_address: 300,
  public_phone_display: 32,
  public_hours_note: 300,
  public_about: 2000,
};
const CLINIC_FIELDS = new Set<string>([
  ...Object.keys(PROFILE_FIELDS),
  "name",
  "timezone",
  "lead_time_minutes",
  "horizon_days",
  "quiet_hours_start",
  "quiet_hours_end",
  "reminder_lead_minutes",
  "daily_contact_budget",
  "emergency_text",
  "handoff_text",
  ...OWNER_ONLY,
]);

/** The API's PATCH rules for the profile and number fields: no nulls, https, E.164, lengths. */
function clinicFieldErrors(body: Record<string, unknown>) {
  const errors: { loc: string[]; msg: string; type: string }[] = [];
  for (const [key, value] of Object.entries(body)) {
    const bad = (msg: string) => errors.push({ loc: ["body", key], msg, type: "value_error" });
    if (value === null) bad("may not be null");
    else if (key === "public_chat_enabled" || key === "outbound_enabled") {
      if (typeof value !== "boolean") bad("must be a boolean");
    } else if (key in PROFILE_FIELDS || key === "bolna_outbound_from_number") {
      if (typeof value !== "string") bad("must be a string");
      else if (key === "public_map_url" && !/^https:\/\/\S+$/.test(value)) bad("must be an https URL");
      else if ((key === "public_voice_number" || key === "bolna_outbound_from_number") && !E164.test(value)) bad("must be an E.164 number");
      else if (key === "public_email" && value && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)) bad("must be an email address");
      else if (PROFILE_LIMITS[key] !== undefined && value.length > PROFILE_LIMITS[key]!) bad(`at most ${PROFILE_LIMITS[key]} characters`);
    }
  }
  return errors;
}

function adminRoutes(
  state: State,
  user: StaffUser,
  method: string,
  rest: string[],
  url: URLSearchParams,
  body: Record<string, unknown>,
  raw: unknown,
): UpstreamResult {
  const [first, id, action] = rest;
  if (first === "clinic" && rest.length === 1) {
    if (method === "GET") return ok(state.clinic);
    if (method === "PATCH") {
      if (!canWrite(user)) return forbidden();
      const keys = Object.keys(body);
      if (!keys.length) return detail(422, [{ loc: ["body"], msg: "empty", type: "value_error" }]);
      const unknown = keys.filter((k) => !CLINIC_FIELDS.has(k));
      if (unknown.length) return detail(422, unknown.map((k) => ({ loc: ["body", k], msg: "extra", type: "extra_forbidden" })));
      if (keys.some((k) => OWNER_ONLY.has(k)) && user.role !== "owner") return forbidden();
      const invalid = clinicFieldErrors(body);
      if (invalid.length) return detail(422, invalid);
      if (body.quiet_hours_start !== undefined && body.quiet_hours_start === (body.quiet_hours_end ?? state.clinic.quiet_hours_end)) {
        return detail(422, "Quiet hours start and end must differ");
      }
      const next = { ...state.clinic, ...(body as Partial<ClinicConfig>) };
      if (("emergency_text" in body || "handoff_text" in body) && body.scripts_approved !== true) next.scripts_approved = false;
      state.clinic = next;
      return ok(state.clinic);
    }
  }
  if (first === "doctors") {
    if (rest.length === 1 && method === "GET") return ok(state.doctors);
    const doctor = state.doctors.find((d) => d.id === id);
    if (!doctor) return detail(404, "Doctor not found");
    if (rest.length === 2 && method === "PATCH") {
      if (!canWrite(user)) return forbidden();
      if (typeof body.name === "string") doctor.name = body.name.trim() || doctor.name;
      if (typeof body.active === "boolean") doctor.active = body.active;
      return ok(doctor);
    }
    if (action === "hours") {
      if (method === "GET") return ok(state.hours.get(doctor.id) ?? []);
      if (method === "PUT") {
        if (!canWrite(user)) return forbidden();
        if (!Array.isArray(raw) || raw.length > 50) return detail(422, "Hours must be a list of at most 50 windows");
        const list = raw as HoursWindow[];
        const valid = list.every(
          (w) =>
            Number.isInteger(w?.weekday) &&
            w.weekday >= 0 &&
            w.weekday <= 6 &&
            /^\d{2}:\d{2}$/.test(String(w.opens)) &&
            /^\d{2}:\d{2}$/.test(String(w.closes)) &&
            w.opens < w.closes,
        );
        if (!valid) return detail(422, "Each window needs a weekday and an opening time before its closing time");
        state.hours.set(doctor.id, list.map(({ weekday: wd, opens, closes }) => ({ weekday: wd, opens, closes })));
        return ok(state.hours.get(doctor.id));
      }
    }
  }
  if (first === "services") {
    if (rest.length === 1 && method === "GET") return ok(state.services);
    const service = state.services.find((s) => s.id === id);
    if (!service) return detail(404, "Service not found");
    if (method === "PATCH") {
      if (!canWrite(user)) return forbidden();
      if (typeof body.active === "boolean") service.active = body.active;
      if (typeof body.duration_minutes === "number") service.duration_minutes = body.duration_minutes;
      if (typeof body.buffer_after_minutes === "number") service.buffer_after_minutes = body.buffer_after_minutes;
      if (typeof body.name === "string" && body.name.trim()) service.name = body.name.trim();
      return ok(service);
    }
  }
  if (first === "exceptions") {
    if (rest.length === 1 && method === "GET") {
      const r = range(url);
      return ok(
        state.exceptions
          .filter((e) => !r || (Date.parse(e.starts_at) < r.to && Date.parse(e.ends_at) > r.from))
          .sort((a, b) => Date.parse(a.starts_at) - Date.parse(b.starts_at)),
      );
    }
    if (rest.length === 1 && method === "POST") {
      if (!canWrite(user)) return forbidden();
      const starts = Date.parse(String(body.starts_at ?? ""));
      const ends = Date.parse(String(body.ends_at ?? ""));
      const kind = body.kind;
      if (kind !== "holiday" && kind !== "leave" && kind !== "break") return detail(422, "Choose holiday, leave or break");
      if (!Number.isFinite(starts) || !Number.isFinite(ends) || ends <= starts) return detail(422, "End must be after start");
      const doctorId = typeof body.doctor_id === "string" ? body.doctor_id : null;
      if (doctorId && !state.doctors.some((d) => d.id === doctorId)) return detail(404, "Doctor not found");
      const created: ScheduleException = {
        id: uuid(),
        doctor_id: doctorId,
        kind,
        starts_at: isoIst(starts),
        ends_at: isoIst(ends),
        label: typeof body.label === "string" ? body.label.trim().slice(0, 80) : "",
      };
      state.exceptions.push(created);
      const conflicts = state.appts.filter(
        (a) => ACTIVE.has(a.status) && (!doctorId || a.doctor_id === doctorId) && a.starts < ends && a.blocks > starts,
      );
      return ok({ ...created, conflicting_appointment_ids: conflicts.slice(0, 100).map((a) => a.id), conflict_count: conflicts.length }, 201);
    }
    if (rest.length === 2 && method === "DELETE") {
      if (!canWrite(user)) return forbidden();
      const before = state.exceptions.length;
      state.exceptions = state.exceptions.filter((e) => e.id !== id);
      return before === state.exceptions.length ? detail(404, "Exception not found") : ok(null, 204);
    }
  }
  if (first === "appointments") {
    if (rest.length === 1 && method === "GET") {
      const r = range(url);
      const status = url.get("status");
      const limit = Math.min(500, Math.max(1, Number(url.get("limit") ?? "100") || 100));
      return ok(
        state.appts
          .filter((a) => (!r || (a.starts >= r.from && a.starts < r.to)) && (!status || a.status === status))
          .sort((a, b) => a.starts - b.starts)
          .slice(0, limit)
          .map((a) => staffAppointment(a, user.role)),
      );
    }
    const appt = state.appts.find((a) => a.id === id);
    if (!appt) return detail(404, "Appointment not found");
    if (action === "cancel" && method === "POST") {
      if (!canWrite(user)) return forbidden();
      if (!ACTIVE.has(appt.status)) return detail(409, "Appointment is not active");
      appt.status = "cancelled";
      appt.version += 1;
      return ok(staffAppointment(appt, user.role));
    }
    if (action === "resolve" && method === "POST") {
      if (!canWrite(user)) return forbidden();
      if (appt.status !== "needs_review") return detail(409, "Appointment does not need review");
      if (body.action === "cancel") appt.status = "cancelled";
      else if (body.action === "requeue") appt.status = "pending_sync";
      else return detail(422, [{ loc: ["body", "action"], msg: "invalid", type: "literal_error" }]);
      appt.version += 1;
      return ok(staffAppointment(appt, user.role));
    }
  }
  return detail(404, "Not Found");
}

/* ---------- entry ---------- */

function bearer(token: string | null, state: State): StaffUser | null {
  return token ? (state.sessions.get(token) ?? null) : null;
}

export async function mockUpstream(request: UpstreamRequest): Promise<UpstreamResult> {
  if (!process.env.VITEST) await new Promise((resolve) => setTimeout(resolve, 120 + Math.random() * 180));
  const state = getState();
  const url = new URLSearchParams(request.search.replace(/^\?/, ""));
  const raw = parseRaw(request.body);
  const body = asRecord(raw);
  const parts = request.path.split("/").filter(Boolean); // ["v1", area, ...]
  const area = parts[1];
  const rest = parts.slice(2);

  if (area === "public") {
    if (rest[0] !== "clinics" || rest[1] !== "aster-dental") return fail(404, "not_found", "Clinic not found.");
    return publicRoutes(state, request.method, rest.slice(2), url, body);
  }
  if (area === "chat") return chatRoutes(state, request.method, rest, body);

  if (area === "staff" && rest[0] === "login" && request.method === "POST") {
    const email = typeof body.email === "string" ? body.email.toLowerCase() : "";
    const user = MOCK_USERS.find((u) => u.email === email);
    if (!user || body.password !== MOCK_PASSWORD) return detail(401, "Invalid credentials");
    const token = `cvs_mock.${hex(24)}`;
    state.sessions.set(token, { ...user });
    return ok({ token, expires_at: new Date(Date.now() + 12 * 60 * MINUTE).toISOString(), user });
  }
  const user = bearer(request.token, state);
  if (area === "staff" && rest[0] === "logout") {
    if (request.token) state.sessions.delete(request.token);
    return ok(null, 204);
  }
  if (!user) return detail(401, "Invalid credentials");
  if (area === "staff") return staffRoutes(state, user, request.method, rest, url, body);
  if (area === "admin") return adminRoutes(state, user, request.method, rest, url, body, raw);
  return detail(404, "Not Found");
}
