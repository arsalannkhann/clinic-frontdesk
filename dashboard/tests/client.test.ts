import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient, buildUrl } from "../src/api/client";
import {
  ApiError,
  LOGIN_FAILED_MESSAGE,
  NETWORK_MESSAGE,
  SESSION_ENDED_MESSAGE,
  messageFor,
} from "../src/api/errors";
import { endSession, sessionToken, sessionUser, startSession } from "../src/session";

const TOKEN = `cvs_${"a".repeat(32)}.${"B".repeat(43)}`;
const USER = { id: "00000000-0000-4000-8000-000000000009", email: "o@example.test", role: "owner" } as const;

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

interface Call {
  url: string;
  init: RequestInit;
}

function harness(responses: Response[], token: string | null = TOKEN) {
  const calls: Call[] = [];
  const onUnauthorized = vi.fn();
  const fetchImpl = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(url), init: init ?? {} });
    const next = responses.shift();
    if (!next) throw new TypeError("no response queued");
    return next;
  });
  const client = new ApiClient({
    baseUrl: "https://api.clinic.example/",
    getToken: () => token,
    onUnauthorized,
    fetchImpl: fetchImpl as unknown as typeof fetch,
  });
  return { client, calls, onUnauthorized };
}

function headersOf(call: Call | undefined): Record<string, string> {
  return (call?.init.headers ?? {}) as Record<string, string>;
}

afterEach(() => endSession());

