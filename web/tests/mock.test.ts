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

  it("never places an outbound call", async () => {
    const owner = await signIn("owner@asterdental.example");
    const list = await call("GET", "/v1/staff/callbacks", { token: owner, search: "?status=open" });
    const first = (list.body as { id: string }[])[0]!;
    const result = await call("POST", `/v1/staff/callbacks/${first.id}/call`, { token: owner, body: "{}" });
    expect(result.status).toBe(409);
  });
});
