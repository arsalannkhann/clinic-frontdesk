/**
 * The only place state codes become words. An appointment is shown as "Confirmed" only for
 * the exact status `confirmed` (the calendar consistency policy completed); pending_sync,
 * needs_review and anything unrecognized never are. Unrecognized values show "Unknown".
 */

export type Tone = "ok" | "warn" | "bad" | "info" | "neutral";

export interface Label {
  readonly text: string;
  readonly tone: Tone;
}

export const UNKNOWN: Label = Object.freeze({ text: "Unknown", tone: "neutral" });

function lookup(table: ReadonlyMap<string, Label>, value: unknown): Label {
  return typeof value === "string" ? (table.get(value) ?? UNKNOWN) : UNKNOWN;
}

function table(entries: Record<string, [string, Tone]>): ReadonlyMap<string, Label> {
  return new Map(
    Object.entries(entries).map(([key, [text, tone]]) => [key, Object.freeze({ text, tone })]),
  );
}

const APPOINTMENT = table({
  pending_sync: ["Pending calendar sync", "warn"],
  confirmed: ["Confirmed", "ok"],
  needs_review: ["Needs review", "bad"],
  cancelled: ["Cancelled", "neutral"],
});

// Calendar sync state derived from the appointment status. needs_review does not say why:
// the calendar write may have failed, or the slot may be busy in the external calendar
// (external_conflict) or the review may be a staff/reconciliation decision, so it never
// claims "sync failed".
const SYNC = table({
  pending_sync: ["Pending calendar sync", "warn"],
  confirmed: ["Synced to calendar", "ok"],
  needs_review: ["Not synced: needs staff review", "bad"],
  cancelled: ["Not applicable (cancelled)", "neutral"],
});

const REMINDER = table({
  scheduled: ["Scheduled", "info"],
  sending: ["Sending", "info"],
  sent: ["Sent", "ok"],
  unknown: ["Outcome unknown", "warn"],
  needs_review: ["Needs review", "bad"],
  cancelled: ["Cancelled", "neutral"],
  suppressed: ["Suppressed", "neutral"],
  failed: ["Failed", "bad"],
});

const PROVISIONING = table({
  draft: ["Draft", "neutral"],
  calendar_connected: ["Calendar connected", "info"],
  agent_created: ["Agent created", "info"],
  number_assigned: ["Number assigned", "info"],
  verified: ["Verified (not live)", "warn"],
  active: ["Active (live)", "ok"],
  failed: ["Failed", "bad"],
  manual_review: ["Manual review", "bad"],
});

const OUTBOX = table({
  pending: ["Pending", "info"],
  processing: ["Processing", "info"],
  dead: ["Dead (needs review)", "bad"],
  done: ["Done", "ok"],
});

const CALENDAR_ACCOUNT = table({
  active: ["Connected", "ok"],
  revoked: ["Disconnected", "neutral"],
  error: ["Error: reconnect needed", "bad"],
});

export const appointmentStatusLabel = (status: unknown): Label => lookup(APPOINTMENT, status);
export const syncLabel = (status: unknown): Label => lookup(SYNC, status);
export const reminderStatusLabel = (status: unknown): Label => lookup(REMINDER, status);
export const provisioningStateLabel = (state: unknown): Label => lookup(PROVISIONING, state);
export const outboxStatusLabel = (status: unknown): Label => lookup(OUTBOX, status);
export const calendarAccountLabel = (status: unknown): Label => lookup(CALENDAR_ACCOUNT, status);

export const APPOINTMENT_STATUSES = ["pending_sync", "confirmed", "needs_review", "cancelled"] as const;
export const REMINDER_STATUSES = [
  "scheduled",
  "sending",
  "sent",
  "unknown",
  "needs_review",
  "cancelled",
  "suppressed",
  "failed",
] as const;
