import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useRouter: () => ({}), useSearchParams: () => new URLSearchParams() }));

import { callBlockReason, outboundGap } from "@/components/staff/CallbacksPage";
import { safeNext } from "@/components/staff/LoginForm";
import { validateHours } from "@/components/staff/SettingsPage";
import { canWriteRole } from "@/components/staff/StaffContext";

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
