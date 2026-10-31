/**
 * The online booking stepper as a pure reducer: service -> doctor (or any) -> date and time
 * -> details and consent -> confirmation. Changing an earlier choice clears the later ones,
 * so a stale slot can never be submitted with a different service or doctor.
 */

import type { BookingResult, PublicBookingRequest, PublicDoctor, Slot } from "./api/types";
import { toIndianE164 } from "./phone";
import { dayKey } from "./time";

export const STEPS = ["service", "doctor", "time", "details", "done"] as const;
export type Step = (typeof STEPS)[number];
export const ANY_DOCTOR = "any";

export interface BookingState {
  step: Step;
  serviceId: string | null;
  doctorId: string | null; // a doctor id or ANY_DOCTOR
  date: string | null; // clinic-zone day key
  slot: Slot | null;
  name: string;
  phone: string; // the 10-digit national number as typed
  consent: boolean;
  notes: string;
  result: BookingResult | null;
}

export const initialBooking: BookingState = {
  step: "service",
  serviceId: null,
  doctorId: null,
  date: null,
  slot: null,
  name: "",
  phone: "",
  consent: false,
  notes: "",
  result: null,
};

export type BookingAction =
  | { type: "service"; id: string }
  | { type: "doctor"; id: string }
  | { type: "date"; date: string }
  | { type: "slot"; slot: Slot }
  | { type: "field"; field: "name" | "phone" | "notes"; value: string }
  | { type: "consent"; value: boolean }
  | { type: "goto"; step: Step }
  | { type: "slotLost" }
  | { type: "booked"; result: BookingResult }
  | { type: "reset" };

export type FieldErrors = Partial<Record<"service" | "doctor" | "slot" | "name" | "phone" | "notes", string>>;

const CONTROL = /[\u0000-\u001f\u007f]/;

export function validateDetails(state: Pick<BookingState, "name" | "phone" | "notes">): FieldErrors {
  const errors: FieldErrors = {};
  const name = state.name.trim();
  if (name.length < 2) errors.name = "Enter the patient's full name.";
  else if (name.length > 120) errors.name = "Keep the name under 120 characters.";
  else if (CONTROL.test(name)) errors.name = "The name contains characters we can't accept.";
  const phone = toIndianE164(state.phone);
  if (!phone.ok) errors.phone = phone.message;
  if (state.notes.length > 500) errors.notes = "Keep notes under 500 characters.";
  return errors;
}

/** Errors that stop the user leaving `step`. */
export function validateStep(state: BookingState, step: Step = state.step): FieldErrors {
  switch (step) {
    case "service":
      return state.serviceId ? {} : { service: "Choose a treatment." };
    case "doctor":
      return state.doctorId ? {} : { doctor: "Choose a dentist, or any available dentist." };
    case "time":
      return state.slot ? {} : { slot: "Choose a time." };
    case "details":
      return validateDetails(state);
    default:
      return {};
  }
}

export function canEnter(state: BookingState, step: Step): boolean {
  const index = STEPS.indexOf(step);
  for (let i = 0; i < index; i++) {
    const previous = STEPS[i]!;
    if (previous === "done") return false;
    if (Object.keys(validateStep(state, previous)).length) return false;
  }
  return step !== "done" || state.result !== null;
}

export function bookingReducer(state: BookingState, action: BookingAction): BookingState {
  switch (action.type) {
    case "service":
      if (state.serviceId === action.id) return { ...state, step: "doctor" };
      return { ...state, serviceId: action.id, doctorId: null, slot: null, date: null, step: "doctor" };
    case "doctor":
      if (state.doctorId === action.id) return { ...state, step: "time" };
      return { ...state, doctorId: action.id, slot: null, step: "time" };
    case "date":
      return { ...state, date: action.date, slot: state.slot && state.date === action.date ? state.slot : null };
    case "slot":
      return { ...state, slot: action.slot, step: "details" };
    case "field":
      return { ...state, [action.field]: action.value };
    case "consent":
      return { ...state, consent: action.value };
    case "goto":
      if (state.step === "done" || !canEnter(state, action.step)) return state;
      return { ...state, step: action.step };
    case "slotLost":
      return { ...state, slot: null, step: "time" };
    case "booked":
      return { ...state, result: action.result, step: "done" };
    case "reset":
      return initialBooking;
  }
}

/** The API request, or null if anything is missing or invalid. */
export function toRequest(state: BookingState): PublicBookingRequest | null {
  if (!state.serviceId || !state.slot) return null;
  if (Object.keys(validateDetails(state)).length) return null;
  const phone = toIndianE164(state.phone);
  if (!phone.ok) return null;
  const notes = state.notes.trim();
  return {
    service_id: state.serviceId,
    doctor_id: state.slot.doctor_id,
    starts_at: state.slot.starts_at,
    name: state.name.trim(),
    phone: phone.e164,
    consent_contact: state.consent,
    ...(notes ? { notes } : {}),
  };
}

export function doctorsFor(doctors: readonly PublicDoctor[], serviceId: string | null): PublicDoctor[] {
  if (!serviceId) return [];
  return doctors.filter((d) => d.service_ids.includes(serviceId));
}

/** Slots grouped by clinic-zone day, each day sorted by time. */
export function slotsByDay(slots: readonly Slot[], timeZone: string): Map<string, Slot[]> {
  const days = new Map<string, Slot[]>();
  for (const slot of [...slots].sort((a, b) => Date.parse(a.starts_at) - Date.parse(b.starts_at))) {
    const key = dayKey(slot.starts_at, timeZone);
    const list = days.get(key) ?? [];
    list.push(slot);
    days.set(key, list);
  }
  return days;
}

/** For "any dentist", one slot per start time (the first doctor offering it). */
export function distinctTimes(slots: readonly Slot[]): Slot[] {
  const seen = new Set<string>();
  const out: Slot[] = [];
  for (const slot of slots) {
    const key = String(Date.parse(slot.starts_at));
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(slot);
  }
  return out;
}

/** Morning / afternoon / evening buckets for the slot grid, by clinic minutes. */
export function partOfDay(minutes: number): "Morning" | "Afternoon" | "Evening" {
  if (minutes < 12 * 60) return "Morning";
  if (minutes < 17 * 60) return "Afternoon";
  return "Evening";
}
