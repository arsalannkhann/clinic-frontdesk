// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { login, logout } from "@/lib/server/auth";
import { ConfigError, readConfig } from "@/lib/server/config";
import { normaliseError } from "@/lib/server/errors";
import { clientIp, isSameOriginWrite, readSessionToken } from "@/lib/server/request";
import { adminHandler, chatHandler, publicHandler, staffHandler } from "@/lib/server/routes";
import { pickQuery, safeSegments } from "@/lib/server/upstream";

const SITE = "http://localhost:3000";
const API = "https://api.example.test";
const TOKEN = "tok_abcdefghijklmnop1234";
const PROXY_SECRET = "test-proxy-secret-0123456789";

type FetchCall = { url: string; init: RequestInit };
let calls: FetchCall[];
let respond: (call: FetchCall) => Response | Promise<Response>;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

beforeEach(() => {
  calls = [];
  respond = () => json({ ok: true });
  vi.stubEnv("API_BASE_URL", API);
  vi.stubEnv("CLINIC_SLUG", "aster-dental");
  vi.stubEnv("SITE_URL", "");
  vi.stubEnv("PUBLIC_PROXY_SECRET", PROXY_SECRET);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit) => {
      const call = { url: String(url), init };
      calls.push(call);
      return respond(call);
    }),
  );
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

function request(path: string, init: Omit<RequestInit, "headers"> & { headers?: Record<string, string> } = {}) {
  const method = (init.method ?? "GET").toUpperCase();
  const headers: Record<string, string> = { host: "localhost:3000", ...(init.headers ?? {}) };
  if (method !== "GET" && !("origin" in headers) && !("sec-fetch-site" in headers)) headers.origin = SITE;
  if (init.body && !("content-type" in headers)) headers["content-type"] = "application/json";
  return new Request(`${SITE}${path}`, { ...init, method, headers });
}

const ctx = (path: string[] | undefined) => ({ params: Promise.resolve({ path }) });
const upstreamHeaders = (i = 0) => new Headers(calls[i]!.init.headers);
const setCookies = (response: Response) => response.headers.getSetCookie();

