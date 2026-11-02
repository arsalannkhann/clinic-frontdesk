"use client";

import { CalendarCheck2, Clock, MessageSquare, Mic, MicOff, Phone, PhoneOff, RotateCcw, Sparkles } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { api, ApiError } from "@/lib/api/client";
import type { BookingResult, ChatSessionCreated, VoiceGreeting, VoiceTurnReply } from "@/lib/api/types";
import { TURN_IN_PROGRESS } from "@/lib/chat";
import { cx } from "@/lib/cx";
import { patientBookingStatus } from "@/lib/labels";
import { formatLongDate, formatTime } from "@/lib/time";
import { createPlayer, type Player } from "@/lib/voice/player";
import { createRecorder, MicError, type Recorder } from "@/lib/voice/recorder";
import { LabelBadge } from "@/components/ui/Badge";
import { Button, ButtonLink } from "@/components/ui/Button";

/**
 * The web voice call: a phone-call screen of its own, apart from the text chat. Each call
 * creates a fresh chat session (never the chat's stored one), records with the energy VAD and
 * plays the spoken replies. The assistant books by voice (name, mobile, consent read back),
 * so a held time never shows a card here; a booking the turn returns is shown on the call.
 */

export const VOICE_MESSAGES = {
  micDenied:
    "Microphone access was blocked, so the call couldn't start. Allow the microphone in your browser settings and call again, or chat with us or call the clinic instead.",
  micUnsupported: "This browser can't record audio for a call. You can chat with us or call the clinic instead.",
  micMissing: "No microphone was found. You can chat with us or call the clinic instead.",
  micFailed: "The microphone couldn't be started. You can chat with us or call the clinic instead.",
  noSpeech: "Didn't catch that. Please say it again.",
  allowMic: "Allow microphone access when your browser asks.",
  unavailable: "Voice calls are unavailable right now. You can chat with us, book online, or call the clinic instead.",
  limit: "This call has reached its limit. Call again, chat with us, or call the clinic.",
  failed: "I couldn't get a reply just now. Please say that again.",
  sessionLost: "Sorry, the call restarted. Please say that again.",
} as const;

export function micMessage(error: unknown): string {
  if (!(error instanceof MicError)) return VOICE_MESSAGES.micFailed;
  if (error.kind === "denied") return VOICE_MESSAGES.micDenied;
  if (error.kind === "unsupported") return VOICE_MESSAGES.micUnsupported;
  if (error.kind === "no_device") return VOICE_MESSAGES.micMissing;
  return VOICE_MESSAGES.micFailed;
}

const wait = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

export type CallPhase = "connecting" | "listening" | "hearing" | "thinking" | "speaking";

const LABELS: Record<CallPhase, string> = {
  connecting: "Connecting…",
  listening: "Listening…",
  hearing: "Listening…",
  thinking: "Thinking…",
  speaking: "Speaking…",
};

export function callLabel(phase: CallPhase, muted: boolean): string {
  if (muted && phase !== "thinking" && phase !== "speaking") return "Muted";
  return LABELS[phase];
}

/** "03:07" */
export function callClock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    const query = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    if (!query) return;
    setReduced(query.matches);
    const listener = () => setReduced(query.matches);
    query.addEventListener?.("change", listener);
    return () => query.removeEventListener?.("change", listener);
  }, []);
  return reduced;
}

/** A pulsing orb: steady when reduced motion is preferred, muted or connecting. */
function Orb({ phase, muted, reduced }: { phase: CallPhase; muted: boolean; reduced: boolean }) {
  const live = !reduced && !muted && phase !== "connecting";
  const tone = muted ? "bg-ink-3" : phase === "thinking" ? "bg-ink-2" : "bg-accent";
  return (
    <span className="relative grid size-44 shrink-0 place-items-center" aria-hidden="true">
      {live ? (
        <span
          className={cx(
            "absolute inset-0 rounded-full",
            tone,
            phase === "thinking" ? "animate-blink" : "animate-orb",
            phase === "hearing" && "[animation-duration:900ms]",
          )}
        />
      ) : null}
      <span
        className={cx(
          "relative grid size-28 place-items-center rounded-full text-paper shadow-float transition-transform duration-300",
          tone,
          live && phase === "hearing" && "scale-110",
        )}
      >
        <Sparkles className="size-10" />
      </span>
    </span>
  );
}

