/**
 * The only place state codes become words (after dashboard/src/labels.ts). An appointment is
 * "Confirmed" only for the exact status `confirmed`; pending_sync and needs_review never are.
 * Unknown values read "Unknown".
 */

export type Tone = "ok" | "warn" | "bad" | "info" | "neutral";

export interface Label {
  text: string;
  tone: Tone;
}

const UNKNOWN: Label = { text: "Unknown", tone: "neutral" };

function table(entries: Record<string, [string, Tone]>) {
  const map = new Map(Object.entries(entries).map(([k, [text, tone]]) => [k, { text, tone }]));
  return (value: unknown): Label => (typeof value === "string" ? (map.get(value) ?? UNKNOWN) : UNKNOWN);
}

export const appointmentStatus = table({
  pending_sync: ["Pending sync", "warn"],
  confirmed: ["Confirmed", "ok"],
  needs_review: ["Needs review", "bad"],
  cancelled: ["Cancelled", "neutral"],
});

/** Patient-facing wording: never says confirmed before the calendar confirmed it. */
export const patientBookingStatus = table({
  pending_sync: ["Received, being confirmed", "warn"],
  confirmed: ["Confirmed", "ok"],
  needs_review: ["Received, the clinic will call you", "warn"],
  cancelled: ["Cancelled", "neutral"],
});

/** Calendar sync state; values beyond the plan's are assumed (see the P3 report). */
export const syncState = table({
  synced: ["Synced", "ok"],
  confirmed: ["Synced", "ok"],
  pending: ["Sync pending", "warn"],
  pending_sync: ["Sync pending", "warn"],
  failed: ["Sync failed", "bad"],
  needs_review: ["Sync failed", "bad"],
  not_applicable: ["No sync", "neutral"],
  ledger: ["Clinic ledger", "info"],
});

export const channel = table({
  voice: ["Phone", "info"],
  web: ["Website", "info"],
  chat: ["Chat", "info"],
  staff: ["Front desk", "neutral"],
});

export const callbackStatus = table({
  open: ["Open", "warn"],
  calling: ["Calling", "info"],
  done: ["Done", "ok"],
  dismissed: ["Dismissed", "neutral"],
});

export const callbackSource = table({
  web: ["Website", "neutral"],
  chat: ["Chat", "neutral"],
  voice: ["Phone", "neutral"],
});

export const exceptionKind = table({
  holiday: ["Clinic holiday", "neutral"],
  leave: ["Leave", "neutral"],
  break: ["Break", "neutral"],
});

export const outcome = table({
  booked: ["Booked", "ok"],
  callback_requested: ["Call-back requested", "info"],
  handoff: ["Handed to staff", "warn"],
  no_booking: ["No booking", "neutral"],
  abandoned: ["Left early", "neutral"],
});

export const role = table({
  owner: ["Owner", "info"],
  admin: ["Admin", "info"],
  viewer: ["Viewer (read-only)", "neutral"],
});

/** Refusals of "Call now", shown next to the disabled button. */
export const CALL_REFUSALS: Record<string, string> = {
  outbound_disabled: "Outbound calling is off for this clinic. The owner can turn it on in Settings.",
  no_from_number: "No caller number is connected yet, so calls cannot be placed.",
};
