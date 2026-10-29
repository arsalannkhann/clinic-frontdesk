import { describe, expect, it } from "vitest";
import {
  APPOINTMENT_STATUSES,
  REMINDER_STATUSES,
  UNKNOWN,
  appointmentStatusLabel,
  calendarAccountLabel,
  outboxStatusLabel,
  provisioningStateLabel,
  reminderStatusLabel,
  syncLabel,
} from "../src/labels";

const ODD_VALUES: unknown[] = [
  "unexpected_new_state",
  "",
  "CONFIRMED",
  "Confirmed",
  " confirmed",
  "confirmed ",
  "__proto__",
  "constructor",
  "toString",
  "hasOwnProperty",
  null,
  undefined,
  42,
  true,
  {},
  ["confirmed"],
];

describe("appointment labels", () => {
  it("shows pending_sync and needs_review as such, never as confirmed", () => {
    expect(appointmentStatusLabel("pending_sync").text).toBe("Pending calendar sync");
    expect(appointmentStatusLabel("needs_review").text).toBe("Needs review");
    expect(syncLabel("pending_sync").text).toBe("Pending calendar sync");
    for (const status of ["pending_sync", "needs_review", "cancelled"]) {
      expect(appointmentStatusLabel(status).text).not.toMatch(/confirm/i);
      expect(appointmentStatusLabel(status).tone).not.toBe("ok");
      expect(syncLabel(status).tone).not.toBe("ok");
    }
  });

  it("shows Confirmed only for the exact status confirmed", () => {
    expect(appointmentStatusLabel("confirmed")).toEqual({ text: "Confirmed", tone: "ok" });
    const confirmedLike = APPOINTMENT_STATUSES.filter(
      (s) => appointmentStatusLabel(s).text === "Confirmed",
    );
    expect(confirmedLike).toEqual(["confirmed"]);
    for (const value of ODD_VALUES) {
      expect(appointmentStatusLabel(value).text).not.toBe("Confirmed");
    }
  });

  it("shows unknown values as a neutral Unknown", () => {
    for (const value of ODD_VALUES) {
      expect(appointmentStatusLabel(value)).toEqual(UNKNOWN);
      expect(syncLabel(value)).toEqual(UNKNOWN);
    }
    expect(UNKNOWN).toEqual({ text: "Unknown", tone: "neutral" });
  });
});

describe("reminder, provisioning, outbox and calendar labels", () => {
  it("label every known reminder status and fall back to Unknown", () => {
    for (const status of REMINDER_STATUSES) expect(reminderStatusLabel(status)).not.toEqual(UNKNOWN);
    expect(reminderStatusLabel("sent").text).toBe("Sent");
    // The backend's "unknown" outcome is distinct from an unrecognized code.
    expect(reminderStatusLabel("unknown").text).toBe("Outcome unknown");
    expect(reminderStatusLabel("scheduled").tone).not.toBe("ok");
    for (const value of ODD_VALUES) expect(reminderStatusLabel(value)).toEqual(UNKNOWN);
  });

  it("only active provisioning reads as live", () => {
    const states = [
      "draft",
      "calendar_connected",
      "agent_created",
      "number_assigned",
      "verified",
      "active",
      "failed",
      "manual_review",
    ];
    for (const state of states) expect(provisioningStateLabel(state)).not.toEqual(UNKNOWN);
    const live = states.filter((s) => /live/i.test(provisioningStateLabel(s).text) && !/not live/i.test(provisioningStateLabel(s).text));
    expect(live).toEqual(["active"]);
    expect(provisioningStateLabel("verified").text).toBe("Verified (not live)");
    for (const value of ODD_VALUES) expect(provisioningStateLabel(value)).toEqual(UNKNOWN);
  });

  it("outbox and calendar fall back to Unknown", () => {
    expect(outboxStatusLabel("dead").tone).toBe("bad");
    expect(calendarAccountLabel("active").text).toBe("Connected");
    for (const value of ODD_VALUES) {
      expect(outboxStatusLabel(value)).toEqual(UNKNOWN);
      expect(calendarAccountLabel(value)).toEqual(UNKNOWN);
    }
  });
});
