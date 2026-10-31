import { describe, expect, it } from "vitest";

import type { PublicDoctor, Slot } from "@/lib/api/types";
import {
  ANY_DOCTOR,
  bookingReducer,
  canEnter,
  distinctTimes,
  doctorsFor,
  initialBooking,
  partOfDay,
  slotsByDay,
  toRequest,
  validateDetails,
  validateStep,
  type BookingAction,
  type BookingState,
} from "@/lib/booking";
import { formatIndian, nationalDigits, telHref, toIndianE164 } from "@/lib/phone";

const IST = "Asia/Kolkata";
const slot = (doctor: string, startsAt: string): Slot => ({ doctor_id: doctor, starts_at: startsAt, ends_at: startsAt });
const run = (...actions: BookingAction[]) => actions.reduce(bookingReducer, initialBooking);
const S1 = slot("d1", "2026-11-02T10:00:00+05:30");

describe("phone numbers", () => {
  it("accepts a 10-digit Indian mobile number in any common form", () => {
    for (const input of ["9876543210", "+91 98765 43210", "919876543210", "09876543210", "98765-43210"]) {
      expect(toIndianE164(input)).toEqual({ ok: true, e164: "+919876543210" });
    }
  });

  it("explains what is wrong", () => {
    expect(toIndianE164("")).toMatchObject({ ok: false, message: expect.stringMatching(/Enter/) });
    expect(toIndianE164("98765")).toMatchObject({ ok: false, message: expect.stringMatching(/10-digit/) });
    expect(toIndianE164("5876543210")).toMatchObject({ ok: false, message: expect.stringMatching(/6, 7, 8 or 9/) });
  });

  it("formats and links numbers", () => {
    expect(nationalDigits("+91 90000 00100")).toBe("9000000100");
    expect(formatIndian("+919000000100")).toBe("+91 90000 00100");
    expect(telHref("+91 90000 00100")).toBe("tel:+919000000100");
    expect(telHref("call us")).toBeNull();
  });
});

describe("booking stepper", () => {
  it("moves forward through the steps", () => {
    let state = run({ type: "service", id: "s1" });
    expect(state.step).toBe("doctor");
    state = bookingReducer(state, { type: "doctor", id: ANY_DOCTOR });
    expect(state.step).toBe("time");
    state = bookingReducer(state, { type: "date", date: "2026-11-02" });
    state = bookingReducer(state, { type: "slot", slot: S1 });
    expect(state.step).toBe("details");
  });

  it("clears later choices when an earlier one changes", () => {
    const chosen = run({ type: "service", id: "s1" }, { type: "doctor", id: "d1" }, { type: "date", date: "2026-11-02" }, { type: "slot", slot: S1 });
    const changedService = bookingReducer(chosen, { type: "service", id: "s2" });
    expect(changedService).toMatchObject({ serviceId: "s2", doctorId: null, slot: null, date: null, step: "doctor" });
    const changedDoctor = bookingReducer(chosen, { type: "doctor", id: "d2" });
    expect(changedDoctor).toMatchObject({ doctorId: "d2", slot: null, step: "time" });
    const changedDate = bookingReducer(chosen, { type: "date", date: "2026-11-03" });
    expect(changedDate.slot).toBeNull();
    // Re-choosing the same value keeps the rest.
    expect(bookingReducer(chosen, { type: "service", id: "s1" }).slot).toEqual(S1);
  });

  it("refuses to jump ahead past an incomplete step", () => {
    const state = run({ type: "service", id: "s1" });
    expect(canEnter(state, "time")).toBe(false);
    expect(bookingReducer(state, { type: "goto", step: "details" }).step).toBe("doctor");
    expect(bookingReducer(state, { type: "goto", step: "service" }).step).toBe("service");
    expect(canEnter(initialBooking, "done")).toBe(false);
  });

  it("validates the details step", () => {
    expect(validateDetails({ name: "A", phone: "123", notes: "" })).toEqual({
      name: expect.any(String),
      phone: expect.any(String),
    });
    expect(validateDetails({ name: "Ananya Krishnan", phone: "9876543210", notes: "x".repeat(501) })).toEqual({
      notes: expect.any(String),
    });
    expect(validateDetails({ name: "Ana\u0007", phone: "9876543210", notes: "" }).name).toBeDefined();
    expect(validateStep(initialBooking)).toEqual({ service: "Choose a treatment." });
  });

  it("builds the API request only when complete, with E.164 phone and the slot's doctor", () => {
    const base = run({ type: "service", id: "s1" }, { type: "doctor", id: ANY_DOCTOR }, { type: "slot", slot: S1 });
    expect(toRequest(base)).toBeNull();
    const filled: BookingState = [
      { type: "field", field: "name", value: "  Ananya Krishnan " },
      { type: "field", field: "phone", value: "98765 43210" },
      { type: "consent", value: true },
    ].reduce((s, a) => bookingReducer(s, a as BookingAction), base);
    expect(toRequest(filled)).toEqual({
      service_id: "s1",
      doctor_id: "d1",
      starts_at: S1.starts_at,
      name: "Ananya Krishnan",
      phone: "+919876543210",
      consent_contact: true,
    });
  });

  it("returns to time selection when the slot is lost to a conflict", () => {
    const state = run({ type: "service", id: "s1" }, { type: "doctor", id: "d1" }, { type: "slot", slot: S1 });
    expect(bookingReducer(state, { type: "slotLost" })).toMatchObject({ slot: null, step: "time", serviceId: "s1" });
  });

  it("locks the flow once booked", () => {
    const booked = bookingReducer(run({ type: "service", id: "s1" }), {
      type: "booked",
      result: { reference: "ABCD2345", status: "confirmed", starts_at: S1.starts_at, doctor_name: "Dr. A", service_name: "Check-up" },
    });
    expect(booked.step).toBe("done");
    expect(bookingReducer(booked, { type: "goto", step: "service" }).step).toBe("done");
    expect(bookingReducer(booked, { type: "reset" })).toEqual(initialBooking);
  });
});

describe("slot grouping", () => {
  it("groups slots by clinic day in time order", () => {
    const slots = [
      slot("d1", "2026-11-03T09:30:00+05:30"),
      slot("d1", "2026-11-02T19:00:00Z"), // 00:30 on the 3rd in India
      slot("d1", "2026-11-02T10:00:00+05:30"),
    ];
    const days = slotsByDay(slots, IST);
    expect([...days.keys()]).toEqual(["2026-11-02", "2026-11-03"]);
    expect(days.get("2026-11-03")!.map((s) => s.starts_at)).toEqual(["2026-11-02T19:00:00Z", "2026-11-03T09:30:00+05:30"]);
  });

  it("collapses equal times across doctors for 'any dentist'", () => {
    const slots = [slot("d1", "2026-11-02T10:00:00+05:30"), slot("d2", "2026-11-02T04:30:00Z"), slot("d2", "2026-11-02T10:15:00+05:30")];
    expect(distinctTimes(slots).map((s) => s.doctor_id)).toEqual(["d1", "d2"]);
  });

  it("filters doctors by service and buckets times", () => {
    const doctors = [{ id: "d1", service_ids: ["s1"] }, { id: "d2", service_ids: ["s2"] }] as PublicDoctor[];
    expect(doctorsFor(doctors, "s1").map((d) => d.id)).toEqual(["d1"]);
    expect(doctorsFor(doctors, null)).toEqual([]);
    expect([partOfDay(9 * 60), partOfDay(12 * 60), partOfDay(17 * 60)]).toEqual(["Morning", "Afternoon", "Evening"]);
  });
});
