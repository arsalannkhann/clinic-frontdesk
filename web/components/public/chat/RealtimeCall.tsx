"use client";

import { CalendarClock, MessageSquare, Mic, MicOff, Phone, PhoneOff, RotateCcw, Volume2 } from "lucide-react";
import { ConnectionState, Room, RoomEvent, Track, type Participant, type RemoteTrack, type TextStreamReader } from "livekit-client";
import { useCallback, useEffect, useRef, useState } from "react";

import { api, ApiError } from "@/lib/api/client";
import type { BookingResult, ChatSessionCreated } from "@/lib/api/types";
import { cx } from "@/lib/cx";
import { formatLongDate, formatTime } from "@/lib/time";
import { Button, ButtonLink } from "@/components/ui/Button";

import { BookedPanel, callClock, usePrefersReducedMotion, VOICE_MESSAGES, type VoiceCallProps } from "./VoiceCall";

/**
 * The real-time call (ADR 0014): the browser joins a LiveKit room over WebRTC and talks to the
 * call agent, which streams speech in both directions, can be interrupted, and hangs up when
 * the caller is done. The API mints the room token (POST /api/chat/sessions/{id}/call). The
 * agent publishes its state ("lk.agent.state"), live captions (the "lk.transcription" text
 * stream) and the held time and booking ("call.held", "call.booking" attributes).
 */

export type RealtimePhase = "connecting" | "listening" | "thinking" | "speaking";

const PHASE_LABELS: Record<RealtimePhase, string> = {
  connecting: "Connecting…",
  listening: "Listening",
  thinking: "One moment…",
  speaking: "Speaking",
};

/** The agent's "lk.agent.state" attribute as a call phase. */
export function agentPhase(state: string | undefined): RealtimePhase {
  if (state === "listening" || state === "thinking" || state === "speaking") return state;
  return "connecting";
}

export interface Held {
  doctor_name: string;
  service_name: string;
  starts_at: string;
}

/** A JSON participant attribute, or null when unset, empty or malformed. */
export function parseAttribute<T>(value: string | undefined): T | null {
  if (!value) return null;
  try {
    const parsed = JSON.parse(value) as unknown;
    return parsed && typeof parsed === "object" ? (parsed as T) : null;
  } catch {
    return null;
  }
}

export interface CaptionLine {
  id: string;
  who: "agent" | "caller";
  text: string;
}

/** Sets a segment's caption text (each transcription stream carries the segment's text so far,
 * and the caller's interim guesses arrive as new streams for the same segment); at most `keep`
 * lines stay. */
export function setCaption(lines: CaptionLine[], id: string, who: CaptionLine["who"], text: string, keep = 4): CaptionLine[] {
  if (lines.some((line) => line.id === id)) {
    return lines.map((line) => (line.id === id ? { ...line, text } : line));
  }
  return [...lines, { id, who, text }].slice(-keep);
}

interface CallToken {
  server_url: string;
  participant_token: string;
  room: string;
}

class CallUnavailable extends Error {}

async function requestCall(): Promise<CallToken> {
  let session: ChatSessionCreated;
  try {
    session = await api<ChatSessionCreated>("/api/chat/sessions", { method: "POST", body: {} });
  } catch {
    throw new CallUnavailable();
  }
  try {
    return await api<CallToken>(`/api/chat/sessions/${encodeURIComponent(session.session_id)}/call`, { method: "POST" });
  } catch (error) {
    if (error instanceof ApiError && error.status === 429) throw error;
    throw new CallUnavailable();
  }
}

/** The microphone failure (a getUserMedia DOMException) as the shared messages. */
function micFailure(error: unknown): string {
  const name = error instanceof Error ? error.name : "";
  if (name === "NotAllowedError" || name === "SecurityError") return VOICE_MESSAGES.micDenied;
  if (name === "NotFoundError" || name === "OverconstrainedError") return VOICE_MESSAGES.micMissing;
  if (typeof navigator !== "undefined" && !navigator.mediaDevices) return VOICE_MESSAGES.micUnsupported;
  return VOICE_MESSAGES.micFailed;
}

