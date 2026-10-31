import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatPanel } from "@/components/public/chat/ChatWidget";
import { ProposalCard } from "@/components/public/chat/ProposalCard";
import type { ChatProposal, ChatReply } from "@/lib/api/types";
import { chatReducer, FALLBACK_REPLIES, initialChat, readStoredSession, SESSION_KEY, storeSession, validMessage } from "@/lib/chat";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }) }));

const IST = "Asia/Kolkata";
const SESSION = "sess_0123456789abcdef";
const proposal = (id = "p1", minutes = 10): ChatProposal => ({
  id,
  doctor_name: "Dr. Kavya Iyer",
  service_name: "Check-up and cleaning",
  starts_at: "2026-11-03T05:00:00Z",
  expires_at: new Date(Date.now() + minutes * 60_000).toISOString(),
});
const reply = (text: string, p: ChatProposal | null = null, quick: string[] = []): ChatReply => ({
  messages: [{ role: "assistant", text }],
  proposal: p,
  quick_replies: quick,
});

describe("chat reducer", () => {
  const started = chatReducer(initialChat, {
    type: "started",
    session: { session_id: SESSION, greeting: "Hello", quick_replies: ["Book a check-up"] },
  });

  it("starts a session with the greeting and quick replies", () => {
    expect(started).toMatchObject({ status: "ready", sessionId: SESSION, quickReplies: ["Book a check-up"] });
    expect(started.lines.map((l) => l.text)).toEqual(["Hello"]);
  });

  it("shows typing while waiting and clears it on reply", () => {
    const sent = chatReducer(started, { type: "send", text: "Tomorrow?" });
    expect(sent).toMatchObject({ typing: true, quickReplies: [], pending: "Tomorrow?" });
    const replied = chatReducer(sent, { type: "reply", reply: reply("10:30 works", proposal()) });
    expect(replied).toMatchObject({ typing: false, proposalState: "open", pending: null });
    expect(replied.lines.at(-1)!.text).toBe("10:30 works");
  });

  it("only confirms an open proposal, and a gone proposal expires the card", () => {
    const open = chatReducer(started, { type: "reply", reply: reply("ok", proposal()) });
    const confirming = chatReducer(open, { type: "confirming" });
    expect(confirming.proposalState).toBe("confirming");
    expect(chatReducer(confirming, { type: "confirming" })).toBe(confirming);
    expect(chatReducer(confirming, { type: "confirmFailed", message: "Taken", code: "slot_unavailable" }).proposalState).toBe("expired");
    expect(chatReducer(confirming, { type: "confirmFailed", message: "Offline", code: "network" }).proposalState).toBe("open");
  });

  it("keeps a booked card when later replies arrive", () => {
    const open = chatReducer(started, { type: "reply", reply: reply("ok", proposal()) });
    const booked = chatReducer(open, {
      type: "confirmed",
      result: { reference: "ABCD2345", status: "confirmed", starts_at: "x", doctor_name: "d", service_name: "s" },
    });
    const later = chatReducer(booked, { type: "reply", reply: reply("Anything else?") });
    expect(later.proposalState).toBe("booked");
    expect(later.booking?.reference).toBe("ABCD2345");
  });

  it("falls back to options that work without the assistant", () => {
    expect(chatReducer(initialChat, { type: "startFailed", message: "x" }).quickReplies).toEqual(FALLBACK_REPLIES);
  });

  it("trims and caps messages", () => {
    expect(validMessage("   ")).toBeNull();
    expect(validMessage(" hi ")).toBe("hi");
    expect(validMessage("x".repeat(1200))).toHaveLength(1000);
  });

  it("never throws when storage is unavailable", () => {
    const spy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    expect(readStoredSession()).toBeNull();
    expect(() => storeSession(SESSION)).not.toThrow();
    spy.mockRestore();
    set.mockRestore();
  });
});

