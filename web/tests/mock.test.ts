// @vitest-environment node
import { beforeEach, describe, expect, it } from "vitest";

import type { UpstreamRequest } from "@/lib/server/upstream";
import { mockUpstream, resetMockState } from "@/lib/server/mock/router";
import { MOCK_PASSWORD } from "@/lib/server/mock/fixtures";

const call = (method: string, path: string, opts: Partial<UpstreamRequest> = {}) =>
  mockUpstream({ method, path, search: "", body: null, token: null, ip: null, idempotencyKey: null, ...opts });

async function signIn(email: string): Promise<string> {
  const result = await call("POST", "/v1/staff/login", { body: JSON.stringify({ email, password: MOCK_PASSWORD }) });
  expect(result.status).toBe(200);
  return (result.body as { token: string }).token;
}

beforeEach(() => resetMockState());

describe("dev mock API", () => {
  it("serves the public clinic and refuses another slug", async () => {
    expect((await call("GET", "/v1/public/clinics/aster-dental")).status).toBe(200);
    expect((await call("GET", "/v1/public/clinics/other")).status).toBe(404);
  });

  it("requires a session for staff routes", async () => {
    expect((await call("GET", "/v1/staff/me")).status).toBe(401);
    const token = await signIn("owner@asterdental.example");
    expect((await call("GET", "/v1/staff/me", { token })).status).toBe(200);
  });

  it("keeps viewers read-only and owner-only fields to the owner", async () => {
    const viewer = await signIn("viewer@asterdental.example");
    const admin = await signIn("admin@asterdental.example");
    expect((await call("PATCH", "/v1/admin/clinic", { token: viewer, body: JSON.stringify({ horizon_days: 30 }) })).status).toBe(403);
    expect((await call("PATCH", "/v1/admin/clinic", { token: admin, body: JSON.stringify({ scripts_approved: false }) })).status).toBe(403);
    expect((await call("PATCH", "/v1/admin/clinic", { token: admin, body: JSON.stringify({ horizon_days: 30 }) })).status).toBe(200);
  });

  it("refuses Call now while outbound calling is off, and reports readiness", async () => {
    const owner = await signIn("owner@asterdental.example");
    const admin = await signIn("admin@asterdental.example");
    const list = await call("GET", "/v1/staff/callbacks", { token: owner, search: "?status=open" });
    const first = (list.body as { id: string }[])[0]!;
    const refused = await call("POST", `/v1/staff/callbacks/${first.id}/call`, { token: owner, body: "{}" });
    expect(refused.status).toBe(409);
    expect((refused.body as { error: { code: string } }).error.code).toBe("outbound_disabled");
    expect((await call("GET", "/v1/staff/outbound-status", { token: owner })).body).toEqual({
      enabled: false,
      has_from_number: true,
      has_outbound_agent: true,
    });
    // outbound_enabled is owner-only.
    expect((await call("PATCH", "/v1/admin/clinic", { token: admin, body: JSON.stringify({ outbound_enabled: true }) })).status).toBe(403);
    expect((await call("PATCH", "/v1/admin/clinic", { token: owner, body: JSON.stringify({ outbound_enabled: true }) })).status).toBe(200);
    const started = await call("POST", `/v1/staff/callbacks/${first.id}/call`, { token: owner, body: "{}" });
    expect(started.status).toBe(202);
    expect((started.body as { status: string }).status).toBe("calling");
  });

  it("saves the public profile through the admin clinic route", async () => {
    const owner = await signIn("owner@asterdental.example");
    const saved = await call("PATCH", "/v1/admin/clinic", { token: owner, body: JSON.stringify({ tagline: "New line", chat_enabled: false }) });
    expect(saved.status).toBe(200);
    const profile = (await call("GET", "/v1/public/clinics/aster-dental")).body as { tagline: string; chat_enabled: boolean };
    expect(profile.tagline).toBe("New line");
    expect(profile.chat_enabled).toBe(false);
  });

  it("returns a chat transcript for a reload, and 404 for an unknown session", async () => {
    const created = await call("POST", "/v1/chat/clinics/aster-dental/sessions", { body: "{}" });
    const id = (created.body as { session_id: string }).session_id;
    await call("POST", `/v1/chat/sessions/${id}/messages`, { body: JSON.stringify({ text: "I'd like a check-up" }) });
    const transcript = await call("GET", `/v1/chat/sessions/${id}`);
    expect(transcript.status).toBe(200);
    const body = transcript.body as { messages: { role: string; text: string }[]; proposal: { id: string } | null; quick_replies: string[] };
    expect(body.messages.map((m) => m.role)).toEqual(["assistant", "user", "assistant"]);
    expect(body.messages[1]!.text).toBe("I'd like a check-up");
    expect(body.proposal?.id).toBeTruthy();
    expect(body.quick_replies.length).toBeGreaterThan(0);
    expect((await call("GET", "/v1/chat/sessions/0123456789abcdef0123456789abcdef")).status).toBe(404);
  });
});