describe("staff login", () => {
  it("sets an httpOnly SameSite=Lax cookie and never returns the token", async () => {
    respond = () =>
      json({ token: TOKEN, expires_at: "2030-01-01T00:00:00Z", user: { id: "u1", email: "a@b.example", role: "admin" } });
    const response = await login(
      request("/api/staff/login", { method: "POST", body: JSON.stringify({ email: "a@b.example", password: "pw" }) }),
    );
    expect(response.status).toBe(200);
    const text = await response.text();
    expect(text).not.toContain(TOKEN);
    expect(JSON.parse(text)).toEqual({
      user: { id: "u1", email: "a@b.example", role: "admin" },
      expires_at: "2030-01-01T00:00:00.000Z",
    });
    const [cookie] = setCookies(response);
    expect(cookie).toMatch(/^staff_session=tok_abcdefghijklmnop1234;/);
    expect(cookie).toMatch(/HttpOnly/i);
    expect(cookie).toMatch(/SameSite=Lax/i);
    expect(cookie).toMatch(/Path=\//);
    expect(cookie).not.toMatch(/Secure/i); // development; production adds Secure and __Host-
    expect(calls[0]!.url).toBe(`${API}/v1/staff/login`);
    expect(upstreamHeaders().get("authorization")).toBeNull();
  });

  it("maps a 401 to invalid_credentials without a cookie", async () => {
    respond = () => json({ detail: "Invalid credentials" }, 401);
    const response = await login(
      request("/api/staff/login", { method: "POST", body: JSON.stringify({ email: "a@b.example", password: "x" }) }),
    );
    expect(response.status).toBe(401);
    expect((await response.json()).error.code).toBe("invalid_credentials");
    expect(setCookies(response)).toHaveLength(0);
  });

  it("rejects a cross-origin sign-in before calling the API", async () => {
    const response = await login(
      request("/api/staff/login", {
        method: "POST",
        headers: { origin: "https://evil.example" },
        body: JSON.stringify({ email: "a@b.example", password: "pw" }),
      }),
    );
    expect(response.status).toBe(403);
    expect((await response.json()).error.code).toBe("forbidden_origin");
    expect(calls).toHaveLength(0);
  });

  it("refuses an upstream success without a valid token", async () => {
    respond = () => json({ token: "bad token!", user: { id: "u1", email: "a", role: "owner" } });
    const response = await login(
      request("/api/staff/login", { method: "POST", body: JSON.stringify({ email: "a@b.example", password: "pw" }) }),
    );
    expect(response.status).toBe(502);
    expect(setCookies(response)).toHaveLength(0);
  });

  it("validates credentials locally", async () => {
    const response = await login(request("/api/staff/login", { method: "POST", body: JSON.stringify({ email: "" }) }));
    expect(response.status).toBe(422);
    expect(calls).toHaveLength(0);
  });
});

describe("staff logout", () => {
  it("revokes upstream with the cookie token and clears the cookie", async () => {
    respond = () => new Response(null, { status: 204 });
    const response = await logout(
      request("/api/staff/logout", { method: "POST", headers: { cookie: `staff_session=${TOKEN}` } }),
    );
    expect(response.status).toBe(204);
    expect(calls[0]!.url).toBe(`${API}/v1/staff/logout`);
    expect(upstreamHeaders().get("authorization")).toBe(`Bearer ${TOKEN}`);
    const [cookie] = setCookies(response);
    expect(cookie).toMatch(/^staff_session=;/);
    expect(cookie).toMatch(/Max-Age=0/i);
  });

  it("clears the cookie even when the API is unreachable", async () => {
    respond = () => {
      throw new TypeError("fetch failed");
    };
    const response = await logout(
      request("/api/staff/logout", { method: "POST", headers: { cookie: `staff_session=${TOKEN}` } }),
    );
    expect(response.status).toBe(204);
    expect(setCookies(response)[0]).toMatch(/Max-Age=0/i);
  });
});

describe("staff and admin proxy", () => {
  it("forwards the cookie as a bearer token and never forwards cookies", async () => {
    respond = () => json({ id: "u1" });
    const response = await staffHandler(
      request("/api/staff/me", {
        headers: { cookie: `other=1; staff_session=${TOKEN}`, "x-forwarded-for": "203.0.113.9, 10.0.0.1" },
      }),
      ctx(["me"]),
    );
    expect(response.status).toBe(200);
    expect(calls[0]!.url).toBe(`${API}/v1/staff/me`);
    const headers = upstreamHeaders();
    expect(headers.get("authorization")).toBe(`Bearer ${TOKEN}`);
    expect(headers.get("cookie")).toBeNull();
    expect(headers.get("x-forwarded-for")).toBe("203.0.113.9");
  });

  it("answers 401 without calling the API when there is no session", async () => {
    const response = await adminHandler(request("/api/admin/clinic"), ctx(["clinic"]));
    expect(response.status).toBe(401);
    expect(calls).toHaveLength(0);
  });

  it("clears the cookie when the API rejects the session", async () => {
    respond = () => json({ detail: "Session expired for user 42" }, 401);
    const response = await staffHandler(
      request("/api/staff/calendar", { headers: { cookie: `staff_session=${TOKEN}` } }),
      ctx(["calendar"]),
    );
    expect(response.status).toBe(401);
    const body = await response.json();
    expect(body.error.code).toBe("unauthorized");
    expect(JSON.stringify(body)).not.toContain("user 42");
    expect(setCookies(response)[0]).toMatch(/^staff_session=;.*Max-Age=0/i);
  });

  it("rejects cross-origin writes (CSRF) before calling the API", async () => {
    const response = await adminHandler(
      request("/api/admin/appointments/a1/cancel", {
        method: "POST",
        headers: { cookie: `staff_session=${TOKEN}`, origin: "https://evil.example" },
        body: "{}",
      }),
      ctx(["appointments", "a1", "cancel"]),
    );
    expect(response.status).toBe(403);
    expect(calls).toHaveLength(0);
  });

  it("does not proxy login or logout through the catch-all", async () => {
    const response = await staffHandler(
      request("/api/staff/login", { method: "POST", body: "{}" }),
      ctx(["login"]),
    );
    expect(response.status).toBe(404);
    expect(calls).toHaveLength(0);
  });

  it("keeps only allowlisted query parameters", async () => {
    await staffHandler(
      request("/api/staff/calendar?from=2026-11-02T00:00:00%2B05:30&to=x&evil=1&status=<script>", {
        headers: { cookie: `staff_session=${TOKEN}` },
      }),
      ctx(["calendar"]),
    );
    const url = new URL(calls[0]!.url);
    expect(url.searchParams.get("from")).toBe("2026-11-02T00:00:00+05:30");
    expect(url.searchParams.get("to")).toBe("x");
    expect(url.searchParams.has("evil")).toBe(false);
    expect(url.searchParams.has("status")).toBe(false);
  });

  it("drops a reserved bolna: idempotency key and forwards a valid one", async () => {
    await staffHandler(
      request("/api/staff/bookings", {
        method: "POST",
        headers: { cookie: `staff_session=${TOKEN}`, "idempotency-key": "bolna:abcdefgh" },
        body: "{}",
      }),
      ctx(["bookings"]),
    );
    expect(upstreamHeaders(0).get("idempotency-key")).toBeNull();
    await staffHandler(
      request("/api/staff/bookings", {
        method: "POST",
        headers: { cookie: `staff_session=${TOKEN}`, "idempotency-key": "staff-0123456789abcdef" },
        body: "{}",
      }),
      ctx(["bookings"]),
    );
    expect(upstreamHeaders(1).get("idempotency-key")).toBe("staff-0123456789abcdef");
  });
});

describe("public and chat proxy", () => {
  it("maps public paths onto the configured clinic", async () => {
    await publicHandler(request("/api/public/slots?service_id=s1&date=2026-11-02&days=7&token=x"), ctx(["slots"]));
    expect(calls[0]!.url).toBe(`${API}/v1/public/clinics/aster-dental/slots?service_id=s1&date=2026-11-02&days=7`);
    await publicHandler(request("/api/public"), ctx(undefined));
    expect(calls[1]!.url).toBe(`${API}/v1/public/clinics/aster-dental`);
  });

  it("forwards no cookies or authorization on public calls", async () => {
    await publicHandler(
      request("/api/public/bookings", {
        method: "POST",
        headers: { cookie: `staff_session=${TOKEN}` },
        body: JSON.stringify({ a: 1 }),
      }),
      ctx(["bookings"]),
    );
    const headers = upstreamHeaders();
    expect(headers.get("cookie")).toBeNull();
    expect(headers.get("authorization")).toBeNull();
  });

  it("404s paths outside the allowlist without an upstream call", async () => {
    for (const path of [["admin"], ["..", "staff"], ["bookings%2F..%2Fx"], ["a", "b", "c", "d", "e", "f", "g"]]) {
      const response = await publicHandler(request(`/api/public/${path.join("/")}`), ctx(path));
      expect(response.status).toBe(404);
    }
    const wrongMethod = await publicHandler(request("/api/public/services", { method: "POST", body: "{}" }), ctx(["services"]));
    expect(wrongMethod.status).toBe(405);
    expect(calls).toHaveLength(0);
  });

  it("maps chat routes", async () => {
    await chatHandler(request("/api/chat/sessions", { method: "POST", body: "{}" }), ctx(["sessions"]));
    await chatHandler(request("/api/chat/sessions/abc/messages", { method: "POST", body: "{}" }), ctx(["sessions", "abc", "messages"]));
    await chatHandler(
      request("/api/chat/sessions/abc/proposals/p1/confirm", { method: "POST", body: "{}" }),
      ctx(["sessions", "abc", "proposals", "p1", "confirm"]),
    );
    expect(calls.map((c) => c.url)).toEqual([
      `${API}/v1/chat/clinics/aster-dental/sessions`,
      `${API}/v1/chat/sessions/abc/messages`,
      `${API}/v1/chat/sessions/abc/proposals/p1/confirm`,
    ]);
    const other = await chatHandler(request("/api/chat/sessions/abc", { method: "POST", body: "{}" }), ctx(["sessions", "abc"]));
    expect(other.status).toBe(404);
  });
});

describe("client IP and proxy secret", () => {
  const ip = { "x-forwarded-for": "203.0.113.9, 10.0.0.1" };
  const staff = { ...ip, cookie: `staff_session=${TOKEN}` };

  it("sends X-Client-IP and X-Proxy-Secret on public, chat, staff and admin forwards", async () => {
    await publicHandler(request("/api/public/services", { headers: ip }), ctx(["services"]));
    await chatHandler(request("/api/chat/sessions", { method: "POST", headers: ip, body: "{}" }), ctx(["sessions"]));
    await staffHandler(request("/api/staff/me", { headers: staff }), ctx(["me"]));
    await adminHandler(request("/api/admin/clinic", { headers: staff }), ctx(["clinic"]));
    expect(calls).toHaveLength(4);
    for (let i = 0; i < calls.length; i++) {
      expect(upstreamHeaders(i).get("x-client-ip")).toBe("203.0.113.9");
      expect(upstreamHeaders(i).get("x-proxy-secret")).toBe(PROXY_SECRET);
    }
  });

  it("falls back to x-real-ip for X-Client-IP", async () => {
    await publicHandler(request("/api/public/services", { headers: { "x-real-ip": "198.51.100.7" } }), ctx(["services"]));
    expect(upstreamHeaders().get("x-client-ip")).toBe("198.51.100.7");
  });

  it("sends the secret on login and logout too, and never returns it", async () => {
    respond = () => json({ token: TOKEN, user: { id: "u1", email: "a@b.example", role: "admin" } });
    const response = await login(
      request("/api/staff/login", { method: "POST", headers: ip, body: JSON.stringify({ email: "a@b.example", password: "pw" }) }),
    );
    expect(upstreamHeaders().get("x-proxy-secret")).toBe(PROXY_SECRET);
    expect(upstreamHeaders().get("x-client-ip")).toBe("203.0.113.9");
    expect(await response.text()).not.toContain(PROXY_SECRET);
    expect(JSON.stringify([...response.headers])).not.toContain(PROXY_SECRET);
  });

  it("does not echo the secret in error responses", async () => {
    respond = () => json({ detail: "bad" }, 400);
    const response = await publicHandler(request("/api/public/services"), ctx(["services"]));
    expect(await response.text()).not.toContain(PROXY_SECRET);
  });

  it("omits the secret in mock mode and when unset", () => {
    expect(readConfig({ PUBLIC_PROXY_SECRET: PROXY_SECRET } as unknown as NodeJS.ProcessEnv).proxySecret).toBeNull();
    expect(readConfig({ API_BASE_URL: API } as unknown as NodeJS.ProcessEnv).proxySecret).toBeNull();
    expect(readConfig({ API_BASE_URL: API, PUBLIC_PROXY_SECRET: PROXY_SECRET } as unknown as NodeJS.ProcessEnv).proxySecret).toBe(PROXY_SECRET);
    expect(() => readConfig({ API_BASE_URL: API, PUBLIC_PROXY_SECRET: "short" } as unknown as NodeJS.ProcessEnv)).toThrow(ConfigError);
  });
});

describe("errors and availability", () => {
  it("passes the envelope through and converts FastAPI detail", async () => {
    respond = () => json({ error: { code: "slot_unavailable", message: "That time was just taken." } }, 409);
    let response = await publicHandler(request("/api/public/bookings", { method: "POST", body: "{}" }), ctx(["bookings"]));
    expect(response.status).toBe(409);
    expect(await response.json()).toEqual({ error: { code: "slot_unavailable", message: "That time was just taken." } });

    respond = () => json({ detail: "Doctor is inactive" }, 409);
    response = await publicHandler(request("/api/public/bookings", { method: "POST", body: "{}" }), ctx(["bookings"]));
    expect((await response.json()).error.message).toBe("Doctor is inactive");
  });

  it("never shows 5xx details", () => {
    const body = normaliseError(500, { detail: "psycopg.OperationalError at db-1" });
    expect(body.error.code).toBe("server_error");
    expect(body.error.message).not.toMatch(/psycopg/);
  });

  it("summarises validation errors by field", () => {
    const body = normaliseError(422, { detail: [{ loc: ["body", "phone"], msg: "bad" }] });
    expect(body).toEqual({ error: { code: "validation_error", message: "Some values are invalid: phone." } });
  });

  it("maps network failure and timeouts to a friendly 503", async () => {
    respond = () => {
      throw new TypeError("fetch failed");
    };
    let response = await publicHandler(request("/api/public/services"), ctx(["services"]));
    expect(response.status).toBe(503);
    expect((await response.json()).error.code).toBe("upstream_unavailable");

    respond = () => {
      throw new DOMException("The operation timed out.", "TimeoutError");
    };
    response = await publicHandler(request("/api/public/services"), ctx(["services"]));
    expect(response.status).toBe(503);
    expect((await response.json()).error.code).toBe("upstream_timeout");
  });

  it("puts a timeout signal on every upstream call", async () => {
    await publicHandler(request("/api/public/services"), ctx(["services"]));
    expect(calls[0]!.init.signal).toBeInstanceOf(AbortSignal);
    expect(calls[0]!.init.redirect).toBe("manual");
  });

  it("rejects non-JSON and oversized bodies", async () => {
    let response = await publicHandler(
      request("/api/public/bookings", { method: "POST", headers: { "content-type": "text/plain" }, body: "hi" }),
      ctx(["bookings"]),
    );
    expect(response.status).toBe(415);
    response = await publicHandler(
      request("/api/public/bookings", { method: "POST", body: JSON.stringify({ x: "a".repeat(40_000) }) }),
      ctx(["bookings"]),
    );
    expect(response.status).toBe(413);
    expect(calls).toHaveLength(0);
  });
});

describe("request helpers", () => {
  const post = (headers: Record<string, string>) => new Request(`${SITE}/api/x`, { method: "POST", headers: { host: "localhost:3000", ...headers } });

  it("judges same-origin writes by Origin, then Sec-Fetch-Site", () => {
    expect(isSameOriginWrite(post({ origin: SITE }))).toBe(true);
    expect(isSameOriginWrite(post({ origin: "https://evil.example" }))).toBe(false);
    expect(isSameOriginWrite(post({ origin: "null" }))).toBe(false);
    expect(isSameOriginWrite(post({ "sec-fetch-site": "same-origin" }))).toBe(true);
    expect(isSameOriginWrite(post({ "sec-fetch-site": "cross-site" }))).toBe(false);
    expect(isSameOriginWrite(post({}))).toBe(false);
    expect(isSameOriginWrite(new Request(`${SITE}/api/x`))).toBe(true);
    expect(isSameOriginWrite(post({ origin: "https://www.clinic.example" }), ["https://www.clinic.example"])).toBe(true);
  });

  it("reads only a well-formed session cookie", () => {
    expect(readSessionToken(new Headers({ cookie: `a=1; staff_session=${TOKEN}` }))).toBe(TOKEN);
    expect(readSessionToken(new Headers({ cookie: "staff_session=bad value;" }))).toBeNull();
    expect(readSessionToken(new Headers())).toBeNull();
  });

  it("forwards only an IP literal from the first hop", () => {
    expect(clientIp(new Headers({ "x-forwarded-for": "198.51.100.4, 10.0.0.1" }))).toBe("198.51.100.4");
    expect(clientIp(new Headers({ "x-forwarded-for": "<script>", "x-real-ip": "2001:db8::1" }))).toBe("2001:db8::1");
    expect(clientIp(new Headers())).toBeNull();
  });

  it("validates path segments and queries", () => {
    expect(safeSegments(["sessions", "abc_1-2"])).toEqual(["sessions", "abc_1-2"]);
    expect(safeSegments([".."])).toBeNull();
    expect(safeSegments(["a.b"])).toBeNull();
    expect(safeSegments([])).toBeNull();
    expect(pickQuery(new URL("http://x/?a=1&b=2&a=3"), ["a"])).toBe("?a=1");
  });
});

describe("configuration", () => {
  it("uses mock mode only outside production without API_BASE_URL", () => {
    expect(readConfig({ NODE_ENV: "test" } as NodeJS.ProcessEnv).mock).toBe(true);
    expect(readConfig({ API_BASE_URL: "https://api.example.test/" } as unknown as NodeJS.ProcessEnv)).toMatchObject({
      apiBaseUrl: "https://api.example.test",
      mock: false,
      slug: "aster-dental",
    });
  });

  it("fails clearly in production without API_BASE_URL", () => {
    vi.stubEnv("NODE_ENV", "production");
    expect(() => readConfig({} as NodeJS.ProcessEnv)).toThrow(ConfigError);
    expect(() => readConfig({ API_BASE_URL: "http://api.example.test" } as unknown as NodeJS.ProcessEnv)).toThrow(/https/);
  });

  it("rejects a malformed slug", () => {
    expect(() => readConfig({ CLINIC_SLUG: "../admin" } as unknown as NodeJS.ProcessEnv)).toThrow(ConfigError);
  });
});

describe("dev mock API", () => {
  it("serves fixtures through the same handlers when API_BASE_URL is unset", async () => {
    vi.stubEnv("API_BASE_URL", "");
    const response = await publicHandler(request("/api/public/services"), ctx(["services"]));
    expect(response.status).toBe(200);
    const services = await response.json();
    expect(Array.isArray(services)).toBe(true);
    expect(services.length).toBeGreaterThan(0);
    expect(calls).toHaveLength(0);
  });
});
