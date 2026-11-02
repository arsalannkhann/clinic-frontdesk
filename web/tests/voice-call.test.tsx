import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatLauncher, ChatPanel, openChat } from "@/components/public/chat/ChatWidget";
import { VOICE_MESSAGES, VoiceCall } from "@/components/public/chat/VoiceCall";
import { SESSION_KEY } from "@/lib/chat";
import type { RecorderHandlers } from "@/lib/voice/recorder";
import { MicError } from "@/lib/voice/recorder";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }) }));

// jsdom has no microphone or Web Audio: the recorder and player are fakes driven by the test.
const voice = vi.hoisted(() => ({
  handlers: null as RecorderHandlers | null,
  startError: null as unknown,
  recorder: {
    start: vi.fn(),
    pause: vi.fn(),
    resume: vi.fn(),
    setMuted: vi.fn(),
    stop: vi.fn(),
  },
  player: {
    unlock: vi.fn(),
    play: vi.fn(),
    stop: vi.fn(),
    dispose: vi.fn(),
  },
}));

vi.mock("@/lib/voice/recorder", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/voice/recorder")>();
  return {
    ...actual,
    createRecorder: (handlers: RecorderHandlers) => {
      voice.handlers = handlers;
      return voice.recorder;
    },
  };
});
vi.mock("@/lib/voice/player", () => ({ createPlayer: () => voice.player }));

const IST = "Asia/Kolkata";
const SESSION = "sess_0123456789abcdef";
const WAV = "UklGRiQAAABXQVZF";
const GREETING = "Hello, Aster. I'm the AI receptionist. How can I help you today?";

type Call = { url: string; init: RequestInit };
let calls: Call[];
let handlers: ((call: Call) => Response | undefined)[];
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const body = (call: Call) => JSON.parse(String(call.init.body));
const sessionPosts = () => calls.filter((c) => c.url === "/api/chat/sessions");

const proposal = {
  id: "p1",
  doctor_name: "Dr. Kavya Iyer",
  service_name: "Check-up and cleaning",
  starts_at: "2026-11-03T05:00:00Z",
  expires_at: new Date(Date.now() + 10 * 60_000).toISOString(),
};
const booking = {
  reference: "KNKDRNKV",
  status: "confirmed",
  starts_at: "2026-11-03T05:00:00Z",
  doctor_name: "Dr. Kavya Iyer",
  service_name: "Check-up and cleaning",
};

