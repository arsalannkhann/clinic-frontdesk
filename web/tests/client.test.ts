import { describe, expect, it, vi } from "vitest";

import { api, ApiError, errorMessage, newIdempotencyKey, NETWORK_MESSAGE, onUnauthorized, withQuery } from "@/lib/api/client";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

describe("api()", () => {
  it("only calls same-origin /api routes", async () => {
    await expect(api("https://evil.example/api/x")).rejects.toThrow(/same-origin/);
    await expect(api("/v1/public")).rejects.toThrow(/same-origin/);
  });

  it("sends JSON with same-origin credentials and the idempotency key", async () => {
    const fetchImpl = vi.fn(async () => json({ reference: "ABCD1234" }, 201));
    const result = await api<{ reference: string }>("/api/public/bookings", {
      body: { a: 1 },
      idempotencyKey: "web-0123456789",
      fetchImpl,
    });
    expect(result.reference).toBe("ABCD1234");
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/public/bookings");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("same-origin");
    expect(init.body).toBe('{"a":1}');
    expect((init.headers as Record<string, string>)["Idempotency-Key"]).toBe("web-0123456789");
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined();
  });

  it("raises the error envelope as ApiError", async () => {
    const fetchImpl = vi.fn(async () => json({ error: { code: "slot_unavailable", message: "Taken." } }, 409));
    const error = await api("/api/public/bookings", { body: {}, fetchImpl }).catch((e) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 409, code: "slot_unavailable", message: "Taken." });
  });

  it("maps a network failure to a friendly error", async () => {
    const fetchImpl = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    });
    const error = await api("/api/public", { fetchImpl }).catch((e) => e);
    expect(error).toMatchObject({ status: 0, code: "network", message: NETWORK_MESSAGE });
  });

  it("gives a generic message for a non-JSON 503", async () => {
    const fetchImpl = vi.fn(async () => new Response("<html>", { status: 503 }));
    const error = await api("/api/public", { fetchImpl }).catch((e) => e);
    expect(error).toMatchObject({ status: 503, code: "unavailable" });
  });

  it("notifies listeners when a staff session ends, but not for public routes or login", async () => {
    const listener = vi.fn();
    const off = onUnauthorized(listener);
    const fetchImpl = vi.fn(async () => json({ error: { code: "unauthorized", message: "Sign in." } }, 401));
    await api("/api/staff/me", { fetchImpl }).catch(() => undefined);
    await api("/api/admin/clinic", { fetchImpl }).catch(() => undefined);
    await api("/api/public/bookings/lookup", { body: {}, fetchImpl }).catch(() => undefined);
    await api("/api/staff/login", { body: {}, fetchImpl }).catch(() => undefined);
    expect(listener).toHaveBeenCalledTimes(2);
    off();
  });

  it("returns undefined for 204", async () => {
    const fetchImpl = vi.fn(async () => new Response(null, { status: 204 }));
    await expect(api("/api/admin/exceptions/x", { method: "DELETE", fetchImpl })).resolves.toBeUndefined();
  });
});

describe("helpers", () => {
  it("builds query strings, skipping empty values", () => {
    expect(withQuery("/api/staff/callbacks", { status: "open", channel: undefined, limit: 5, x: "" })).toBe(
      "/api/staff/callbacks?status=open&limit=5",
    );
    expect(withQuery("/api/x?a=1", { b: 2 })).toBe("/api/x?a=1&b=2");
    expect(withQuery("/api/x")).toBe("/api/x");
  });

  it("makes distinct, well-formed idempotency keys", () => {
    const a = newIdempotencyKey("web");
    const b = newIdempotencyKey("web");
    expect(a).toMatch(/^web-[0-9a-f]{24}$/);
    expect(a).not.toBe(b);
  });

  it("shows only safe messages", () => {
    expect(errorMessage(new ApiError(409, "x", "Taken."))).toBe("Taken.");
    expect(errorMessage(new Error("TypeError: secret internals"))).toBe("Something went wrong. Please try again.");
  });
});