describe("proposal card", () => {
  it("requires a name and a valid +91 mobile before confirming", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(<ProposalCard proposal={proposal()} state="open" error={null} booking={null} timeZone={IST} onConfirm={onConfirm} onChange={vi.fn()} />);
    expect(screen.getByText(/Check-up and cleaning/)).toBeInTheDocument();
    expect(screen.getByText(/10:30\s?am/i)).toBeInTheDocument(); // clinic time, not browser time

    await user.click(screen.getByRole("button", { name: "Confirm booking" }));
    expect(onConfirm).not.toHaveBeenCalled();
    expect(screen.getByText(/full name/i)).toBeInTheDocument();

    await user.type(screen.getByLabelText("Patient name"), "Ananya Krishnan");
    await user.type(screen.getByLabelText("Mobile number"), "5876543210");
    await user.click(screen.getByRole("button", { name: "Confirm booking" }));
    expect(onConfirm).not.toHaveBeenCalled();
    expect(screen.getByText(/start with 6, 7, 8 or 9/)).toBeInTheDocument();

    await user.clear(screen.getByLabelText("Mobile number"));
    await user.type(screen.getByLabelText("Mobile number"), "+91 98765 43210");
    await user.click(screen.getByRole("button", { name: "Confirm booking" }));
    expect(onConfirm).toHaveBeenCalledWith({ name: "Ananya Krishnan", phone: "+919876543210", consent_contact: false });
  });

  it("passes consent only when ticked", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(<ProposalCard proposal={proposal()} state="open" error={null} booking={null} timeZone={IST} onConfirm={onConfirm} onChange={vi.fn()} />);
    await user.type(screen.getByLabelText("Patient name"), "Ananya Krishnan");
    await user.type(screen.getByLabelText("Mobile number"), "9876543210");
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "Confirm booking" }));
    expect(onConfirm).toHaveBeenCalledWith(expect.objectContaining({ consent_contact: true }));
  });

  it("offers Change without submitting", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const onConfirm = vi.fn();
    render(<ProposalCard proposal={proposal()} state="open" error={null} booking={null} timeZone={IST} onConfirm={onConfirm} onChange={onChange} />);
    await user.click(screen.getByRole("button", { name: "Change" }));
    expect(onChange).toHaveBeenCalledOnce();
    expect(onConfirm).not.toHaveBeenCalled();
  });
});

