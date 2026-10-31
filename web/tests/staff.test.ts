import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useRouter: () => ({}), useSearchParams: () => new URLSearchParams() }));

import { callBlockReason } from "@/components/staff/CallbacksPage";
import { safeNext } from "@/components/staff/LoginForm";
import { validateHours } from "@/components/staff/SettingsPage";
import { canWriteRole } from "@/components/staff/StaffContext";

describe("staff helpers", () => {
  it("only returns to a staff page after sign-in", () => {
    expect(safeNext("/staff/calendar?view=week")).toBe("/staff/calendar?view=week");
    expect(safeNext("/staff")).toBe("/staff");
    for (const bad of [null, "", "https://evil.example", "//evil.example", "/staffevil", "/staff\\..\\x", "/staff/login", "/book"]) {
      expect(safeNext(bad)).toBe("/staff");
    }
  });

  it("explains why Call now is unavailable", () => {
    expect(callBlockReason(true, null, true)).toBeNull();
    expect(callBlockReason(false, null, true)).toMatch(/outbound/i);
    expect(callBlockReason(true, "no_from_number", true)).toBeTruthy();
    expect(callBlockReason(true, null, false)).toMatch(/owner or an admin/);
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