beforeEach(() => {
  calls = [];
  handlers = [(c) => (c.url === "/api/chat/sessions" ? json({ session_id: SESSION, greeting: "Hello, how can I help?", quick_replies: [] }, 201) : undefined)];
  voice.handlers = null;
  voice.startError = null;
  for (const fn of [...Object.values(voice.recorder), ...Object.values(voice.player)]) fn.mockReset();
  voice.recorder.start.mockImplementation(async () => {
    if (voice.startError) throw voice.startError;
  });
  voice.player.play.mockResolvedValue("ended");
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

const greetingHandler = (status = 200) => (c: Call) =>
  c.url === `/api/chat/sessions/${SESSION}/voice/greeting`
    ? status === 200
      ? json({ text: GREETING, audio: "R1JFRVQ=", audio_mime: "audio/mpeg" })
      : json({ error: { code: "voice_unavailable", message: "Voice is unavailable." } }, status)
    : undefined;

const turnHandler = (reply: Record<string, unknown>) => (c: Call) =>
  c.url === `/api/chat/sessions/${SESSION}/voice`
    ? json({ proposal: null, quick_replies: [], audio: "UkVQTFk=", audio_mime: "audio/mpeg", booking: null, ...reply })
    : undefined;

function renderCall(onClose = vi.fn()) {
  const user = userEvent.setup();
  render(<VoiceCall clinicName="Aster" timeZone={IST} callHref="tel:+919000000100" open onClose={onClose} />);
  return { user, onClose };
}

const callScreen = () => screen.getByRole("group", { name: "Voice call" });
const listening = () => waitFor(() => expect(within(callScreen()).getByText("Listening…")).toBeInTheDocument());

const stubMatchMedia = () =>
  vi.stubGlobal("matchMedia", vi.fn((query: string) => ({ matches: false, media: query, addEventListener: vi.fn(), removeEventListener: vi.fn() })));

describe("voice call screen", () => {
  it("greets, sends an utterance, speaks the reply, and never shows a proposal card", async () => {
    handlers.push(greetingHandler());
    handlers.push(
      turnHandler({
        messages: [{ role: "assistant", text: "I'm holding 10:30 am with Dr. Kavya Iyer. May I have your full name?" }],
        proposal,
        transcript: "I'd like a check-up tomorrow",
      }),
    );
    const { user } = renderCall();
    expect(voice.player.unlock).toHaveBeenCalled();
    expect(voice.recorder.start).toHaveBeenCalledOnce();
    expect(await screen.findByText(GREETING)).toBeInTheDocument();
    expect(voice.player.play).toHaveBeenCalledWith("R1JFRVQ=", "audio/mpeg");
    await listening();
    // The microphone was paused for the greeting and resumed after it.
    expect(voice.recorder.pause).toHaveBeenCalled();
    expect(voice.recorder.resume).toHaveBeenCalled();
    // A fresh session of its own; the chat's stored session is left alone.
    expect(sessionPosts()).toHaveLength(1);
    expect(window.sessionStorage.getItem(SESSION_KEY)).toBeNull();
    expect(screen.getByText("00:00")).toBeInTheDocument();

    act(() => voice.handlers!.onSpeechStart?.());
    await act(async () => voice.handlers!.onUtterance(WAV, 1500));
    const turn = calls.find((c) => c.url.endsWith("/voice"))!;
    expect(turn.init.method).toBe("POST");
    expect(body(turn)).toEqual({ audio: WAV });
    expect(await screen.findByText(/May I have your full name/)).toBeInTheDocument();
    expect(voice.player.play).toHaveBeenLastCalledWith("UkVQTFk=", "audio/mpeg");
    await listening();
    // A held time is spoken, not shown: no card, no confirm.
    expect(screen.queryByRole("region", { name: "Suggested time" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm booking" })).not.toBeInTheDocument();
    expect(calls.some((c) => c.url.endsWith("/confirm"))).toBe(false);

    await user.click(screen.getByRole("button", { name: "Mute" }));
    expect(voice.recorder.setMuted).toHaveBeenLastCalledWith(true);
    expect(within(callScreen()).getByText("Muted")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "End call" }));
    expect(voice.recorder.stop).toHaveBeenCalled();
    expect(voice.player.dispose).toHaveBeenCalled();
    expect(screen.queryByRole("group", { name: "Voice call" })).not.toBeInTheDocument();
    expect(screen.getByText(/^Call ended · \d\d:\d\d$/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows the booking made by voice and keeps the call going", async () => {
    handlers.push(greetingHandler());
    handlers.push(
      turnHandler({
        messages: [{ role: "assistant", text: "You're booked. Your reference is K N K D R N K V. Is there anything else?" }],
        transcript: "Yes, that's fine",
        booking,
      }),
    );
    const { user } = renderCall();
    await listening();
    await act(async () => voice.handlers!.onUtterance(WAV, 1200));
    const panel = await screen.findByRole("region", { name: "Booking" });
    expect(within(panel).getByText("Booked")).toBeInTheDocument();
    expect(within(panel).getByText("Check-up and cleaning")).toBeInTheDocument();
    expect(within(panel).getByText("with Dr. Kavya Iyer")).toBeInTheDocument();
    // 05:00 UTC is 10:30 am in the clinic's zone.
    expect(within(panel).getByText("Tuesday, 3 November, 10:30 am")).toBeInTheDocument();
    expect(within(panel).getByText("KNKDRNKV")).toBeInTheDocument();
    await listening();
    expect(voice.recorder.stop).not.toHaveBeenCalled();

    // The end screen keeps the summary.
    await user.click(screen.getByRole("button", { name: "End call" }));
    expect(screen.getByText(/^Call ended/)).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Booking" })).getByText("KNKDRNKV")).toBeInTheDocument();
  });

  it("offers Tap to interrupt while the assistant speaks", async () => {
    handlers.push(greetingHandler());
    let finish: (value: string) => void = () => {};
    voice.player.play.mockImplementation(() => new Promise((resolve) => (finish = resolve)));
    voice.player.stop.mockImplementation(() => finish("stopped"));
    const { user } = renderCall();
    expect(await within(callScreen()).findByText("Speaking…")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Tap to interrupt" }));
    expect(voice.player.stop).toHaveBeenCalled();
    await listening();
  });

  it("says it didn't catch that on no_speech and keeps listening", async () => {
    handlers.push(greetingHandler());
    handlers.push((c) =>
      c.url.endsWith("/voice") ? json({ error: { code: "no_speech", message: "We couldn't hear that. Please try again." } }, 422) : undefined,
    );
    renderCall();
    await listening();
    const resumed = voice.recorder.resume.mock.calls.length;
    await act(async () => voice.handlers!.onUtterance(WAV, 600));
    expect(await within(callScreen()).findByText(VOICE_MESSAGES.noSpeech)).toBeInTheDocument();
    await waitFor(() => expect(voice.recorder.resume.mock.calls.length).toBeGreaterThan(resumed));
    expect(within(callScreen()).getByText("Listening…")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(voice.recorder.stop).not.toHaveBeenCalled();
  });

  it("ends with a call-appropriate message when the microphone is denied", async () => {
    voice.startError = new MicError("denied");
    const { user, onClose } = renderCall();
    expect(await screen.findByRole("alert")).toHaveTextContent(VOICE_MESSAGES.micDenied);
    expect(screen.getByRole("alert").textContent).not.toMatch(/keep typing/i);
    expect(screen.getByText(/^Call ended · 00:00$/)).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Voice call" })).not.toBeInTheDocument();
    expect(voice.recorder.stop).toHaveBeenCalled();
    expect(calls.some((c) => c.url.endsWith("/voice"))).toBe(false);
    expect(screen.getByRole("link", { name: "Call the clinic" })).toHaveAttribute("href", "tel:+919000000100");
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalled();
  });

  it("asks for the session and greeting while the browser asks for the microphone", async () => {
    handlers.push(greetingHandler());
    let grant: () => void = () => {};
    voice.recorder.start.mockImplementation(() => new Promise<void>((resolve) => (grant = resolve)));
    renderCall();
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/voice/greeting"))).toBe(true));
    expect(sessionPosts()).toHaveLength(1);
    // The greeting waits for the microphone.
    expect(voice.player.play).not.toHaveBeenCalled();
    expect(within(callScreen()).getByText("Connecting…")).toBeInTheDocument();
    expect(within(callScreen()).getByText(VOICE_MESSAGES.allowMic)).toBeInTheDocument();
    await act(async () => grant());
    await waitFor(() => expect(voice.player.play).toHaveBeenCalledWith("R1JFRVQ=", "audio/mpeg"));
    await listening();
  });

  it("ends the call when voice is unavailable", async () => {
    handlers.push(greetingHandler(503));
    renderCall();
    expect(await screen.findByRole("alert")).toHaveTextContent(VOICE_MESSAGES.unavailable);
    expect(screen.getByRole("alert").textContent).not.toMatch(/keep typing/i);
    expect(screen.queryByRole("group", { name: "Voice call" })).not.toBeInTheDocument();
    expect(voice.recorder.stop).toHaveBeenCalled();
  });

  it("waits and keeps listening while the server is still answering a turn", async () => {
    handlers.push(greetingHandler());
    handlers.push((c) =>
      c.url.endsWith("/voice") ? json({ error: { code: "turn_in_progress", message: "Still answering" } }, 409) : undefined,
    );
    renderCall();
    await listening();
    await act(async () => voice.handlers!.onUtterance(WAV, 900));
    expect(await within(callScreen()).findByText(/Still answering/)).toBeInTheDocument();
    await waitFor(() => expect(within(callScreen()).getByText("Listening…")).toBeInTheDocument(), { timeout: 3000 });
    expect(voice.recorder.stop).not.toHaveBeenCalled();
  });

  it("starts a new call from the end screen with Call again", async () => {
    handlers.push(greetingHandler());
    const { user } = renderCall();
    await listening();
    await user.click(screen.getByRole("button", { name: "End call" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Call again" })).toHaveFocus());
    await user.click(screen.getByRole("button", { name: "Call again" }));
    await listening();
    expect(screen.getByRole("button", { name: "End call" })).toHaveFocus();
    expect(voice.recorder.start).toHaveBeenCalledTimes(2);
    expect(sessionPosts()).toHaveLength(2);
    expect(screen.queryByText(/^Call ended/)).not.toBeInTheDocument();
  });

  it("releases the microphone and stops audio when unmounted", async () => {
    handlers.push(greetingHandler());
    const { unmount } = render(<VoiceCall clinicName="Aster" timeZone={IST} callHref={null} open onClose={vi.fn()} />);
    await listening();
    unmount();
    expect(voice.recorder.stop).toHaveBeenCalled();
    expect(voice.player.dispose).toHaveBeenCalled();
  });
});

describe("opening the call", () => {
  it("shows the chat's Talk button only when the clinic offers voice", async () => {
    const { unmount } = render(<ChatPanel clinicName="Aster" timeZone={IST} callHref={null} />);
    expect(await screen.findByText("Hello, how can I help?")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Talk to the AI receptionist" })).not.toBeInTheDocument();
    unmount();
    render(<ChatPanel clinicName="Aster" timeZone={IST} callHref={null} voiceEnabled />);
    const talk = await screen.findByRole("button", { name: "Talk to the AI receptionist" });
    expect(talk).toHaveTextContent("Talk");
  });

  it("opens the call overlay, not the chat, from the landing page's Talk buttons", async () => {
    handlers.push(greetingHandler());
    stubMatchMedia();
    render(<ChatLauncher clinicName="Aster" timeZone={IST} callHref={null} enabled voiceEnabled />);
    act(() => openChat(undefined, { mode: "call" }));
    const dialog = await screen.findByRole("dialog", { name: /AI receptionist/ });
    expect(within(dialog).getByRole("group", { name: "Voice call" })).toBeInTheDocument();
    expect(voice.recorder.start).toHaveBeenCalledOnce();
    expect(await within(dialog).findByText(GREETING)).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: /AI booking assistant/ })).not.toBeInTheDocument();
    // Only the call's session: the chat was never opened.
    expect(sessionPosts()).toHaveLength(1);
    expect(screen.queryByText("Hello, how can I help?")).not.toBeInTheDocument();
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "End call" })).toHaveFocus());

    // Escape closes the overlay and ends the call.
    await userEvent.setup().keyboard("{Escape}");
    expect(voice.recorder.stop).toHaveBeenCalled();
    expect(screen.queryByRole("dialog", { name: /AI receptionist/ })).not.toBeInTheDocument();
  });

  it("opens the call from the chat header's Talk button, hiding the chat until it closes", async () => {
    handlers.push(greetingHandler());
    stubMatchMedia();
    const user = userEvent.setup();
    render(<ChatLauncher clinicName="Aster" timeZone={IST} callHref={null} enabled voiceEnabled />);
    act(() => openChat());
    const chat = await screen.findByRole("dialog", { name: /AI booking assistant/ });
    expect(await within(chat).findByText("Hello, how can I help?")).toBeInTheDocument();
    await user.click(within(chat).getByRole("button", { name: "Talk to the AI receptionist" }));
    const dialog = await screen.findByRole("dialog", { name: /AI receptionist/ });
    expect(within(dialog).getByRole("group", { name: "Voice call" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: /AI booking assistant/ })).not.toBeInTheDocument();
    await waitFor(() => expect(voice.recorder.start).toHaveBeenCalledOnce());
    // A session for the chat and a separate one for the call.
    await waitFor(() => expect(sessionPosts()).toHaveLength(2));

    await user.click(within(dialog).getByRole("button", { name: "End call" }));
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog", { name: /AI receptionist/ })).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: /AI booking assistant/ })).toBeInTheDocument();
  });

  it("opens plain text chat by default", async () => {
    stubMatchMedia();
    render(<ChatLauncher clinicName="Aster" timeZone={IST} callHref={null} enabled voiceEnabled />);
    act(() => openChat());
    expect(await screen.findByText("Hello, how can I help?")).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Voice call" })).not.toBeInTheDocument();
    expect(voice.recorder.start).not.toHaveBeenCalled();
  });

  it("opens the chat instead when the clinic doesn't offer voice", async () => {
    stubMatchMedia();
    render(<ChatLauncher clinicName="Aster" timeZone={IST} callHref={null} enabled />);
    act(() => openChat(undefined, { mode: "call" }));
    expect(await screen.findByText("Hello, how can I help?")).toBeInTheDocument();
    expect(voice.recorder.start).not.toHaveBeenCalled();
  });
});
