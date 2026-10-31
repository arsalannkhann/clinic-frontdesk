import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useRouter: () => ({}), useSearchParams: () => new URLSearchParams() }));

import { callBlockReason, callFailure, outboundGap } from "@/components/staff/CallbacksPage";
import { safeNext } from "@/components/staff/LoginForm";
import { clinicChanges, clinicFieldErrors, validateHours } from "@/components/staff/SettingsPage";
import { canWriteRole } from "@/components/staff/StaffContext";
import { ApiError } from "@/lib/api/client";

describe("staff helpers", () => {
  it("only returns to a staff page after sign-in", () => {
    const origin = "https://clinic.example";
    expect(safeNext("/staff/calendar?view=week", origin)).toBe("/staff/calendar?view=week");
    expect(safeNext("/staff", origin)).toBe("/staff");
    expect(safeNext("/staff/callbacks#open", origin)).toBe("/staff/callbacks#open");
    expect(safeNext("/staff?x=1", origin)).toBe("/staff?x=1");
    for (const bad of [
      null,
      "",
      "https://evil.example",
      "https://clinic.example/staff",
      "//evil.example",
      "///evil.example/staff",
      "/\\evil.example",
      "/staffevil",
      "/staff\\..\\x",
      "/staff/login",
      "/staff/login?next=/staff",
      "/book",
      "/staff/../book",
      "/staff/./calendar",
      "/staff/%2e%2e/book",
      "/staff/%2E/calendar",
      "/staff/.%2e/book",
      "/staff/calendar/..",
      "/staff/\tcalendar",
      "javascript:alert(1)",
      "staff",
    ]) {
      expect(safeNext(bad, origin), String(bad)).toBe("/staff");
    }
  });

  it("uses the page origin by default", () => {
    expect(safeNext("/staff/settings")).toBe("/staff/settings");
    expect(safeNext(`${window.location.origin}/staff/settings`)).toBe("/staff");
  });

  it("explains why Call now is unavailable", () => {
    expect(callBlockReason(true, null, true)).toBeNull();
    expect(callBlockReason(false, null, true)).toMatch(/outbound/i);
    expect(callBlockReason(true, "no_from_number", true)).toBeTruthy();
    expect(callBlockReason(true, null, false)).toMatch(/owner or an admin/);
    const ready = { enabled: true, has_from_number: true, has_outbound_agent: true };
    expect(outboundGap(ready)).toBeNull();
    expect(outboundGap({ ...ready, has_from_number: false })).toBe("no_from_number");
    expect(outboundGap({ ...ready, has_outbound_agent: false })).toBe("no_outbound_agent");
    expect(outboundGap({ ...ready, enabled: false })).toBe("outbound_disabled");
    expect(callBlockReason({ ...ready, enabled: false }, null, true)).toMatch(/owner can turn it on in Settings once a calling number is configured/);
    expect(callBlockReason(ready, "outbound_disabled", true)).toMatch(/off/);
  });

  it("gives viewers read-only screens", () => {
    expect(canWriteRole("owner")).toBe(true);
    expect(canWriteRole("admin")).toBe(true);
    expect(canWriteRole("viewer")).toBe(false);
  });

  it("validates a working week before it is sent", () => {
    expect(validateHours([{ weekday: 0, opens: "09:30", closes: "13:30" }, { weekday: 0, opens: "14:30", closes: "19:00" }])).toBeNull();
    expect(validateHours([{ weekday: 1, opens: "14:00", closes: "09:00" }])).toMatch(/Tuesday: opening must be before closing/);
    expect(validateHours([{ weekday: 2, opens: "09:00", closes: "13:00" }, { weekday: 2, opens: "12:00", closes: "15:00" }])).toMatch(/Wednesday: windows overlap/);
    expect(validateHours([{ weekday: 0, opens: "", closes: "13:00" }])).toMatch(/opening and closing/);
  });
});

describe("clinic settings", () => {
  const original = {
    id: "c1",
    name: "Aster Dental",
    timezone: "Asia/Kolkata",
    lead_time_minutes: 60,
    horizon_days: 30,
    outbound_enabled: false,
    quiet_hours_start: "21:00",
    quiet_hours_end: "09:00",
    reminder_lead_minutes: 1440,
    daily_contact_budget: 50,
    emergency_text: "",
    handoff_text: "",
    scripts_approved: false,
    test_calls_enabled: false,
    include_reference_in_messages: false,
    public_slug: "aster-dental",
    public_enabled: true,
    public_tagline: "Gentle care",
    public_address: null,
    public_map_url: "https://maps.example/aster",
    public_chat_enabled: true,
    bolna_outbound_from_number: "+919800000001",
  };
  const { id: _id, timezone: _tz, public_slug: _slug, public_enabled: _enabled, ...base } = original;

  it("sends changed fields only, never null, and keeps owner-only fields to the owner", () => {
    const form = { ...base, public_tagline: "  Gentle care ", public_address: "", public_about: "", outbound_enabled: true, public_chat_enabled: false };
    expect(clinicChanges(form, original, true)).toEqual({ outbound_enabled: true, public_chat_enabled: false });
    expect(clinicChanges(form, original, false)).toEqual({ public_chat_enabled: false });
    // A cleared text goes as "", not null (the API refuses nulls).
    expect(clinicChanges({ ...base, public_tagline: "" }, original, false)).toEqual({ public_tagline: "" });
    expect(clinicChanges({ ...base, public_address: " 12 MG Road " }, original, false)).toEqual({ public_address: "12 MG Road" });
    expect(clinicChanges({ ...base, bolna_outbound_from_number: "+919800000002" }, original, false)).toEqual({});
    expect(clinicChanges({ ...base, bolna_outbound_from_number: "+919800000002" }, original, true)).toEqual({ bolna_outbound_from_number: "+919800000002" });
  });

  it("checks links and numbers, and refuses to clear what the API can't clear", () => {
    expect(clinicFieldErrors({ public_map_url: "" }, original).public_map_url).toMatch(/replaced but not cleared/);
    expect(clinicFieldErrors({ public_map_url: "http://maps.example" }, original).public_map_url).toMatch(/https/);
    expect(clinicFieldErrors({ public_voice_number: "98000 00001" }, original).public_voice_number).toMatch(/international/);
    expect(clinicFieldErrors({ bolna_outbound_from_number: "" }, original).bolna_outbound_from_number).toMatch(/not cleared/);
    expect(clinicFieldErrors({ public_tagline: "", public_voice_number: "+919800000003" }, original)).toEqual({});
  });
});

describe("call-backs", () => {
  const apiError = (status: number, code: string) => new ApiError(status, code, "upstream text");

  it("tells clinic-wide refusals from per-call failures, by code", () => {
    expect(callFailure(apiError(409, "no_outbound_agent"))).toMatchObject({ clinicWide: true, code: "no_outbound_agent" });
    expect(callFailure(apiError(409, "synthetic_contact"))).toMatchObject({ clinicWide: false, text: "Demo record — calls disabled.", refresh: true });
    expect(callFailure(apiError(409, "quiet_hours")).text).toMatch(/quiet hours/);
    expect(callFailure(apiError(503, "bolna_unconfigured")).text).toMatch(/voice provider/);
    expect(callFailure(apiError(502, "call_failed"))).toMatchObject({ refresh: true });
  });
});
