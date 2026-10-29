import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { ApiClient } from "../src/api/client";
import { consentUrl } from "../src/screens/calendar";
import { appointmentsScreen } from "../src/screens/appointments";
import { h, mount } from "../src/dom";
import type { Context } from "../src/context";
import { badge, table } from "../src/ui";
import { appointmentStatusLabel } from "../src/labels";

const PAYLOAD = `<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>`;

declare global {
  interface Window {
    __xss?: number;
  }
}

afterEach(() => {
  document.body.replaceChildren();
  delete window.__xss;
});

describe("rendering user data", () => {
  it("renders strings as text, not HTML", () => {
    const root = h("div");
    document.body.append(root);
    mount(root, table(["Name"], [[PAYLOAD]], "none"), h("p", {}, PAYLOAD));
    expect(root.querySelector("img")).toBeNull();
    expect(root.querySelector("script")).toBeNull();
    expect(root.querySelectorAll("td")[0]?.textContent).toBe(PAYLOAD);
    expect(root.querySelector("p")?.textContent).toBe(PAYLOAD);
    expect(window.__xss).toBeUndefined();
  });

  it("renders a hostile appointment from the API as text", async () => {
    const appointment = {
      id: "00000000-0000-4000-8000-0000000000aa",
      booking_reference: PAYLOAD,
      doctor_id: "d1",
      service_id: "s1",
      starts_at: "2030-01-01T04:30:00Z",
      ends_at: "2030-01-01T05:00:00Z",
      status: "<b>confirmed</b>",
      rescheduled_from: null,
      created_at: "2029-12-01T00:00:00Z",
      patient_name: PAYLOAD,
      phone_last2: "<i>",
    };
    const responses: Record<string, unknown> = {
      "/v1/admin/doctors": [{ id: "d1", name: PAYLOAD, active: true, booking_calendar_id: null, busy_calendar_ids: [], calendar_account_id: null }],
      "/v1/admin/services": [],
      "/v1/admin/clinic": { timezone: "Asia/Kolkata" },
      "/v1/admin/appointments": [appointment],
    };
    const api = new ApiClient({
      baseUrl: "",
      getToken: () => "t",
      onUnauthorized: () => undefined,
      fetchImpl: (async (url: RequestInfo | URL) => {
        const path = String(url).split("?")[0] ?? "";
        return new Response(JSON.stringify(responses[path] ?? []), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }) as typeof fetch,
    });
    const ctx: Context = {
      api,
      user: { id: "u", email: "v@example.test", role: "viewer" },
      timeZone: () => "Asia/Kolkata",
      setTimeZone: () => undefined,
      refresh: () => undefined,
    };
    const root = appointmentsScreen.render(ctx);
    document.body.append(root);
    await new Promise((resolve) => setTimeout(resolve, 20));
    const text = root.textContent ?? "";
    expect(text).toContain(PAYLOAD);
    expect(root.querySelector("img, script, b, i")).toBeNull();
    expect(window.__xss).toBeUndefined();
    // An unrecognized status (even one containing "confirmed") reads Unknown.
    const badges = [...root.querySelectorAll(".badge")].map((b) => b.textContent);
    expect(badges.length).toBeGreaterThan(0);
    expect(new Set(badges)).toEqual(new Set(["Unknown"]));
    // Viewers get no mutation controls.
    expect([...root.querySelectorAll("button")].map((b) => b.textContent)).toEqual(["Apply filters"]);
  });

  it("badges use the label text only", () => {
    const el = badge(appointmentStatusLabel(PAYLOAD));
    expect(el.textContent).toBe("Unknown");
    expect(el.children).toHaveLength(0);
  });

  it("creates only allowlisted tags", () => {
    for (const tag of ["script", "iframe", "object", "embed", "style", "link", "meta", "img", "base"]) {
      expect(() => h(tag as "div")).toThrow(/Tag not allowed/);
    }
    expect(h("div").tagName).toBe("DIV");
  });

  it("refuses URL-list attributes", () => {
    expect(() => h("a", { attrs: { ping: "https://evil.example" } })).toThrow();
    expect(() => h("div", { attrs: { srcset: "https://evil.example/x 1x" } })).toThrow();
    expect(() => h("div", { attrs: { SrcSet: "x" } })).toThrow();
  });

  it("refuses event-handler attributes and script URLs", () => {
    expect(() => h("div", { attrs: { onclick: "alert(1)" } })).toThrow();
    expect(() => h("div", { attrs: { style: "x" } })).toThrow();
    expect(() => h("a", { attrs: { href: "javascript:alert(1)" } })).toThrow();
    expect(() => h("a", { attrs: { href: " JaVaScRiPt:alert(1)" } })).toThrow();
    expect(() => h("a", { attrs: { href: "data:text/html,x" } })).toThrow();
    expect(h("a", { attrs: { href: "https://accounts.google.com/o" } }).getAttribute("href")).toBe(
      "https://accounts.google.com/o",
    );
  });

  it("accepts only Google's https consent URL for calendar authorization", () => {
    expect(consentUrl("https://accounts.google.com/o/oauth2/v2/auth?state=s")).not.toBeNull();
    for (const bad of [
      "javascript:alert(1)",
      "http://accounts.google.com/o",
      "https://accounts.google.com.evil.example/o",
      "https://evil.example/?https://accounts.google.com",
      "not a url",
    ]) {
      expect(consentUrl(bad)).toBeNull();
    }
  });
});

describe("source hygiene", () => {
  const files = (dir: string): string[] =>
    readdirSync(dir).flatMap((name) => {
      const path = join(dir, name);
      return statSync(path).isDirectory() ? files(path) : [path];
    });
  const sources = files(join(__dirname, "..", "src")).filter((p) => /\.ts$/.test(p));

  it("never uses HTML string sinks, eval, web storage or console logging", () => {
    const forbidden =
      /\.(innerHTML|outerHTML)\s*=|insertAdjacentHTML|document\.write|\beval\(|new Function\(|localStorage|sessionStorage|console\.(log|info|warn|error|debug)|document\.cookie/;
    const offenders = sources.filter((path) => {
      const code = readFileSync(path, "utf8")
        .replace(/\/\*[\s\S]*?\*\//g, "")
        .replace(/^\s*\/\/.*$/gm, "");
      return forbidden.test(code);
    });
    expect(offenders).toEqual([]);
  });
});
