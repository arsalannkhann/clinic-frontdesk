// @vitest-environment node
import { beforeEach, describe, expect, it } from "vitest";

import type { UpstreamRequest } from "@/lib/server/upstream";
import { istDate, MOCK_CALL, mockUpstream, resetMockState } from "@/lib/server/mock/router";
import { DOCTORS, MOCK_PASSWORD, SERVICES } from "@/lib/server/mock/fixtures";

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
    const saved = await call("PATCH", "/v1/admin/clinic", { token: owner, body: JSON.stringify({ public_tagline: "New line", public_chat_enabled: false }) });
    expect(saved.status).toBe(200);
    const profile = (await call("GET", "/v1/public/clinics/aster-dental")).body as { tagline: string; chat_enabled: boolean };
    expect(profile.tagline).toBe("New line");
    expect(profile.chat_enabled).toBe(false);
  });

  it("offers the web voice call and walks a scripted call to a booking by voice", async () => {
    const profile = (await call("GET", "/v1/public/clinics/aster-dental")).body as { voice_enabled: boolean };
    expect(profile.voice_enabled).toBe(true);
    const created = await call("POST", "/v1/chat/clinics/aster-dental/sessions", { body: "{}" });
    const id = (created.body as { session_id: string }).session_id;
    const greeting = await call("POST", `/v1/chat/sessions/${id}/voice/greeting`);
    expect(greeting.status).toBe(200);
    expect(greeting.body).toMatchObject({ audio: null, audio_mime: "audio/mpeg" });
    expect((greeting.body as { text: string }).text).toBe("Hello, Aster Dental Studio. I'm the AI receptionist. How can I help you today?");

    type Turn = {
      transcript: string;
      audio: null;
      proposal: { id: string; starts_at: string; doctor_name: string } | null;
      messages: { role: string; text: string }[];
      quick_replies: string[];
      booking: { reference: string; status: string; starts_at: string; doctor_name: string; service_name: string } | null;
    };
    const audio = "UklGR".padEnd(8000, "A");
    const say = async () => {
      const turn = await call("POST", `/v1/chat/sessions/${id}/voice`, { body: JSON.stringify({ audio }) });
      expect(turn.status).toBe(200);
      return turn.body as Turn;
    };
    const spoken = (turn: Turn) => turn.messages.map((m) => m.text).join(" ");

    // Asking to book offers two real open times, spoken, with nothing held yet.
    const ask = await say();
    expect(ask.transcript).toBe(MOCK_CALL[0]);
    expect(ask.audio).toBeNull();
    expect(ask.proposal).toBeNull();
    expect(ask.booking).toBeNull();
    expect(spoken(ask)).toMatch(/I have two times for a check-up and cleaning: .+ with Dr\. .+, or .+ with Dr\. .+\. Which would you like\?/);

    // Choosing one holds it (the proposal), then name, number and consent are asked aloud.
    const pick = await say();
    expect(pick.transcript).toBe(MOCK_CALL[1]);
    expect(pick.proposal?.id).toBeTruthy();
    expect(spoken(pick)).toMatch(/full name/);
    const name = await say();
    expect(name.transcript).toBe("My name is Asha Rao");
    expect(spoken(name)).toMatch(/mobile number/);
    const number = await say();
    expect(spoken(number)).toMatch(/98765 43210/);
    expect(spoken(number)).toMatch(/may the clinic contact you/);
    expect(number.booking).toBeNull();
    // Nothing is booked before consent: the held time is still open.
    const held = Date.parse(pick.proposal!.starts_at);
    const open = async () => {
      const date = istDate(held);
      const result = await call("GET", "/v1/public/clinics/aster-dental/slots", { search: `?service_id=${SERVICES[0]!.id}&date=${date}&days=1` });
      // Another doctor may be free at the same time, so the slot is matched by doctor too.
      const doctor = DOCTORS.find((d) => d.name === pick.proposal!.doctor_name)!.id;
      return (result.body as { slots: { starts_at: string; doctor_id: string }[] }).slots.some(
        (slot) => Date.parse(slot.starts_at) === held && slot.doctor_id === doctor,
      );
    };
    expect(await open()).toBe(true);

    const yes = await say();
    expect(yes.transcript).toBe(MOCK_CALL[4]);
    expect(yes.proposal).toBeNull();
    expect(yes.booking).toMatchObject({ status: "confirmed", service_name: "Check-up and cleaning" });
    expect(yes.booking!.starts_at).toBe(pick.proposal!.starts_at);
    expect(spoken(yes)).toMatch(/You're booked for .+ Your reference is .+ Is there anything else/);
    for (const turn of [ask, pick, name, number, yes]) expect(spoken(turn)).not.toMatch(/card|screen|tap|below|button/i);

    // The booking is real in the mock: the held slot is no longer open and the reference looks up.
    expect(await open()).toBe(false);
    const lookup = await call("POST", "/v1/public/clinics/aster-dental/bookings/lookup", {
      body: JSON.stringify({ reference: yes.booking!.reference, phone: "+919876543210" }),
    });
    expect(lookup.status).toBe(200);
    expect(lookup.body).toMatchObject({ reference: yes.booking!.reference });

    // Later turns carry on without booking again.
    const after = await say();
    expect(after.booking).toBeNull();
    expect(spoken(after)).toMatch(/anything else/);
    expect((await say()).booking).toBeNull();

    // The heard lines are in the transcript like typed ones; the booking note is hidden.
    const transcript = (await call("GET", `/v1/chat/sessions/${id}`)).body as { messages: { role: string; text: string }[] };
    expect(transcript.messages[1]).toEqual({ role: "user", text: MOCK_CALL[0] });
    expect(transcript.messages.some((m) => m.role === "system")).toBe(false);

    const short = await call("POST", `/v1/chat/sessions/${id}/voice`, { body: JSON.stringify({ audio: "UklGRg==" }) });
    expect(short.status).toBe(422);
    expect(short.body).toMatchObject({ error: { code: "no_speech" } });
    expect((await call("POST", `/v1/chat/sessions/${id}/voice`, { body: "{}" })).status).toBe(422);
    expect((await call("POST", "/v1/chat/sessions/0123456789abcdef0123456789abcdef/voice/greeting")).status).toBe(404);
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

  it("books from chat only with consent, and hides the booking note from the transcript", async () => {
    const created = await call("POST", "/v1/chat/clinics/aster-dental/sessions", { body: "{}" });
    const id = (created.body as { session_id: string }).session_id;
    const turn = await call("POST", `/v1/chat/sessions/${id}/messages`, { body: JSON.stringify({ text: "I'd like a check-up" }) });
    const proposal = (turn.body as { proposal: { id: string } }).proposal;
    const confirm = (consent: boolean) =>
      call("POST", `/v1/chat/sessions/${id}/proposals/${proposal.id}/confirm`, {
        body: JSON.stringify({ name: "Ananya Krishnan", phone: "+919876543210", consent_contact: consent }),
      });
    const refused = await confirm(false);
    expect(refused.status).toBe(422);
    expect((refused.body as { error: { code: string } }).error.code).toBe("consent_required");
    const booked = await confirm(true);
    expect(booked.status).toBe(202);
    expect(Object.keys(booked.body as object).sort()).toEqual(["doctor_name", "reference", "service_name", "starts_at", "status"]);
    const used = await confirm(true);
    expect((used.body as { error: { code: string } }).error.code).toBe("proposal_used");
    const transcript = (await call("GET", `/v1/chat/sessions/${id}`)).body as { messages: { role: string }[]; proposal: unknown };
    expect(transcript.messages.some((m) => m.role === "system")).toBe(false);
    expect(transcript.proposal).toBeNull();
  });

  it("follows the call-back shapes: masked phone, hidden details for viewers, demo records not callable", async () => {
    const owner = await signIn("owner@asterdental.example");
    const viewer = await signIn("viewer@asterdental.example");
    expect((await call("PATCH", "/v1/admin/clinic", { token: owner, body: JSON.stringify({ outbound_enabled: true }) })).status).toBe(200);
    const items = (await call("GET", "/v1/staff/callbacks", { token: owner, search: "?status=open" })).body as {
      id: string;
      phone_masked: string | null;
      synthetic: boolean;
    }[];
    expect(items[0]!.phone_masked).toMatch(/^\*{4}\d{4}$/);
    const demo = items.find((item) => item.synthetic)!;
    const refused = await call("POST", `/v1/staff/callbacks/${demo.id}/call`, { token: owner, body: "{}" });
    expect(refused.status).toBe(409);
    expect((refused.body as { error: { code: string } }).error.code).toBe("synthetic_contact");
    const hidden = (await call("GET", "/v1/staff/callbacks", { token: viewer, search: "?status=open" })).body as Record<string, unknown>[];
    expect(hidden[0]).toMatchObject({ name: null, phone_masked: null, reason: null });
    const resolved = await call("POST", `/v1/staff/callbacks/${demo.id}/resolve`, { token: owner, body: JSON.stringify({ status: "done" }) });
    expect(Object.keys(resolved.body as object).sort()).toEqual(["id", "resolved_at", "status"]);
    expect((await call("POST", "/v1/staff/callbacks/missing/call", { token: owner, body: "{}" })).body).toEqual({ detail: expect.any(String) });
  });

  it("refuses null and badly formed public profile fields", async () => {
    const owner = await signIn("owner@asterdental.example");
    const patch = (body: object) => call("PATCH", "/v1/admin/clinic", { token: owner, body: JSON.stringify(body) });
    expect((await patch({ public_tagline: null })).status).toBe(422);
    expect((await patch({ public_map_url: "http://maps.example" })).status).toBe(422);
    expect((await patch({ public_voice_number: "98000 00001" })).status).toBe(422);
    expect((await patch({ public_slug: "other" })).status).toBe(422);
  });

  it("reports the call outcome and reopens a called request without dialling", async () => {
    const owner = await signIn("owner@asterdental.example");
    const viewer = await signIn("viewer@asterdental.example");
    expect((await call("PATCH", "/v1/admin/clinic", { token: owner, body: JSON.stringify({ outbound_enabled: true }) })).status).toBe(200);
    const open = (await call("GET", "/v1/staff/callbacks", { token: owner, search: "?status=open" })).body as { id: string; synthetic: boolean; call_outcome: string | null }[];
    const live = open.find((item) => !item.synthetic)!;
    expect(live.call_outcome).toBeNull();
    const reopen = (id: string, token = owner) => call("POST", `/v1/staff/callbacks/${id}/reopen`, { token, body: "{}" });
    // An open request is returned unchanged.
    expect((await reopen(live.id)).body).toEqual({ id: live.id, status: "open" });

    expect((await call("POST", `/v1/staff/callbacks/${live.id}/call`, { token: owner, body: "{}" })).status).toBe(202);
    const calling = (await call("GET", "/v1/staff/callbacks", { token: owner, search: "?status=calling" })).body as { id: string; call_outcome: string }[];
    expect(calling.find((item) => item.id === live.id)?.call_outcome).toBe("placed");

    expect((await reopen(live.id, viewer)).status).toBe(403);
    const reopened = await reopen(live.id);
    expect(reopened.status).toBe(200);
    expect(reopened.body).toEqual({ id: live.id, status: "open" });
    const again = (await call("GET", "/v1/staff/callbacks", { token: owner, search: "?status=open" })).body as Record<string, unknown>[];
    expect(again.find((item) => item.id === live.id)).toMatchObject({ status: "open", call_outcome: null, call_requested_at: null, execution_id: null });

    await call("POST", `/v1/staff/callbacks/${live.id}/resolve`, { token: owner, body: JSON.stringify({ status: "done" }) });
    expect(((await reopen(live.id)).body as { error: { code: string } }).error.code).toBe("callback_resolved");
    expect((await reopen("0b5d1c1e-7a52-4f0e-9a51-3c1a8f0d0999")).status).toBe(404);
  });

  it("says whether the calendar read was truncated", async () => {
    const owner = await signIn("owner@asterdental.example");
    const from = new Date(Date.now() - 3 * 86_400_000).toISOString();
    const to = new Date(Date.now() + 7 * 86_400_000).toISOString();
    const result = await call("GET", "/v1/staff/calendar", { token: owner, search: `?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}` });
    expect(result.status).toBe(200);
    expect((result.body as { truncated: boolean }).truncated).toBe(false);
  });

  it("refuses a chat confirm of a deactivated service as slot_unavailable", async () => {
    const owner = await signIn("owner@asterdental.example");
    const created = await call("POST", "/v1/chat/clinics/aster-dental/sessions", { body: "{}" });
    const id = (created.body as { session_id: string }).session_id;
    const turn = await call("POST", `/v1/chat/sessions/${id}/messages`, { body: JSON.stringify({ text: "I'd like a check-up" }) });
    const proposal = (turn.body as { proposal: { id: string; doctor_name: string; service_name: string } }).proposal;
    const doctors = (await call("GET", "/v1/admin/doctors", { token: owner })).body as { id: string; name: string }[];
    const doctor = doctors.find((d) => d.name === proposal.doctor_name)!;
    const services = (await call("GET", "/v1/admin/services", { token: owner })).body as { id: string; doctor_id: string; name: string }[];
    const service = services.find((s) => s.doctor_id === doctor.id && s.name === proposal.service_name)!;
    expect((await call("PATCH", `/v1/admin/services/${service.id}`, { token: owner, body: JSON.stringify({ active: false }) })).status).toBe(200);
    const confirm = await call("POST", `/v1/chat/sessions/${id}/proposals/${proposal.id}/confirm`, {
      body: JSON.stringify({ name: "Ananya Krishnan", phone: "+919876543210", consent_contact: true }),
    });
    expect(confirm.status).toBe(409);
    expect((confirm.body as { error: { code: string } }).error.code).toBe("slot_unavailable");
  });
});