/** What was booked on the call, in the clinic's time zone. */
export function BookedPanel({ booking, timeZone }: { booking: BookingResult; timeZone: string }) {
  return (
    <section aria-label="Booking" className="w-full rounded-lg border border-ok/30 bg-raised p-4 text-left">
      <div className="mb-2 flex items-center gap-2 text-ok">
        <CalendarCheck2 className="size-5" aria-hidden="true" />
        <p className="font-semibold">{booking.status === "confirmed" ? "Booked" : "Booking received"}</p>
      </div>
      <p className="font-display text-lg leading-tight text-ink">{booking.service_name}</p>
      <p className="text-sm text-ink-2">with {booking.doctor_name}</p>
      <p className="mt-1 flex items-center gap-1.5 text-sm font-semibold text-ink tnum">
        <Clock className="size-4 text-accent" aria-hidden="true" />
        {formatLongDate(booking.starts_at, timeZone)}, {formatTime(booking.starts_at, timeZone)}
      </p>
      <dl className="mt-3 grid grid-cols-[auto_1fr] items-center gap-x-3 gap-y-1 text-sm">
        <dt className="text-ink-3">Reference</dt>
        <dd className="font-mono font-semibold tracking-wider text-ink">{booking.reference}</dd>
        <dt className="text-ink-3">Status</dt>
        <dd>
          <LabelBadge label={patientBookingStatus(booking.status)} />
        </dd>
      </dl>
    </section>
  );
}

interface CallSession {
  recorder: Recorder;
  player: Player;
  active: boolean;
  /** The chat session this call talks to; replaced if the server forgets it. */
  id: string | null;
  /** When the call connected (microphone ready and session created), for the timer. */
  connectedAt: number | null;
  /** A turn is being answered: utterances in the meantime are dropped. */
  busy: boolean;
}

interface Ended {
  seconds: number;
  /** Why the call ended itself (microphone blocked, voice unavailable…). */
  reason: string | null;
}

export interface VoiceCallProps {
  clinicName: string;
  timeZone: string;
  callHref: string | null;
  /** Opening starts a call; closing ends it without the end screen. */
  open: boolean;
  onClose: () => void;
  /** Switch to the text chat (offered on the end screen). */
  onChat?: () => void;
}

async function createSession(): Promise<string | null> {
  try {
    const session = await api<ChatSessionCreated>("/api/chat/sessions", { method: "POST", body: {} });
    return session.session_id;
  } catch {
    return null;
  }
}

/** The session and its greeting, fetched while the browser asks for the microphone. */
async function prepareCall(): Promise<{ id: string | null; greeting: VoiceGreeting | null; unavailable: boolean }> {
  const id = await createSession();
  if (!id) return { id, greeting: null, unavailable: true };
  try {
    const greeting = await api<VoiceGreeting>(`/api/chat/sessions/${encodeURIComponent(id)}/voice/greeting`, { method: "POST" });
    return { id, greeting, unavailable: false };
  } catch (error) {
    const unavailable = error instanceof ApiError && (error.code === "voice_unavailable" || error.status === 503);
    // Without a greeting the call still works: it goes straight to listening.
    return { id, greeting: null, unavailable };
  }
}

