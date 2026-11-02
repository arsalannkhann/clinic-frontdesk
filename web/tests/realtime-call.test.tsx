import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { agentPhase, parseAttribute, RealtimeCall, setCaption } from "@/components/public/chat/RealtimeCall";
import { VOICE_MESSAGES } from "@/components/public/chat/VoiceCall";

// jsdom has no WebRTC: the LiveKit room is a fake the test drives through its events.
const lk = vi.hoisted(() => ({
  room: { connectError: null as unknown, micError: null as unknown },
  rooms: [] as unknown[],
}));

vi.mock("livekit-client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("livekit-client")>();
  class FakeRoom {
    listeners = new Map<string, ((...args: unknown[]) => void)[]>();
    textHandler: ((...args: unknown[]) => void) | null = null;
    state = actual.ConnectionState.Connected;
    canPlaybackAudio = true;
    remoteParticipants = new Map();
    localParticipant = {
      identity: "caller-1",
      setMicrophoneEnabled: vi.fn(async () => {
        if (lk.room.micError) throw lk.room.micError;
      }),
      getTrackPublication: () => undefined,
    };
    connect = vi.fn(async () => {
      if (lk.room.connectError) throw lk.room.connectError;
    });
    disconnect = vi.fn(async () => {});
    prepareConnection = vi.fn();
    startAudio = vi.fn(async () => {});
    constructor() {
      lk.rooms.push(this);
    }
    on(event: string, fn: (...args: unknown[]) => void) {
      this.listeners.set(event, [...(this.listeners.get(event) ?? []), fn]);
      return this;
    }
    emit(event: string, ...args: unknown[]) {
      for (const fn of this.listeners.get(event) ?? []) fn(...args);
    }
    registerTextStreamHandler(_topic: string, fn: (...args: unknown[]) => void) {
      this.textHandler = fn;
    }
  }
  return { ...actual, Room: FakeRoom };
});

type FakeRoom = {
  emit: (event: string, ...args: unknown[]) => void;
  textHandler: (...args: unknown[]) => void;
  connect: ReturnType<typeof vi.fn>;
  disconnect: ReturnType<typeof vi.fn>;
  localParticipant: { setMicrophoneEnabled: ReturnType<typeof vi.fn> };
};
const room = () => lk.rooms.at(-1) as FakeRoom;

const IST = "Asia/Kolkata";
const SESSION = "sess_0123456789abcdef";
const TOKEN = { server_url: "wss://example.livekit.cloud", participant_token: "jwt", room: "call-1" };
const booking = {
  reference: "KNKDRNKV",
  status: "confirmed",
  starts_at: "2026-11-03T05:00:00Z",
  doctor_name: "Dr. Kavya Iyer",
  service_name: "Check-up and cleaning",
};

type Call = { url: string; init: RequestInit };
let calls: Call[];
let tokenStatus: number;
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