/** Drives a CSS variable (--level, 0..1) from an audio track's loudness, without re-rendering. */
function meter(track: MediaStreamTrack, target: HTMLElement, name: string): () => void {
  const AudioContextClass = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!AudioContextClass) return () => {};
  const context = new AudioContextClass();
  const analyser = context.createAnalyser();
  analyser.fftSize = 256;
  context.createMediaStreamSource(new MediaStream([track])).connect(analyser);
  const data = new Uint8Array(analyser.frequencyBinCount);
  let frame = 0;
  let level = 0;
  const tick = () => {
    analyser.getByteFrequencyData(data);
    let sum = 0;
    for (const value of data) sum += value;
    const now = Math.min(1, sum / data.length / 90);
    level = level * 0.7 + now * 0.3;
    target.style.setProperty(name, level.toFixed(3));
    frame = requestAnimationFrame(tick);
  };
  tick();
  return () => {
    cancelAnimationFrame(frame);
    target.style.setProperty(name, "0");
    void context.close();
  };
}

function HeldPanel({ held, timeZone }: { held: Held; timeZone: string }) {
  return (
    <section aria-label="Time held" className="w-full rounded-lg border border-white/15 bg-white/5 p-3 text-left text-white">
      <p className="flex items-center gap-2 text-xs font-semibold tracking-wide text-white/60 uppercase">
        <CalendarClock className="size-4" aria-hidden="true" /> Holding this time
      </p>
      <p className="mt-1 font-semibold">{held.service_name}</p>
      <p className="text-sm text-white/75">
        {formatLongDate(held.starts_at, timeZone)}, {formatTime(held.starts_at, timeZone)} · {held.doctor_name}
      </p>
    </section>
  );
}

/** The orb: rings follow the receptionist's voice (--agent) and the caller's (--caller). */
function Orb({ phase, muted, reduced }: { phase: RealtimePhase; muted: boolean; reduced: boolean }) {
  const live = !reduced && phase !== "connecting";
  return (
    <span className="relative grid size-56 shrink-0 place-items-center" aria-hidden="true">
      <span
        className={cx("absolute inset-0 rounded-full bg-accent/25 blur-2xl transition-opacity", phase === "connecting" && "opacity-40")}
        style={live ? { transform: "scale(calc(0.85 + var(--agent, 0) * 0.5))" } : undefined}
      />
      <span
        className="absolute inset-6 rounded-full border border-white/15"
        style={live ? { transform: "scale(calc(1 + var(--caller, 0) * 0.25))" } : undefined}
      />
      <span
        className={cx(
          "relative size-32 rounded-full shadow-float",
          "bg-[radial-gradient(circle_at_30%_30%,var(--color-accent-tint),var(--color-accent)_55%,color-mix(in_oklab,var(--color-accent),black_45%))]",
          phase === "thinking" && live && "animate-blink",
          muted && "grayscale",
        )}
        style={live ? { transform: "scale(calc(1 + var(--agent, 0) * 0.18))" } : undefined}
      />
    </span>
  );
}

interface Ended {
  seconds: number;
  reason: string | null;
}

