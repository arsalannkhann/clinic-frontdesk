import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatLauncher, ChatPanel, SMALL_SCREEN_QUERY } from "@/components/public/chat/ChatWidget";
import { ProposalCard } from "@/components/public/chat/ProposalCard";
import type { ChatProposal, ChatReply } from "@/lib/api/types";
import {
  BOOKED_KEY,
  chatReducer,
  CONFIRM_ERRORS,
  CONVERSATION_LIMIT,
  FALLBACK_REPLIES,
  initialChat,
  NEW_CHAT,
  readStoredBooking,
  readStoredSession,
  SESSION_KEY,
  storeSession,
  TURN_IN_PROGRESS,
  validMessage,
} from "@/lib/chat";

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

  it("puts a stored booked card back after a reload, where it was", () => {
    const booking = { reference: "ABCD2345", status: "confirmed" as const, starts_at: "x", doctor_name: "d", service_name: "s" };
    const transcript = {
      messages: [
        { role: "assistant" as const, text: "Hello" },
        { role: "user" as const, text: "A check-up" },
        { role: "assistant" as const, text: "10:30?" },
        { role: "user" as const, text: "Thanks" },
        { role: "assistant" as const, text: "Anything else?" },
      ],
      proposal: null,
      quick_replies: [],
    };
    const restored = chatReducer(initialChat, {
      type: "restored",
      sessionId: SESSION,
      transcript,
      booked: { sessionId: SESSION, proposal: proposal(), booking, after: 3 },
    });
    expect(restored).toMatchObject({ proposalState: "booked", booking });
    expect(restored.lines.map((l) => l.text)).toEqual(["Hello", "A check-up", "10:30?", "Booking reference ABCD2345", "Thanks", "Anything else?"]);
    expect(restored.proposalAfter).toBe(restored.lines[2]!.id);
    // Another session's booking is ignored.
    const other = chatReducer(initialChat, {
      type: "restored",
      sessionId: "sess_other_0123456789",
      transcript,
      booked: { sessionId: SESSION, proposal: proposal(), booking, after: 3 },
    });
    expect(other.proposalState).toBeNull();
  });

  it("ends a conversation at the turn limit without offering a retry", () => {
    const sent = chatReducer(started, { type: "send", text: "More?" });
    const limited = chatReducer(sent, { type: "replyFailed", message: CONVERSATION_LIMIT, final: true });
    expect(limited).toMatchObject({ pending: null, error: CONVERSATION_LIMIT });
    expect(limited.quickReplies).toEqual([NEW_CHAT, ...FALLBACK_REPLIES]);
    expect(chatReducer(limited, { type: "reset" })).toBe(initialChat);
  });

  it("takes a refused message back while an earlier turn is still being answered", () => {
    const sent = chatReducer(started, { type: "send", text: "And on Friday?" });
    const busy = chatReducer(sent, { type: "turnBusy", unsend: true });
    expect(busy).toMatchObject({ typing: false, error: null, notice: TURN_IN_PROGRESS, pending: null });
    expect(busy.lines.map((l) => l.text)).toEqual(["Hello"]);
    // A retry of a message already on screen keeps it, and can be tried again.
    const failed = chatReducer(sent, { type: "replyFailed", message: "x" });
    const retried = chatReducer(chatReducer(failed, { type: "retrying" }), { type: "turnBusy", unsend: false });
    expect(retried).toMatchObject({ notice: TURN_IN_PROGRESS, pending: "And on Friday?" });
    expect(retried.lines.map((l) => l.text)).toEqual(["Hello", "And on Friday?"]);
    // The next message clears the notice.
    expect(chatReducer(busy, { type: "send", text: "Hi" }).notice).toBeNull();
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
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "Confirm booking" }));
    expect(onConfirm).toHaveBeenCalledWith({ name: "Ananya Krishnan", phone: "+919876543210", consent_contact: true });
  });

  it("requires consent to contact before booking", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(<ProposalCard proposal={proposal()} state="open" error={null} booking={null} timeZone={IST} onConfirm={onConfirm} onChange={vi.fn()} />);
    await user.type(screen.getByLabelText("Patient name"), "Ananya Krishnan");
    await user.type(screen.getByLabelText("Mobile number"), "9876543210");
    await user.click(screen.getByRole("button", { name: "Confirm booking" }));
    expect(onConfirm).not.toHaveBeenCalled();
    const box = screen.getByRole("checkbox");
    expect(box).toHaveAttribute("aria-invalid", "true");
    expect(box).toHaveAccessibleDescription(/Tick this to book/);
    await user.click(box);
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
    await user.click(within(card).getByRole("checkbox"));
    await user.click(within(card).getByRole("button", { name: "Confirm booking" }));

    expect(await screen.findByText("You're booked")).toBeInTheDocument();
    const confirm = calls.find((c) => c.url.endsWith("/confirm"))!;
    expect(confirm.url).toBe(`/api/chat/sessions/${SESSION}/proposals/p1/confirm`);
    expect(body(confirm)).toEqual({ name: "Ananya Krishnan", phone: "+919876543210", consent_contact: true });
    expect((confirm.init.headers as Record<string, string>)["Idempotency-Key"]).toMatch(/^chat-/);
    expect(screen.getAllByText("K7M2Q9XA").length).toBeGreaterThan(0);
    expect(readStoredBooking(SESSION)?.booking.reference).toBe("K7M2Q9XA");
  });

  it("shows the booked card again after a reload, though the server hides the booking note", async () => {
    window.sessionStorage.setItem(SESSION_KEY, SESSION);
    window.sessionStorage.setItem(
      BOOKED_KEY,
      JSON.stringify({
        sessionId: SESSION,
        proposal: proposal(),
        booking: { reference: "K7M2Q9XA", status: "pending_sync", starts_at: "2026-11-03T05:00:00Z", doctor_name: "Dr. Kavya Iyer", service_name: "Check-up and cleaning" },
        after: 3,
      }),
    );
    handlers.push((c) =>
      c.url === `/api/chat/sessions/${SESSION}`
        ? json({
            messages: [
              { role: "assistant", text: "Hello" },
              { role: "user", text: "A check-up please" },
              { role: "assistant", text: "How about 10:30?" },
            ],
            proposal: null,
            quick_replies: ["Opening hours"],
          })
        : undefined,
    );
    render(<ChatPanel clinicName="Aster" timeZone={IST} callHref={null} />);
    const booked = await screen.findByRole("region", { name: "Booking" });
    expect(within(booked).getByText("Booking received")).toBeInTheDocument();
    expect(within(booked).getByText("K7M2Q9XA")).toBeInTheDocument();
    expect(screen.getByText("How about 10:30?").compareDocumentPosition(booked) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("explains a refused confirm by its code and keeps the card for consent", async () => {
    const user = userEvent.setup();
    handlers.push((c) => (c.url === "/api/chat/sessions" ? json({ session_id: SESSION, greeting: "Hi", quick_replies: ["Book"] }, 201) : undefined));
    handlers.push((c) => (c.url.endsWith("/messages") ? json(reply("How about 10:30?", proposal())) : undefined));
    handlers.push((c) =>
      c.url.endsWith("/confirm") ? json({ error: { code: "consent_required", message: "consent_contact must be true" } }, 422) : undefined,
    );
    render(<ChatPanel clinicName="Aster" timeZone={IST} callHref={null} />);
    await user.click(await screen.findByRole("button", { name: "Book" }));
    const card = await screen.findByRole("region", { name: "Suggested time" });
    await user.type(within(card).getByLabelText("Patient name"), "Ananya Krishnan");
    await user.type(within(card).getByLabelText("Mobile number"), "9876543210");
    await user.click(within(card).getByRole("checkbox"));
    await user.click(within(card).getByRole("button", { name: "Confirm booking" }));
    expect(await screen.findByText(CONFIRM_ERRORS.consent_required!)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Confirm booking" })).toBeEnabled();
  });

  it("offers a new chat once the conversation reaches its limit", async () => {
    const user = userEvent.setup();
    let created = 0;
    handlers.push((c) => {
      if (c.url !== "/api/chat/sessions") return undefined;
      created++;
      return json({ session_id: created === 1 ? SESSION : "sess_fresh_0123456789", greeting: `Hello ${created}`, quick_replies: [] }, 201);
    });
    handlers.push((c) =>
      c.url.endsWith("/messages") ? json({ error: { code: "conversation_limit", message: "Too many turns" } }, 409) : undefined,
    );
    render(<ChatPanel clinicName="Aster" timeZone={IST} callHref={null} />);
    expect(await screen.findByText("Hello 1")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Message"), "One more thing{Enter}");
    expect(await screen.findByText(CONVERSATION_LIMIT)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Try again/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: NEW_CHAT }));
    expect(await screen.findByText("Hello 2")).toBeInTheDocument();
    expect(screen.queryByText("One more thing")).not.toBeInTheDocument();
    expect(window.sessionStorage.getItem(SESSION_KEY)).toBe("sess_fresh_0123456789");
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
    await user.click(within(card).getByRole("checkbox"));
    await user.click(within(card).getByRole("button", { name: "Confirm booking" }));
    expect(await screen.findByText(CONFIRM_ERRORS.slot_unavailable!)).toBeInTheDocument();
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
    expect(await screen.findByText("Hello again")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Message"), "When do you open?{Enter}");
    expect(await screen.findByText("We open at 9:30 am.")).toBeInTheDocument();
    expect(created).toBe(1);
    // The reload looked the stored conversation up first; its 404 started a new one.
    expect(calls[0]!.url).toBe("/api/chat/sessions/sess_stale_0123456789");
    expect(calls[0]!.init.method).toBe("GET");
    expect(window.sessionStorage.getItem(SESSION_KEY)).toBe(SESSION);
  });

  it("restores the transcript, proposal and quick replies after a reload", async () => {
    window.sessionStorage.setItem(SESSION_KEY, SESSION);
    handlers.push((c) =>
      c.url === `/api/chat/sessions/${SESSION}`
        ? json({
            messages: [
              { role: "assistant", text: "Hello, how can I help?" },
              { role: "user", text: "A check-up please" },
              { role: "assistant", text: "Dr. Kavya Iyer can see you at 10:30 am." },
            ],
            proposal: proposal(),
            quick_replies: ["Show a later time"],
          })
        : undefined,
    );
    render(<ChatPanel clinicName="Aster" timeZone={IST} callHref={null} />);
    expect(await screen.findByText("A check-up please")).toBeInTheDocument();
    expect(screen.getByText("Dr. Kavya Iyer can see you at 10:30 am.")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Suggested time" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show a later time" })).toBeInTheDocument();
    expect(calls.filter((c) => c.url === "/api/chat/sessions")).toHaveLength(0);
  });

  it("creates one session under StrictMode", async () => {
    let created = 0;
    handlers.push((c) => {
      if (c.url !== "/api/chat/sessions") return undefined;
      created++;
      return json({ session_id: SESSION, greeting: "Hello", quick_replies: [] }, 201);
    });
    render(
      <StrictMode>
        <ChatPanel clinicName="Aster" timeZone={IST} callHref={null} />
      </StrictMode>,
    );
    expect(await screen.findByText("Hello")).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(created).toBe(1);
  });

  it("keeps the proposal card at its place in the transcript and clears stale quick replies once booked", async () => {
    const user = userEvent.setup();
    handlers.push((c) => (c.url === "/api/chat/sessions" ? json({ session_id: SESSION, greeting: "Hi", quick_replies: ["Book"] }, 201) : undefined));
    let replies = 0;
    handlers.push((c) => {
      if (!c.url.endsWith("/messages")) return undefined;
      replies++;
      return replies === 1
        ? json(reply("How about 10:30?", proposal(), ["Show a later time", "A different treatment"]))
        : json(reply("Anything else?", null, ["Opening hours"]));
    });
    handlers.push((c) =>
      c.url.endsWith("/confirm")
        ? json({ reference: "K7M2Q9XA", status: "confirmed", starts_at: "2026-11-03T05:00:00Z", doctor_name: "Dr. Kavya Iyer", service_name: "Check-up and cleaning" }, 202)
        : undefined,
    );
    render(<ChatPanel clinicName="Aster" timeZone={IST} callHref={null} />);
    await user.click(await screen.findByRole("button", { name: "Book" }));
    const card = await screen.findByRole("region", { name: "Suggested time" });
    expect(screen.getByRole("button", { name: "Show a later time" })).toBeInTheDocument();
    await user.type(within(card).getByLabelText("Patient name"), "Ananya Krishnan");
    await user.type(within(card).getByLabelText("Mobile number"), "9876543210");
    await user.click(within(card).getByRole("checkbox"));
    await user.click(within(card).getByRole("button", { name: "Confirm booking" }));
    expect(await screen.findByText("You're booked")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show a later time" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "A different treatment" })).not.toBeInTheDocument();

    await user.type(screen.getByLabelText("Message"), "Thanks{Enter}");
    const later = await screen.findByText("Anything else?");
    const booked = screen.getByRole("region", { name: "Booking" });
    const offer = screen.getByText("How about 10:30?");
    // DOM order: the offer, then the card, then the later reply.
    expect(offer.compareDocumentPosition(booked) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(booked.compareDocumentPosition(later) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("keeps the text and says so when the server is still answering the last message", async () => {
    const user = userEvent.setup();
    handlers.push((c) => (c.url === "/api/chat/sessions" ? json({ session_id: SESSION, greeting: "Hi", quick_replies: [] }, 201) : undefined));
    handlers.push((c) =>
      c.url.endsWith("/messages")
        ? json({ error: { code: "turn_in_progress", message: "The assistant is still answering your previous message" } }, 409)
        : undefined,
    );
    render(<ChatPanel clinicName="Aster" timeZone={IST} callHref={null} />);
    expect(await screen.findByText("Hi")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Message"), "Is Saturday open?{Enter}");
    expect(await screen.findByText(TURN_IN_PROGRESS)).toBeInTheDocument();
    expect(screen.getByLabelText("Message")).toHaveValue("Is Saturday open?");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    // The refused message is not left in the transcript as if it had been sent.
    expect(within(screen.getByRole("log")).queryByText("Is Saturday open?")).not.toBeInTheDocument();
  });

  it("sends nothing more while a reply is pending, and keeps what is typed", async () => {
    const user = userEvent.setup();
    let release: (() => void) | null = null;
    handlers.push((c) => (c.url === "/api/chat/sessions" ? json({ session_id: SESSION, greeting: "Hi", quick_replies: [] }, 201) : undefined));
    vi.mocked(globalThis.fetch).mockImplementation(async (url, init) => {
      const call = { url: String(url), init: init ?? {} };
      calls.push(call);
      if (call.url.endsWith("/messages")) {
        await new Promise<void>((resolve) => (release = resolve));
        return json(reply("Yes, from 9 am."));
      }
      for (const handler of handlers) {
        const response = handler(call);
        if (response) return response;
      }
      return json({ error: { code: "not_found", message: "Not found" } }, 404);
    });
    render(<ChatPanel clinicName="Aster" timeZone={IST} callHref={null} />);
    expect(await screen.findByText("Hi")).toBeInTheDocument();
    const input = screen.getByLabelText("Message");
    await user.type(input, "Open Saturday?{Enter}");
    await waitFor(() => expect(calls.filter((c) => c.url.endsWith("/messages"))).toHaveLength(1));
    await user.type(input, "And Sunday?{Enter}");
    expect(calls.filter((c) => c.url.endsWith("/messages"))).toHaveLength(1);
    expect(input).toHaveValue("And Sunday?");
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
    release!();
    expect(await screen.findByText("Yes, from 9 am.")).toBeInTheDocument();
    expect(input).toHaveValue("And Sunday?");
    await user.type(input, "{Enter}");
    await waitFor(() => expect(calls.filter((c) => c.url.endsWith("/messages"))).toHaveLength(2));
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

describe("chat launcher", () => {
  function stubMatchMedia(small: boolean) {
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => ({
        matches: query === SMALL_SCREEN_QUERY ? small : false,
        media: query,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    );
  }

  beforeEach(() => {
    window.sessionStorage.clear();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ session_id: SESSION, greeting: "Hello", quick_replies: ["Opening hours"] }), {
          status: 201,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
  });
  afterEach(() => vi.unstubAllGlobals());

  it("is a focus-trapping modal on a phone, closes on Escape and returns focus", async () => {
    stubMatchMedia(true);
    const user = userEvent.setup();
    render(<ChatLauncher clinicName="Aster" timeZone={IST} callHref={null} enabled />);
    const launcher = screen.getByRole("button", { name: "Chat with us" });
    await user.click(launcher);
    const dialog = await screen.findByRole("dialog", { name: "Aster" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    await screen.findByText("Hello");
    await waitFor(() => expect(screen.getByLabelText("Message")).toHaveFocus());

    // Tab cycles inside the dialog.
    for (let i = 0; i < 6; i++) {
      await user.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
    await user.tab({ shift: true });
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(document.body.style.overflow).toBe("hidden");

    await user.keyboard("{Escape}");
    expect(dialog).not.toBeVisible();
    await waitFor(() => expect(launcher).toHaveFocus());
    expect(document.body.style.overflow).toBe("");
  });

  it("is a non-modal panel on larger screens", async () => {
    stubMatchMedia(false);
    const user = userEvent.setup();
    render(<ChatLauncher clinicName="Aster" timeZone={IST} callHref={null} enabled />);
    await user.click(screen.getByRole("button", { name: "Chat with us" }));
    const dialog = await screen.findByRole("dialog", { name: "Aster" });
    expect(dialog).toHaveAttribute("aria-modal", "false");
    await user.click(within(dialog).getByRole("button", { name: "Close chat" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Chat with us" })).toHaveFocus());
  });
});
