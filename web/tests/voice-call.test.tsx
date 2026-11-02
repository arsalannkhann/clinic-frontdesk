import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatLauncher, ChatPanel, openChat, VOICE_MESSAGES } from "@/components/public/chat/ChatWidget";
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

type Call = { url: string; init: RequestInit };
let calls: Call[];
let handlers: ((call: Call) => Response | undefined)[];
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const body = (call: Call) => JSON.parse(String(call.init.body));

const proposal = {
  id: "p1",
  doctor_name: "Dr. Kavya Iyer",
  service_name: "Check-up and cleaning",
  starts_at: "2026-11-03T05:00:00Z",
  expires_at: new Date(Date.now() + 10 * 60_000).toISOString(),
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
      ? json({ text: "Hi, you're through to the AI receptionist.", audio: "R1JFRVQ=", audio_mime: "audio/mpeg" })
      : json({ error: { code: "voice_unavailable", message: "Voice is unavailable." } }, status)
    : undefined;

async function startCall() {
  const user = userEvent.setup();
  render(<ChatPanel clinicName="Aster" timeZone={IST} callHref={null} voiceEnabled />);
  expect(await screen.findByText("Hello, how can I help?")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Talk to the AI receptionist" }));
  return user;
}

describe("voice call in the chat panel", () => {
  it("shows the Talk button only when the clinic offers voice", async () => {
    const { unmount } = render(<ChatPanel clinicName="Aster" timeZone={IST} callHref={null} />);
    expect(await screen.findByText("Hello, how can I help?")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Talk to the AI receptionist" })).not.toBeInTheDocument();
    unmount();
    render(<ChatPanel clinicName="Aster" timeZone={IST} callHref={null} voiceEnabled />);
    expect(await screen.findByRole("button", { name: "Talk to the AI receptionist" })).toBeInTheDocument();
  });

  it("greets, sends an utterance, shows the transcript and reply, and offers the proposal card", async () => {
    handlers.push(greetingHandler());
    handlers.push((c) =>
      c.url === `/api/chat/sessions/${SESSION}/voice`
        ? json({
            messages: [{ role: "assistant", text: "Dr. Kavya Iyer can see you at 10:30 am. Confirm on screen to book." }],
            proposal,
            quick_replies: ["Show a later time"],
            transcript: "I'd like a check-up tomorrow",
            audio: "UkVQTFk=",
            audio_mime: "audio/mpeg",
          })
        : undefined,
    );
    const user = await startCall();
    expect(voice.player.unlock).toHaveBeenCalled();
    expect(voice.recorder.start).toHaveBeenCalledOnce();
    expect(await screen.findByText("Hi, you're through to the AI receptionist.")).toBeInTheDocument();
    expect(voice.player.play).toHaveBeenCalledWith("R1JFRVQ=", "audio/mpeg");
    const bar = screen.getByRole("group", { name: "Voice call" });
    await waitFor(() => expect(within(bar).getByText("Listening…")).toBeInTheDocument());
    // The microphone was paused for the greeting and resumed after it.
    expect(voice.recorder.pause).toHaveBeenCalled();
    expect(voice.recorder.resume).toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Talk to the AI receptionist" })).not.toBeInTheDocument();

    act(() => voice.handlers!.onSpeechStart?.());
    await act(async () => voice.handlers!.onUtterance(WAV, 1500));
    const turn = calls.find((c) => c.url.endsWith("/voice"))!;
    expect(turn.init.method).toBe("POST");
    expect(body(turn)).toEqual({ audio: WAV });

    const log = screen.getByRole("log");
    expect(await within(log).findByText("I'd like a check-up tomorrow")).toBeInTheDocument();
    expect(within(log).getByText(/Dr. Kavya Iyer can see you at 10:30 am/)).toBeInTheDocument();
    const card = await screen.findByRole("region", { name: "Suggested time" });
    expect(within(card).getByRole("button", { name: "Confirm booking" })).toBeInTheDocument();
    expect(voice.player.play).toHaveBeenLastCalledWith("UkVQTFk=", "audio/mpeg");
    await waitFor(() => expect(within(bar).getByText("Listening…")).toBeInTheDocument());
    // Nothing was booked by voice.
    expect(calls.some((c) => c.url.endsWith("/confirm"))).toBe(false);

    await user.click(within(bar).getByRole("button", { name: "Mute" }));
    expect(voice.recorder.setMuted).toHaveBeenLastCalledWith(true);
    expect(within(bar).getByText("Muted")).toBeInTheDocument();

    await user.click(within(bar).getByRole("button", { name: "End call" }));
    expect(voice.recorder.stop).toHaveBeenCalled();
    expect(voice.player.dispose).toHaveBeenCalled();
    expect(screen.queryByRole("group", { name: "Voice call" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Talk to the AI receptionist" })).toBeInTheDocument();
  });

  it("offers Tap to interrupt while the assistant speaks", async () => {
    handlers.push(greetingHandler());
    let finish: (value: string) => void = () => {};
    voice.player.play.mockImplementation(() => new Promise((resolve) => (finish = resolve)));
    voice.player.stop.mockImplementation(() => finish("stopped"));
    const user = await startCall();
    const bar = screen.getByRole("group", { name: "Voice call" });
    expect(await within(bar).findByText("Speaking…")).toBeInTheDocument();
    await user.click(within(bar).getByRole("button", { name: "Tap to interrupt" }));
    expect(voice.player.stop).toHaveBeenCalled();
    await waitFor(() => expect(within(bar).getByText("Listening…")).toBeInTheDocument());
  });

  it("says it didn't catch that on no_speech and keeps listening", async () => {
    handlers.push(greetingHandler());
    handlers.push((c) =>
      c.url.endsWith("/voice") ? json({ error: { code: "no_speech", message: "We couldn't hear that. Please try again." } }, 422) : undefined,
    );
    await startCall();
    const bar = screen.getByRole("group", { name: "Voice call" });
    await waitFor(() => expect(within(bar).getByText("Listening…")).toBeInTheDocument());
    const resumed = voice.recorder.resume.mock.calls.length;
    await act(async () => voice.handlers!.onUtterance(WAV, 600));
    expect(await within(bar).findByText(VOICE_MESSAGES.noSpeech)).toBeInTheDocument();
    await waitFor(() => expect(voice.recorder.resume.mock.calls.length).toBeGreaterThan(resumed));
    expect(within(bar).getByText("Listening…")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    // Nothing was added to the transcript.
    expect(within(screen.getByRole("log")).queryAllByText(/./).filter((el) => el.tagName === "P")).toHaveLength(2);
    expect(voice.recorder.stop).not.toHaveBeenCalled();
  });

  it("stays in text chat with a clear message when the microphone is denied", async () => {
    voice.startError = new MicError("denied");
    await startCall();
    expect(await screen.findByRole("alert")).toHaveTextContent(VOICE_MESSAGES.micDenied);
    expect(screen.queryByRole("group", { name: "Voice call" })).not.toBeInTheDocument();
    expect(calls.some((c) => c.url.includes("/voice"))).toBe(false);
    expect(screen.getByLabelText("Message")).toBeEnabled();
    expect(screen.getByRole("button", { name: "Talk to the AI receptionist" })).toBeInTheDocument();
  });

  it("ends the call when voice is unavailable", async () => {
    handlers.push(greetingHandler(503));
    await startCall();
    expect(await screen.findByRole("alert")).toHaveTextContent(VOICE_MESSAGES.unavailable);
    expect(screen.queryByRole("group", { name: "Voice call" })).not.toBeInTheDocument();
    expect(voice.recorder.stop).toHaveBeenCalled();
  });

  it("waits and keeps listening while the server is still answering a turn", async () => {
    handlers.push(greetingHandler());
    handlers.push((c) =>
      c.url.endsWith("/voice") ? json({ error: { code: "turn_in_progress", message: "Still answering" } }, 409) : undefined,
    );
    await startCall();
    const bar = screen.getByRole("group", { name: "Voice call" });
    await waitFor(() => expect(within(bar).getByText("Listening…")).toBeInTheDocument());
    await act(async () => voice.handlers!.onUtterance(WAV, 900));
    expect(await within(bar).findByText(/Still answering/)).toBeInTheDocument();
    await waitFor(() => expect(within(bar).getByText("Listening…")).toBeInTheDocument(), { timeout: 3000 });
    expect(voice.recorder.stop).not.toHaveBeenCalled();
  });
});

describe("opening the chat in call mode", () => {
  it("opens the launcher straight into a voice call", async () => {
    handlers.push(greetingHandler());
    vi.stubGlobal("matchMedia", vi.fn((query: string) => ({ matches: false, media: query, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
    render(<ChatLauncher clinicName="Aster" timeZone={IST} callHref={null} enabled voiceEnabled />);
    act(() => openChat(undefined, { mode: "call" }));
    const dialog = await screen.findByRole("dialog", { name: /AI booking assistant/ });
    expect(await within(dialog).findByRole("group", { name: "Voice call" })).toBeInTheDocument();
    expect(voice.recorder.start).toHaveBeenCalledOnce();
    expect(await within(dialog).findByText("Hi, you're through to the AI receptionist.")).toBeInTheDocument();
    // One session only: the call waited for the one the panel started.
    expect(calls.filter((c) => c.url === "/api/chat/sessions")).toHaveLength(1);

    // Closing the panel ends the call.
    await userEvent.setup().click(within(dialog).getByRole("button", { name: "Close chat" }));
    expect(voice.recorder.stop).toHaveBeenCalled();
  });

  it("opens plain text chat by default", async () => {
    vi.stubGlobal("matchMedia", vi.fn((query: string) => ({ matches: false, media: query, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
    render(<ChatLauncher clinicName="Aster" timeZone={IST} callHref={null} enabled voiceEnabled />);
    act(() => openChat());
    expect(await screen.findByText("Hello, how can I help?")).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Voice call" })).not.toBeInTheDocument();
    expect(voice.recorder.start).not.toHaveBeenCalled();
  });
});