export function RealtimeCall({ clinicName, timeZone, callHref, open, onClose, onChat }: VoiceCallProps) {
  const reduced = usePrefersReducedMotion();
  const [phase, setPhase] = useState<RealtimePhase>("connecting");
  const [active, setActive] = useState(false);
  const [muted, setMuted] = useState(false);
  const [needsAudio, setNeedsAudio] = useState(false);
  const [captions, setCaptions] = useState<CaptionLine[]>([]);
  const [held, setHeld] = useState<Held | null>(null);
  const [booking, setBooking] = useState<BookingResult | null>(null);
  const [ended, setEnded] = useState<Ended | null>(null);
  const [seconds, setSeconds] = useState(0);
  const [note, setNote] = useState<string | null>(null);
  const roomRef = useRef<Room | null>(null);
  const connectedAt = useRef<number | null>(null);
  const cleanups = useRef<(() => void)[]>([]);
  const rootRef = useRef<HTMLDivElement>(null);
  const audioRef = useRef<HTMLDivElement>(null);

  const release = useCallback((): number => {
    const room = roomRef.current;
    roomRef.current = null;
    for (const cleanup of cleanups.current.splice(0)) cleanup();
    if (room) void room.disconnect();
    const start = connectedAt.current;
    connectedAt.current = null;
    return start === null ? 0 : (Date.now() - start) / 1000;
  }, []);

  const endCall = useCallback(
    (reason: string | null = null) => {
      const duration = release();
      setActive(false);
      setNote(null);
      setEnded({ seconds: duration, reason });
    },
    [release],
  );

  const readAgent = useCallback((participant: Participant) => {
    if (!participant.isAgent) return;
    setPhase(agentPhase(participant.attributes["lk.agent.state"]));
    setHeld(parseAttribute<Held>(participant.attributes["call.held"]));
    const made = parseAttribute<BookingResult>(participant.attributes["call.booking"]);
    if (made) setBooking(made);
  }, []);

  const startCall = useCallback(async () => {
    if (roomRef.current) return;
    setEnded(null);
    setCaptions([]);
    setHeld(null);
    setBooking(null);
    setSeconds(0);
    setMuted(false);
    setPhase("connecting");
    setActive(true);
    setNote(VOICE_MESSAGES.allowMic);
    const room = new Room({
      audioCaptureDefaults: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    roomRef.current = room;
    let token: CallToken;
    try {
      token = await requestCall();
    } catch (error) {
      if (roomRef.current !== room) return;
      endCall(error instanceof ApiError ? error.message : VOICE_MESSAGES.unavailable);
      return;
    }
    if (roomRef.current !== room) return;
    room.prepareConnection(token.server_url, token.participant_token);

    room
      .on(RoomEvent.TrackSubscribed, (track: RemoteTrack, _publication, participant) => {
        if (track.kind !== Track.Kind.Audio || !participant.isAgent) return;
        const element = track.attach();
        audioRef.current?.appendChild(element);
        if (rootRef.current) cleanups.current.push(meter(track.mediaStreamTrack, rootRef.current, "--agent"));
        cleanups.current.push(() => track.detach().forEach((el) => el.remove()));
      })
      .on(RoomEvent.ParticipantAttributesChanged, (_changed, participant) => readAgent(participant))
      .on(RoomEvent.ParticipantConnected, (participant) => readAgent(participant))
      .on(RoomEvent.AudioPlaybackStatusChanged, () => setNeedsAudio(!room.canPlaybackAudio))
      .on(RoomEvent.Disconnected, () => {
        if (roomRef.current === room) endCall(null);
      });
    room.registerTextStreamHandler("lk.transcription", (reader: TextStreamReader, from) => {
      const id = reader.info.attributes?.["lk.segment_id"] ?? reader.info.id;
      const who: CaptionLine["who"] = from.identity === room.localParticipant.identity ? "caller" : "agent";
      void (async () => {
        try {
          let text = "";
          for await (const chunk of reader) {
            text += chunk;
            const current = text;
            setCaptions((lines) => setCaption(lines, id, who, current));
          }
        } catch {
          /* The stream ends with the call. */
        }
      })();
    });

    try {
      await room.connect(token.server_url, token.participant_token);
    } catch {
      if (roomRef.current === room) endCall(VOICE_MESSAGES.unavailable);
      return;
    }
    try {
      await room.localParticipant.setMicrophoneEnabled(true);
    } catch (error) {
      if (roomRef.current === room) endCall(micFailure(error));
      return;
    }
    if (roomRef.current !== room) return;
    const mic = room.localParticipant.getTrackPublication(Track.Source.Microphone)?.track;
    if (mic && rootRef.current) cleanups.current.push(meter(mic.mediaStreamTrack, rootRef.current, "--caller"));
    connectedAt.current = Date.now();
    setNote(null);
    setNeedsAudio(!room.canPlaybackAudio);
    room.remoteParticipants.forEach(readAgent);
    // The agent joins within seconds; without it there is nobody to talk to.
    const timer = window.setTimeout(() => {
      if (roomRef.current === room && ![...room.remoteParticipants.values()].some((p) => p.isAgent)) {
        endCall(VOICE_MESSAGES.unavailable);
      }
    }, 15000);
    cleanups.current.push(() => window.clearTimeout(timer));
  }, [endCall, readAgent]);

  async function toggleMute() {
    const room = roomRef.current;
    if (!room || room.state !== ConnectionState.Connected) return;
    await room.localParticipant.setMicrophoneEnabled(muted);
    setMuted(!muted);
  }

  useEffect(() => {
    if (!open) return;
    void startCall();
    return () => {
      release();
      setActive(false);
      setEnded(null);
    };
  }, [open, release, startCall]);

  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => {
      if (connectedAt.current !== null) setSeconds((Date.now() - connectedAt.current) / 1000);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [active]);

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
    <header className="flex flex-col items-center gap-1.5 px-5 pt-8 text-center">
      <h2 id="call-title" className="font-display text-2xl leading-tight text-white">
        {clinicName}
      </h2>
      <p id="call-subtitle" className="flex items-center gap-2 text-sm text-white/60">
        AI receptionist
        {active && connectedAt.current !== null ? (
          <span className="flex items-center gap-1.5 rounded-full bg-white/10 px-2 py-0.5 text-[0.6875rem] font-semibold tracking-wide text-white uppercase">
            <span className="size-1.5 rounded-full bg-ok" aria-hidden="true" /> Live · <span className="tnum">{callClock(seconds)}</span>
          </span>
        ) : null}
      </p>
    </header>
  );

  if (ended) {
    return (
      <div ref={rootRef} className="flex h-full flex-col bg-ink text-white">
        {header}
        <div className="flex flex-1 flex-col items-center gap-4 overflow-y-auto px-5 py-6 text-center">
          <span className="grid size-16 place-items-center rounded-full bg-white/10" aria-hidden="true">
            <PhoneOff className="size-7" />
          </span>
          <p className="font-semibold tnum">Call ended · {callClock(ended.seconds)}</p>
          {ended.reason ? (
            <p role="alert" className="rounded-md border border-warn/40 bg-warn/15 px-3 py-2 text-sm text-white">
              {ended.reason}
            </p>
          ) : null}
          {booking ? (
            <div className="w-full text-ink">
              <BookedPanel booking={booking} timeZone={timeZone} />
            </div>
          ) : null}
          {booking ? <p className="text-xs text-white/60">Keep the reference. You can check this booking on the website with it and your phone number.</p> : null}
        </div>
        <div className="flex flex-wrap justify-center gap-2 border-t border-white/10 px-5 py-4">
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
          <Button variant="ghost" onClick={onClose} className="text-white hover:bg-white/10">
            Close
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div
      ref={rootRef}
      role="group"
      aria-label="Voice call"
      className="flex h-full flex-col bg-ink bg-[radial-gradient(ellipse_at_top,color-mix(in_oklab,var(--color-accent),transparent_70%),transparent_60%)] text-white"
    >
      {header}
      <div className="flex flex-1 flex-col items-center justify-center gap-5 overflow-y-auto px-5 py-4 text-center">
        <Orb phase={phase} muted={muted} reduced={reduced} />
        <div role="status" aria-live="polite" className="flex flex-col items-center gap-1">
          <p className="text-lg font-semibold">{muted && phase === "listening" ? "Muted" : PHASE_LABELS[phase]}</p>
          {note ? <p className="text-sm text-white/70">{note}</p> : null}
        </div>
        {needsAudio ? (
          <button
            type="button"
            onClick={() => void roomRef.current?.startAudio()}
            className="flex items-center gap-2 rounded-full bg-white px-4 py-2 text-sm font-semibold text-ink"
          >
            <Volume2 className="size-4" aria-hidden="true" /> Tap to hear the receptionist
          </button>
        ) : null}
        <ol aria-label="Live captions" aria-live="polite" className="flex w-full max-w-md flex-col gap-1.5 text-left">
          {captions.map((line) => (
            <li key={line.id} className={cx("text-sm leading-snug", line.who === "agent" ? "text-white" : "text-right text-white/55")}>
              <span className="sr-only">{line.who === "agent" ? "Receptionist: " : "You: "}</span>
              {line.text}
            </li>
          ))}
        </ol>
        {held && !booking ? <HeldPanel held={held} timeZone={timeZone} /> : null}
        {booking ? (
          <div className="w-full text-ink">
            <BookedPanel booking={booking} timeZone={timeZone} />
          </div>
        ) : null}
      </div>
      <div ref={audioRef} hidden />
      <div className="flex items-start justify-center gap-10 px-5 pt-2 pb-8">
        <div className="flex flex-col items-center gap-1.5">
          <button
            type="button"
            onClick={() => void toggleMute()}
            disabled={!active || phase === "connecting"}
            aria-label={muted ? "Unmute" : "Mute"}
            aria-pressed={muted}
            className={cx(
              "grid size-16 place-items-center rounded-full bg-white/10 text-white hover:bg-white/20 disabled:opacity-40",
              muted && "bg-white text-ink hover:bg-white",
            )}
          >
            {muted ? <MicOff className="size-6" aria-hidden="true" /> : <Mic className="size-6" aria-hidden="true" />}
          </button>
          <span className="text-xs text-white/60" aria-hidden="true">
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
          <span className="text-xs text-white/60" aria-hidden="true">
            End
          </span>
        </div>
      </div>
    </div>
  );
}