describe("ApiClient", () => {
  it("attaches the bearer token in the Authorization header only", async () => {
    const { client, calls } = harness([json(200, [])]);
    await client.request("get", "/v1/admin/appointments", {
      query: { status: "needs_review", limit: 50, from: null },
    });
    const call = calls[0];
    expect(headersOf(call)["Authorization"]).toBe(`Bearer ${TOKEN}`);
    expect(call?.url).toBe(
      "https://api.clinic.example/v1/admin/appointments?status=needs_review&limit=50",
    );
    expect(call?.url).not.toContain(TOKEN);
    expect(call?.url).not.toContain("cvs_");
    expect(call?.init.credentials).toBe("omit");
    expect(call?.init.referrerPolicy).toBe("no-referrer");
  });

  it("never puts the token in the URL, even for path parameters and bodies", async () => {
    const { client, calls } = harness([json(200, { id: "x" }), new Response(null, { status: 204 })]);
    await client.request("patch", "/v1/admin/doctors/{doctor_id}", {
      params: { doctor_id: "a/b?c" },
      body: { name: "Dr. A" },
    });
    await client.request("post", "/v1/staff/logout");
    expect(calls[0]?.url).toBe("https://api.clinic.example/v1/admin/doctors/a%2Fb%3Fc");
    expect(calls[0]?.init.body).toBe(JSON.stringify({ name: "Dr. A" }));
    expect(headersOf(calls[0])["Content-Type"]).toBe("application/json");
    for (const call of calls) {
      expect(call.url).not.toContain(TOKEN);
      expect(String(call.init.body ?? "")).not.toContain(TOKEN);
    }
  });

  it("sends no Authorization header without a session", async () => {
    const { client, calls } = harness(
      [json(200, { token: TOKEN, expires_at: "2099-01-01T00:00:00Z", user: USER })],
      null,
    );
    await client.request("post", "/v1/staff/login", {
      body: { email: "o@example.test", password: "secret-password" },
    });
    expect(headersOf(calls[0])["Authorization"]).toBeUndefined();
  });

  it("returns undefined for 204 responses", async () => {
    const { client } = harness([new Response(null, { status: 204 })]);
    await expect(
      client.request("delete", "/v1/admin/exceptions/{exception_id}", {
        params: { exception_id: "e1" },
      }),
    ).resolves.toBeUndefined();
  });

  it("on 401 with a token, ends the session and reports a session-ended message", async () => {
    startSession({ token: TOKEN, expires_at: "2099-01-01T00:00:00Z", user: USER });
    const onUnauthorized = vi.fn(endSession);
    const client = new ApiClient({
      baseUrl: "",
      getToken: sessionToken,
      onUnauthorized,
      fetchImpl: (async () => json(401, { detail: "Invalid credentials" })) as typeof fetch,
    });
    const error = await client.request("get", "/v1/staff/me").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(401);
    expect((error as ApiError).message).toBe(SESSION_ENDED_MESSAGE);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(sessionToken()).toBeNull();
    expect(sessionUser()).toBeNull();
  });

  it("a failed login (401 without a token) does not trigger the session handler", async () => {
    const { client, onUnauthorized } = harness([json(401, { detail: "Invalid email or password" })], null);
    const error = await client
      .request("post", "/v1/staff/login", { body: { email: "a@b.co", password: "x" } })
      .catch((e: unknown) => e);
    expect((error as ApiError).message).toBe(LOGIN_FAILED_MESSAGE);
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it("maps network failures to a safe message", async () => {
    const { client } = harness([]);
    const error = await client.request("get", "/v1/admin/clinic").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(0);
    expect((error as ApiError).message).toBe(NETWORK_MESSAGE);
  });

  it("does not surface raw or HTML response bodies", async () => {
    const html = "<html><body><script>alert(1)</script>Internal stack trace</body></html>";
    const { client } = harness([
      new Response(html, { status: 500, headers: { "Content-Type": "text/html" } }),
      json(503, { detail: "<img src=x onerror=alert(1)>" }),
      json(403, { detail: "Insufficient role" }),
    ]);
    const messages: string[] = [];
    for (const path of ["/v1/admin/clinic", "/v1/admin/clinic", "/v1/admin/clinic"] as const) {
      messages.push(
        await client.request("get", path).then(
          () => "resolved",
          (e: ApiError) => e.message,
        ),
      );
    }
    expect(messages[0]).toBe("The server had a problem. Try again later.");
    expect(messages[1]).toBe("The service is temporarily unavailable. Try again later.");
    expect(messages[2]).toBe("Your role does not allow this action.");
    for (const message of messages) expect(message).not.toMatch(/[<>]/);
  });
});

describe("messageFor", () => {
  it("shows short backend details for conflicts and validation", () => {
    expect(messageFor(409, { detail: "A provisioning step is already pending" }, true)).toBe(
      "A provisioning step is already pending",
    );
    expect(messageFor(422, { detail: "to must be after from" }, true)).toBe("to must be after from");
  });

  it("lists field names, never submitted values, for 422 arrays", () => {
    const body = {
      detail: [
        { loc: ["body", "timezone"], type: "value_error", input: "Mars/Olympus" },
        { loc: ["body", "name"], type: "missing" },
        { loc: ["body", "<script>"], type: "x" },
      ],
    };
    const message = messageFor(422, body, true);
    expect(message).toBe("Some values are invalid: timezone, name.");
    expect(message).not.toContain("Mars");
  });

  it("drops overlong or non-string details and strips control characters", () => {
    expect(messageFor(409, { detail: "x".repeat(500) }, true)).toBe(
      "This action conflicts with the current state. Refresh and try again.",
    );
    expect(messageFor(400, { detail: { nested: true } }, true)).toBe("The request was rejected.");
    expect(messageFor(409, { detail: "line1\nline2" }, true)).toBe("line1 line2");
  });

  it("uses fixed messages for other statuses", () => {
    expect(messageFor(404, { detail: "secret detail" }, true)).not.toContain("secret");
    expect(messageFor(418, null, true)).toBe("The request failed.");
    expect(messageFor(401, null, true)).toBe(SESSION_ENDED_MESSAGE);
    expect(messageFor(401, null, false)).toBe(LOGIN_FAILED_MESSAGE);
  });
});

describe("buildUrl", () => {
  it("encodes parameters and drops empty query values", () => {
    expect(buildUrl("", "/v1/x/{id}", { id: "1 2" }, { a: "", b: null, c: 0, d: false })).toBe(
      "/v1/x/1%202?c=0&d=false",
    );
  });

  it("refuses a missing path parameter", () => {
    expect(() => buildUrl("", "/v1/x/{id}", {})).toThrow(/id/);
  });
});

describe("session", () => {
  it("is held in memory only and expires", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2030-01-01T00:00:00Z"));
      startSession({ token: TOKEN, expires_at: "2030-01-01T00:10:00Z", user: USER });
      expect(sessionToken()).toBe(TOKEN);
      expect(window.localStorage.length).toBe(0);
      expect(window.sessionStorage.length).toBe(0);
      expect(document.cookie).toBe("");
      expect(window.location.href).not.toContain(TOKEN);
      vi.advanceTimersByTime(10 * 60 * 1000);
      expect(sessionToken()).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});