beforeEach(() => {
  calls = [];
  tokenStatus = 200;
  lk.rooms.length = 0;
  lk.room.connectError = null;
  lk.room.micError = null;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit) => {
      calls.push({ url, init });
      if (url === "/api/chat/sessions") return json({ session_id: SESSION, greeting: "Hi", quick_replies: [] }, 201);
      if (url === `/api/chat/sessions/${SESSION}/call`) {
        return tokenStatus === 200
          ? json(TOKEN)
          : json({ error: { code: "call_unavailable", message: "Calls are unavailable." } }, tokenStatus);
      }
      return json({ error: { code: "not_found", message: "Not found" } }, 404);
    }),
  );
  vi.stubGlobal("matchMedia", vi.fn((query: string) => ({ matches: true, media: query, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
});
afterEach(() => vi.unstubAllGlobals());

const agent = (attributes: Record<string, string>) => ({ isAgent: true, identity: "agent-1", attributes });

function renderCall(onClose = vi.fn()) {
  const user = userEvent.setup();
  render(<RealtimeCall clinicName="Aster" timeZone={IST} callHref="tel:+919000000100" open onClose={onClose} />);
  return { user, onClose };
}

async function connected() {
  await waitFor(() => expect(room().localParticipant.setMicrophoneEnabled).toHaveBeenCalledWith(true));
}

function stream(id: string, chunks: string[]) {
  return {
    info: { id, attributes: { "lk.segment_id": id } },
    async *[Symbol.asyncIterator]() {
      for (const chunk of chunks) yield chunk;
    },
  };
}

describe("real-time call helpers", () => {
  it("reads the agent state, attributes and captions", () => {
    expect(agentPhase("speaking")).toBe("speaking");
    expect(agentPhase("initializing")).toBe("connecting");
    expect(agentPhase(undefined)).toBe("connecting");
    expect(parseAttribute('{"a":1}')).toEqual({ a: 1 });
    for (const bad of [undefined, "", "not json", "3", "null"]) expect(parseAttribute(bad)).toBeNull();
    let lines = setCaption([], "s1", "caller", "You can");
    lines = setCaption(lines, "s1", "caller", "You can book a cleaning");
    expect(lines).toEqual([{ id: "s1", who: "caller", text: "You can book a cleaning" }]);
    for (let i = 2; i <= 6; i++) lines = setCaption(lines, `s${i}`, "caller", "x");
    expect(lines.map((l) => l.id)).toEqual(["s3", "s4", "s5", "s6"]);
  });
});

describe("real-time call screen", () => {
  it("joins the room with the minted token and follows the agent", async () => {
    renderCall();
    await connected();
    expect(calls.map((c) => c.url)).toEqual(["/api/chat/sessions", `/api/chat/sessions/${SESSION}/call`]);
    expect(room().connect).toHaveBeenCalledWith(TOKEN.server_url, TOKEN.participant_token);
    const screenEl = screen.getByRole("group", { name: "Voice call" });

    act(() => room().emit("participantAttributesChanged", {}, agent({ "lk.agent.state": "speaking" })));
    expect(within(screenEl).getByText("Speaking")).toBeInTheDocument();

    await act(async () => {
      room().textHandler(stream("seg1", ["Hello, ", "how can I help?"]), { identity: "agent-1" });
      // The caller's interim guesses arrive as new streams for the same segment.
      room().textHandler(stream("seg2", ["A clean"]), { identity: "caller-1" });
      room().textHandler(stream("seg2", ["A cleaning please"]), { identity: "caller-1" });
    });
    const captions = screen.getByRole("list", { name: "Live captions" });
    expect(within(captions).getByText("Hello, how can I help?")).toBeInTheDocument();
    expect(within(captions).getByText("A cleaning please")).toBeInTheDocument();

    act(() =>
      room().emit(
        "participantAttributesChanged",
        {},
        agent({
          "lk.agent.state": "listening",
          "call.held": JSON.stringify({ doctor_name: "Dr. Kavya Iyer", service_name: "Check-up and cleaning", starts_at: booking.starts_at }),
        }),
      ),
    );
    expect(screen.getByRole("region", { name: "Time held" })).toBeInTheDocument();

    act(() => room().emit("participantAttributesChanged", {}, agent({ "lk.agent.state": "listening", "call.held": "", "call.booking": JSON.stringify(booking) })));
    expect(screen.queryByRole("region", { name: "Time held" })).not.toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Booking" })).getByText("KNKDRNKV")).toBeInTheDocument();
  });

  it("mutes, ends the call, and keeps the booking on the end screen", async () => {
    const { user } = renderCall();
    await connected();
    act(() => room().emit("participantAttributesChanged", {}, agent({ "lk.agent.state": "listening", "call.booking": JSON.stringify(booking) })));
    await user.click(screen.getByRole("button", { name: "Mute" }));
    expect(room().localParticipant.setMicrophoneEnabled).toHaveBeenLastCalledWith(false);
    expect(screen.getByRole("button", { name: "Unmute" })).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("button", { name: "End call" }));
    expect(room().disconnect).toHaveBeenCalled();
    expect(screen.getByText(/Call ended/)).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Booking" })).getByText("KNKDRNKV")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows the end screen when the agent hangs up", async () => {
    renderCall();
    await connected();
    const current = room();
    act(() => current.emit("disconnected"));
    expect(screen.getByText(/Call ended/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Call again" })).toBeInTheDocument();
  });

  it("says calls are unavailable when no token can be minted", async () => {
    tokenStatus = 503;
    renderCall();
    expect(await screen.findByRole("alert")).toHaveTextContent(VOICE_MESSAGES.unavailable);
    expect(room().connect).not.toHaveBeenCalled();
    expect(screen.getByRole("link", { name: "Call the clinic" })).toHaveAttribute("href", "tel:+919000000100");
  });

  it("explains a blocked microphone and leaves the room", async () => {
    lk.room.micError = Object.assign(new Error("denied"), { name: "NotAllowedError" });
    renderCall();
    expect(await screen.findByRole("alert")).toHaveTextContent(VOICE_MESSAGES.micDenied);
    expect(room().disconnect).toHaveBeenCalled();
  });

  it("closing the panel leaves the room without the end screen", async () => {
    const { unmount } = render(<RealtimeCall clinicName="Aster" timeZone={IST} callHref={null} open onClose={vi.fn()} />);
    await connected();
    unmount();
    expect(room().disconnect).toHaveBeenCalled();
  });
});