describe("chat panel flow", () => {
  type Call = { url: string; init: RequestInit };
  let calls: Call[];
  let handlers: ((call: Call) => Response | undefined)[];

  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

  beforeEach(() => {
    calls = [];
    handlers = [];
    window.sessionStorage.clear();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit) => {
        const call = { url, init };
        calls.push(call);
        for (const handler of handlers) {
          const response = handler(call);
          if (response) return response;
        }
        return json({ error: { code: "not_found", message: "Not found" } }, 404);
      }),
    );
  });
  afterEach(() => vi.unstubAllGlobals());

  const body = (call: Call) => JSON.parse(String(call.init.body));

  it("creates a session, proposes a time and books only on Confirm", async () => {
    const user = userEvent.setup();
    handlers.push((c) =>
      c.url === "/api/chat/sessions"
        ? json({ session_id: SESSION, greeting: "Hello, how can I help?", quick_replies: ["Book a check-up"] }, 201)
        : undefined,
    );
    handlers.push((c) =>
      c.url.endsWith("/messages") ? json(reply("Dr. Kavya Iyer can see you at 10:30 am.", proposal())) : undefined,
    );
    handlers.push((c) =>
      c.url.endsWith("/confirm")
        ? json({ reference: "K7M2Q9XA", status: "confirmed", starts_at: "2026-11-03T05:00:00Z", doctor_name: "Dr. Kavya Iyer", service_name: "Check-up and cleaning" }, 201)
        : undefined,
    );

    render(<ChatPanel clinicName="Aster Dental Studio" timeZone={IST} callHref="tel:+919000000100" />);
    expect(await screen.findByText("Hello, how can I help?")).toBeInTheDocument();
    expect(window.sessionStorage.getItem(SESSION_KEY)).toBe(SESSION);

    await user.click(screen.getByRole("button", { name: "Book a check-up" }));
    const card = await screen.findByRole("region", { name: "Suggested time" });
    expect(calls.filter((c) => c.url.endsWith("/confirm"))).toHaveLength(0);
    expect(body(calls[1]!)).toEqual({ text: "Book a check-up" });

    await user.type(within(card).getByLabelText("Patient name"), "Ananya Krishnan");
    await user.type(within(card).getByLabelText("Mobile number"), "9876543210");
    await user.click(within(card).getByRole("button", { name: "Confirm booking" }));

    expect(await screen.findByText("You're booked")).toBeInTheDocument();
    const confirm = calls.find((c) => c.url.endsWith("/confirm"))!;
    expect(confirm.url).toBe(`/api/chat/sessions/${SESSION}/proposals/p1/confirm`);
    expect(body(confirm)).toEqual({ name: "Ananya Krishnan", phone: "+919876543210", consent_contact: false });
    expect((confirm.init.headers as Record<string, string>)["Idempotency-Key"]).toMatch(/^chat-/);
    expect(screen.getAllByText("K7M2Q9XA").length).toBeGreaterThan(0);
  });

  it("shows a lost slot as expired and lets the patient ask again", async () => {
    const user = userEvent.setup();
    handlers.push((c) => (c.url === "/api/chat/sessions" ? json({ session_id: SESSION, greeting: "Hi", quick_replies: ["Book"] }, 201) : undefined));
    handlers.push((c) => (c.url.endsWith("/messages") ? json(reply("How about 10:30?", proposal())) : undefined));
    handlers.push((c) =>
      c.url.endsWith("/confirm") ? json({ error: { code: "slot_unavailable", message: "That time was just taken." } }, 409) : undefined,
    );
    render(<ChatPanel clinicName="Aster" timeZone={IST} callHref={null} />);
    await user.click(await screen.findByRole("button", { name: "Book" }));
    const card = await screen.findByRole("region", { name: "Suggested time" });
    await user.type(within(card).getByLabelText("Patient name"), "Ananya Krishnan");
    await user.type(within(card).getByLabelText("Mobile number"), "9876543210");
    await user.click(within(card).getByRole("button", { name: "Confirm booking" }));
    expect(await screen.findByText("That time was just taken.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm booking" })).not.toBeInTheDocument();
  });

  it("starts a fresh session once when the stored one is gone", async () => {
    const user = userEvent.setup();
    window.sessionStorage.setItem(SESSION_KEY, "sess_stale_0123456789");
    let created = 0;
    handlers.push((c) => {
      if (c.url !== "/api/chat/sessions") return undefined;
      created++;
      return json({ session_id: SESSION, greeting: "Hello again", quick_replies: [] }, 201);
    });
    handlers.push((c) => (c.url === `/api/chat/sessions/${SESSION}/messages` ? json(reply("We open at 9:30 am.")) : undefined));
    render(<ChatPanel clinicName="Aster" timeZone={IST} callHref={null} />);
    await user.type(screen.getByLabelText("Message"), "When do you open?{Enter}");
    expect(await screen.findByText("We open at 9:30 am.")).toBeInTheDocument();
    expect(created).toBe(1);
    expect(calls[0]!.url).toBe("/api/chat/sessions/sess_stale_0123456789/messages");
    expect(window.sessionStorage.getItem(SESSION_KEY)).toBe(SESSION);
  });

  it("offers working fallbacks when the assistant is unavailable", async () => {
    handlers.push((c) =>
      c.url === "/api/chat/sessions" ? json({ error: { code: "upstream_unavailable", message: "We can't reach the clinic system right now." } }, 503) : undefined,
    );
    render(<ChatPanel clinicName="Aster" timeZone={IST} callHref="tel:+919000000100" />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Book online" })).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Request a call back" })).toBeInTheDocument();
  });
});