export function VoiceCall({ clinicName, timeZone, callHref, open, onClose, onChat }: VoiceCallProps) {
  const reduced = usePrefersReducedMotion();
  const [call, setCall] = useState<{ phase: CallPhase; muted: boolean; connected: boolean } | null>(null);
  // A brief note about the last turn ("Didn't catch that").
  const [note, setNote] = useState<string | null>(null);
  // The assistant's latest spoken line, as a caption.
  const [caption, setCaption] = useState("");
  const [booking, setBooking] = useState<BookingResult | null>(null);
  const [ended, setEnded] = useState<Ended | null>(null);
  const [seconds, setSeconds] = useState(0);
  const callRef = useRef<CallSession | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  const setPhase = useCallback((phase: CallPhase) => setCall((current) => (current ? { ...current, phase } : current)), []);

  /** Releases the microphone and stops playback; returns how long the call was connected. */
  const release = useCallback((): number => {
    const current = callRef.current;
    callRef.current = null;
    if (!current) return 0;
    current.active = false;
    current.recorder.stop();
    current.player.dispose();
    return current.connectedAt === null ? 0 : (Date.now() - current.connectedAt) / 1000;
  }, []);

  /** Ends the call and shows the end screen; `reason` says why, when it ended itself. */
  const endCall = useCallback(
    (reason: string | null = null) => {
      const duration = release();
      setCall(null);
      setNote(null);
      setEnded({ seconds: duration, reason });
    },
    [release],
  );

  const listen = useCallback(() => {
    const current = callRef.current;
    if (!current?.active) return;
    current.recorder.resume();
    setPhase("listening");
  }, [setPhase]);

  /** Plays a spoken reply with the microphone paused, so the assistant never hears itself. */
  const speak = useCallback(
    async (audio: string | null | undefined, mime: string | undefined) => {
      const current = callRef.current;
      if (!current?.active || !audio) return;
      current.recorder.pause();
      setPhase("speaking");
      await current.player.play(audio, mime);
    },
    [setPhase],
  );

  /** One spoken turn: the utterance goes up, the spoken reply (and any booking) comes back. */
  const voiceTurn = useCallback(
    async (audio: string) => {
      const current = callRef.current;
      if (!current?.active) return;
      current.recorder.pause();
      if (current.busy) {
        setNote(TURN_IN_PROGRESS);
        await wait(1500);
        listen();
        return;
      }
      current.busy = true;
      setNote(null);
      setPhase("thinking");
      let pause = 0;
      try {
        const id = current.id ?? (current.id = await createSession());
        if (!current.active) return;
        if (!id) {
          endCall(VOICE_MESSAGES.unavailable);
          return;
        }
        const reply = await api<VoiceTurnReply>(`/api/chat/sessions/${encodeURIComponent(id)}/voice`, {
          method: "POST",
          body: { audio },
        });
        if (!current.active) return;
        const said = reply.messages
          .filter((m) => m.role === "assistant" && m.text.trim())
          .map((m) => m.text.trim())
          .join(" ");
        if (said) setCaption(said);
        if (reply.booking) setBooking(reply.booking);
        current.busy = false;
        await speak(reply.audio, reply.audio_mime);
      } catch (error) {
        if (!current.active) return;
        const code = error instanceof ApiError ? error.code : "request_failed";
        const status = error instanceof ApiError ? error.status : 0;
        if (code === "no_speech") {
          setNote(VOICE_MESSAGES.noSpeech);
        } else if (code === "turn_in_progress") {
          // The server is still answering an earlier turn: wait, then listen again.
          setNote(TURN_IN_PROGRESS);
          pause = 1500;
        } else if (code === "conversation_limit") {
          endCall(VOICE_MESSAGES.limit);
          return;
        } else if (code === "voice_unavailable" || status === 503) {
          endCall(VOICE_MESSAGES.unavailable);
          return;
        } else if (status === 404) {
          // The server forgot this call's session: carry on in a new one.
          current.id = await createSession();
          setNote(VOICE_MESSAGES.sessionLost);
        } else if (status === 429) {
          setNote(error instanceof ApiError ? error.message : VOICE_MESSAGES.failed);
          pause = 3000;
        } else {
          setNote(VOICE_MESSAGES.failed);
        }
      } finally {
        current.busy = false;
      }
      if (pause) await wait(pause);
      listen();
    },
    [endCall, listen, setPhase, speak],
  );

  const startCall = useCallback(async () => {
    if (callRef.current) return;
    // Unlocked inside the tap where there is one, so mobile browsers let the replies play.
    const player = createPlayer();
    player.unlock();
    const session: CallSession = {
      player,
      active: true,
      id: null,
      connectedAt: null,
      busy: false,
      recorder: createRecorder({
        onSpeechStart: () => {
          if (!session.active) return;
          setNote(null);
          setPhase("hearing");
        },
        onUtterance: (audio) => void voiceTurn(audio),
      }),
    };
    callRef.current = session;
    setEnded(null);
    setBooking(null);
    setCaption("");
    setSeconds(0);
    setNote(VOICE_MESSAGES.allowMic);
    setCall({ phase: "connecting", muted: false, connected: false });
    // The session and greeting are fetched while the browser asks for the microphone.
    const prepared = prepareCall();
    try {
      await session.recorder.start();
    } catch (error) {
      if (callRef.current === session) endCall(micMessage(error));
      return;
    }
    if (!session.active) return;
    session.recorder.pause();
    const { id, greeting, unavailable } = await prepared;
    if (!session.active) return;
    if (unavailable) {
      endCall(VOICE_MESSAGES.unavailable);
      return;
    }
    session.id = id;
    session.connectedAt = Date.now();
    setNote(null);
    setCall((current) => (current ? { ...current, connected: true } : current));
    if (greeting) {
      if (greeting.text?.trim()) setCaption(greeting.text.trim());
      await speak(greeting.audio, greeting.audio_mime);
    }
    listen();
  }, [endCall, listen, setPhase, speak, voiceTurn]);

  function toggleMute() {
    const current = callRef.current;
    if (!current || !call) return;
    const muted = !call.muted;
    current.recorder.setMuted(muted);
    setCall({ ...call, muted });
  }

  // Opening starts a call; closing (or unmounting) releases the microphone and stops audio.
  useEffect(() => {
    if (!open) return;
    void startCall();
    return () => {
      release();
      setCall(null);
      setNote(null);
      setEnded(null);
    };
  }, [open, release, startCall]);

  // The call timer, once connected.
  const connected = call?.connected ?? false;
  useEffect(() => {
    if (!connected) return;
    const timer = window.setInterval(() => {
      const start = callRef.current?.connectedAt;
      if (start != null) setSeconds((Date.now() - start) / 1000);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [connected]);

  // The focused End or Call again button goes away when the call ends or restarts: focus moves
  // to its counterpart on the new screen.
  const isEnded = ended !== null;
  const wasEnded = useRef(isEnded);
  useEffect(() => {
    if (wasEnded.current === isEnded) return;
    wasEnded.current = isEnded;
    if (rootRef.current?.contains(document.activeElement) || document.activeElement === document.body) {
      rootRef.current?.querySelector<HTMLElement>("[data-autofocus]")?.focus();
    }
  }, [isEnded]);

  const header = (
    <header className="flex flex-col items-center gap-1 px-5 pt-8 text-center">
      <h2 id="call-title" className="font-display text-2xl leading-tight text-ink">
        {clinicName}
      </h2>
      <p id="call-subtitle" className="flex items-center gap-2 text-sm text-ink-3">
        AI receptionist
        <span className="rounded-full bg-accent-tint px-1.5 py-0.5 text-[0.625rem] font-semibold tracking-wide text-accent uppercase">AI</span>
      </p>
    </header>
  );

  if (ended) {
    return (
      <div ref={rootRef} className="flex h-full flex-col bg-paper text-ink">
        {header}
        <div className="flex flex-1 flex-col items-center gap-4 overflow-y-auto px-5 py-6 text-center">
          <span className="grid size-16 place-items-center rounded-full bg-sunk text-ink-2" aria-hidden="true">
            <PhoneOff className="size-7" />
          </span>
          <p className="font-semibold text-ink tnum">Call ended · {callClock(ended.seconds)}</p>
          {ended.reason ? (
            <p role="alert" className="rounded-md border border-warn/30 bg-warn-tint/70 px-3 py-2 text-sm text-ink">
              {ended.reason}
            </p>
          ) : null}
          {booking ? <BookedPanel booking={booking} timeZone={timeZone} /> : null}
          {booking ? (
            <p className="text-xs text-ink-3">Keep the reference. You can check this booking on the website with it and your phone number.</p>
          ) : null}
        </div>
        <div className="flex flex-wrap justify-center gap-2 border-t border-line px-5 py-4">
          <Button onClick={() => void startCall()} icon={<RotateCcw className="size-4" aria-hidden="true" />} data-autofocus>
            Call again
          </Button>
          {onChat ? (
            <Button variant="secondary" onClick={onChat} icon={<MessageSquare className="size-4" aria-hidden="true" />}>
              Chat instead
            </Button>
          ) : null}
          {ended.reason && callHref ? (
            <ButtonLink href={callHref} variant="secondary" icon={<Phone className="size-4" aria-hidden="true" />}>
              Call the clinic
            </ButtonLink>
          ) : null}
          <Button variant="ghost" onClick={onClose}>
            Close
          </Button>
        </div>
      </div>
    );
  }

  const phase = call?.phase ?? "connecting";
  const muted = call?.muted ?? false;
  return (
    <div ref={rootRef} role="group" aria-label="Voice call" className="flex h-full flex-col bg-paper text-ink">
      {header}
      <div className="flex flex-1 flex-col items-center justify-center gap-4 overflow-y-auto px-5 py-4 text-center">
        <Orb phase={phase} muted={muted} reduced={reduced} />
        <div role="status" aria-live="polite" className="flex flex-col items-center gap-1">
          <p className="text-lg font-semibold text-ink">{callLabel(phase, muted)}</p>
          {note ? <p className="text-sm text-ink-2">{note}</p> : null}
        </div>
        {connected ? (
          <p className="text-sm text-ink-3 tnum" aria-label={`Call time ${callClock(seconds)}`}>
            {callClock(seconds)}
          </p>
        ) : null}
        <p aria-live="polite" className="w-full max-w-sm truncate text-sm text-ink-2" title={caption || undefined}>
          {caption ? (
            <>
              <span className="sr-only">Assistant: </span>
              {caption}
            </>
          ) : null}
        </p>
        {phase === "speaking" ? (
          <button
            type="button"
            onClick={() => callRef.current?.player.stop()}
            className="rounded-full border border-line-strong bg-raised px-4 py-1.5 text-sm font-semibold text-ink hover:border-accent hover:text-accent"
          >
            Tap to interrupt
          </button>
        ) : null}
        {booking ? <BookedPanel booking={booking} timeZone={timeZone} /> : null}
      </div>
      <div className="flex items-start justify-center gap-10 px-5 pt-2 pb-8">
        <div className="flex flex-col items-center gap-1.5">
          <button
            type="button"
            onClick={toggleMute}
            disabled={!call}
            aria-label={muted ? "Unmute" : "Mute"}
            aria-pressed={muted}
            className={cx(
              "grid size-16 place-items-center rounded-full border border-line-strong bg-raised text-ink hover:bg-sunk disabled:opacity-50",
              muted && "border-ink bg-ink text-paper hover:bg-ink",
            )}
          >
            {muted ? <MicOff className="size-6" aria-hidden="true" /> : <Mic className="size-6" aria-hidden="true" />}
          </button>
          <span className="text-xs text-ink-3" aria-hidden="true">
            {muted ? "Unmute" : "Mute"}
          </span>
        </div>
        <div className="flex flex-col items-center gap-1.5">
          <button
            type="button"
            onClick={() => endCall()}
            aria-label="End call"
            data-autofocus
            className="grid size-16 place-items-center rounded-full bg-bad text-white shadow-float hover:bg-bad/90"
          >
            <PhoneOff className="size-6" aria-hidden="true" />
          </button>
          <span className="text-xs text-ink-3" aria-hidden="true">
            End
          </span>
        </div>
      </div>
    </div>
  );
}
