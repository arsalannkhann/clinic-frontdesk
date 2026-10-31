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

/** Calendar sync state (staff calendar `sync_state`). */
export const syncState = table({
  synced: ["Synced", "ok"],
  pending: ["Sync pending", "warn"],
  failed: ["Sync failed", "bad"],
  not_applicable: ["No sync", "neutral"],
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

/** Clinic-wide refusals of "Call now", shown next to the disabled buttons. */
export const CALL_REFUSALS: Record<string, string> = {
  outbound_disabled: "Outbound calling is off for this clinic. The owner can turn it on in Settings once a calling number is configured.",
  no_from_number: "No calling number is configured yet, so calls can't be placed. The owner can set one in Settings and then turn outbound calling on.",
  no_outbound_agent: "No outbound voice agent is configured yet, so calls can't be placed.",
};

/**
 * Why one call didn't start (POST staff/callbacks/{id}/call). Codes come through on 5xx too;
 * the BFF drops the upstream message there, so the text is ours.
 */
export const CALL_FAILURES: Record<string, string> = {
  callback_resolved: "This request has already been resolved.",
  synthetic_contact: "Demo record — calls disabled.",
  quiet_hours: "It's within the clinic's quiet hours, so no call was placed. Try again after quiet hours end.",
  budget_exhausted: "Today's outbound contact budget is used up, so no call was placed.",
  retry: "The clinic's settings changed while the call was being set up. Try again.",
  bolna_unconfigured: "The voice provider isn't set up on the server, so calls can't be placed.",
  unavailable: "Calling is unavailable right now. Try again shortly.",
  call_failed: "The call couldn't be placed, and the request is open again. Try again later or ring from the clinic phone.",
};

/** Shown for a demo (synthetic) record, whose Call now is always disabled. */
export const DEMO_RECORD = "Demo record — calls disabled";

/**
 * What happened to the current call of a call-back (`call_outcome`), for requests in the
 * Calling state. A failed call returns the request to Open, so "failed" reads as history.
 */
export const CALL_OUTCOMES: Record<string, string> = {
  placed: "The call was placed. If it didn't reach the patient, reopen the request to try again.",
  unknown: "We couldn't confirm the call went through — check with the patient or reopen to try again.",
  failed: "The call couldn't be placed. Reopen the request to try again.",
};

/** Shown on an open request whose last call failed (the API reopened it). */
export const LAST_CALL_FAILED = "The last call couldn't be placed.";

/** Why a reopen (POST staff/callbacks/{id}/reopen) was refused, by code. */
export const REOPEN_FAILURES: Record<string, string> = {
  call_in_progress: "The call is still being placed. Try again in a moment.",
  callback_resolved: "This request has already been resolved.",
  not_found: "This request no longer exists.",
};
