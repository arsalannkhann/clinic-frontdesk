import { afterEach, describe, expect, it } from "vitest";
import { ApiClient, type Schemas } from "../src/api/client";
import type { Context } from "../src/context";
import { appointmentsScreen } from "../src/screens/appointments";
import {
  BUDGET_ZERO_WARNING,
  OUTBOUND_OFF_WARNING,
  confirmationsFor,
} from "../src/screens/clinic";
import { exceptionsScreen } from "../src/screens/exceptions";
import { ZONE_UNAVAILABLE } from "../src/clinicZone";

type Clinic = Schemas["ClinicConfig"];

const CLINIC: Clinic = {
  id: "00000000-0000-4000-8000-000000000001",
  name: "Demo",
  timezone: "Asia/Kolkata",
  lead_time_minutes: 60,
  horizon_days: 90,
  outbound_enabled: true,
  quiet_hours_start: "21:00",
  quiet_hours_end: "08:00",
  reminder_lead_minutes: 1440,
  daily_contact_budget: 100,
  emergency_text: "Call 112.",
  handoff_text: "Connecting you.",
  scripts_approved: true,
  test_calls_enabled: false,
  include_reference_in_messages: false,
  public_enabled: false,
  public_tagline: "",
  public_address: "",
  public_phone_display: "",
  public_email: "",
  public_hours_note: "",
  public_about: "",
  public_chat_enabled: false,
};

interface Seen {
  urls: string[];
}

function context(clinic: { status: number; body: unknown }, seen: Seen, zoneHolder = { tz: "UTC" }) {
  const api = new ApiClient({
    baseUrl: "",
    getToken: () => "t",
    onUnauthorized: () => undefined,
    fetchImpl: (async (url: RequestInfo | URL) => {
      const text = String(url);
      seen.urls.push(text);
      const path = text.split("?")[0];
      const body = path === "/v1/admin/clinic" ? clinic.body : [];
      const status = path === "/v1/admin/clinic" ? clinic.status : 200;
      return new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    }) as typeof fetch,
  });
  const ctx: Context = {
    api,
    user: { id: "u", email: "a@example.test", role: "admin" },
    timeZone: () => zoneHolder.tz,
    setTimeZone: (tz) => {
      zoneHolder.tz = tz;
    },
    refresh: () => undefined,
  };
  return ctx;
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 20));

afterEach(() => document.body.replaceChildren());

describe("clinic time zone on time-entry screens", () => {
  for (const screen of [appointmentsScreen, exceptionsScreen]) {
    it(`${screen.path}: loads the clinic zone itself and labels time inputs with it`, async () => {
      const seen: Seen = { urls: [] };
      const holder = { tz: "UTC" }; // A stale app-level value must not be used.
      const root = screen.render(context({ status: 200, body: CLINIC }, seen, holder));
      await settle();
      expect(seen.urls.some((u) => u.startsWith("/v1/admin/clinic"))).toBe(true);
      const labels = [...root.querySelectorAll(".field-label")].map((l) => l.textContent ?? "");
      const timeLabels = labels.filter((l) => /from|to,|starts|ends/i.test(l));
      expect(timeLabels.length).toBeGreaterThanOrEqual(2);
      for (const label of timeLabels) expect(label).toContain("Asia/Kolkata");
      expect(holder.tz).toBe("Asia/Kolkata");
    });

    it(`${screen.path}: refuses to render time inputs when the zone cannot be loaded`, async () => {
      for (const clinic of [
        { status: 503, body: { detail: "Scheduling unavailable" } },
        { status: 200, body: { ...CLINIC, timezone: "Mars/Olympus" } },
      ]) {
        const seen: Seen = { urls: [] };
        const root = screen.render(context(clinic, seen));
        await settle();
        expect(root.textContent).toContain(ZONE_UNAVAILABLE);
        expect(root.querySelector("input, button")).toBeNull();
        // Nothing time-filtered was requested with a guessed zone.
        expect(seen.urls.filter((u) => /appointments|exceptions/.test(u))).toEqual([]);
      }
    });
  }
});

describe("clinic settings confirmations", () => {
  it("confirms turning outbound off with the suppression warning", () => {
    const messages = confirmationsFor({ outbound_enabled: false }, CLINIC);
    expect(messages).toEqual([OUTBOUND_OFF_WARNING]);
    expect(OUTBOUND_OFF_WARNING).toContain(
      "Contacts due while outbound is off are suppressed permanently",
    );
  });

  it("confirms a zero daily budget and a time zone change", () => {
    expect(confirmationsFor({ daily_contact_budget: 0 }, CLINIC)).toEqual([BUDGET_ZERO_WARNING]);
    const zone = confirmationsFor({ timezone: "Europe/London" }, CLINIC);
    expect(zone).toHaveLength(1);
    expect(zone[0]).toContain("Asia/Kolkata");
    expect(zone[0]).toContain("Europe/London");
  });

  it("asks nothing for harmless changes", () => {
    expect(confirmationsFor({ daily_contact_budget: 5, name: "New name" }, CLINIC)).toEqual([]);
  });
});
