"use client";

import { AudioLines, Phone, RotateCcw, SendHorizontal, Sparkles, X } from "lucide-react";
import { useRouter } from "next/navigation";
import { Fragment, useCallback, useEffect, useReducer, useRef, useState, type FormEvent, type KeyboardEvent as ReactKeyboardEvent } from "react";

import { api, ApiError, errorMessage, newIdempotencyKey } from "@/lib/api/client";
import type { BookingResult, ChatReply, ChatSessionCreated, ChatTranscript, VoiceGreeting, VoiceTurnReply } from "@/lib/api/types";
import {
  chatReducer,
  CONFIRM_ERRORS,
  CONVERSATION_LIMIT,
  FALLBACK_REPLIES,
  initialChat,
  MAX_MESSAGE,
  NEW_CHAT,
  readStoredBooking,
  readStoredSession,
  storeBooking,
  storeSession,
  TURN_IN_PROGRESS,
  validMessage,
  visibleCount,
  type ChatLine,
} from "@/lib/chat";
import { cx } from "@/lib/cx";
import { createPlayer, type Player } from "@/lib/voice/player";
import { createRecorder, MicError, type Recorder } from "@/lib/voice/recorder";
import { CallBar, type CallPhase } from "./CallBar";
import { ProposalCard } from "./ProposalCard";

export const OPEN_CHAT_EVENT = "aster:open-chat";

/** "call" opens the chat straight into a web voice call with the assistant. */
export type ChatOpenMode = "text" | "call";

export function openChat(prompt?: string, options: { mode?: ChatOpenMode } = {}) {
  window.dispatchEvent(new CustomEvent(OPEN_CHAT_EVENT, { detail: { prompt, mode: options.mode ?? "text" } }));
}

interface ChatWidgetProps {
  clinicName: string;
  timeZone: string;
  callHref: string | null;
  /** The clinic offers the web voice call (profile voice_enabled). */
  voiceEnabled?: boolean;
}

/* ---------- voice call messages ---------- */

export const VOICE_MESSAGES = {
  micDenied:
    "Microphone access was blocked, so the voice call couldn't start. You can keep typing here, or allow the microphone in your browser settings and tap Talk again.",
  micUnsupported: "This browser can't record audio for a voice call. You can keep typing here.",
  micMissing: "No microphone was found. You can keep typing here.",
  micFailed: "The microphone couldn't be started. You can keep typing here.",
  noSpeech: "Didn't catch that. Please say it again.",
  allowMic: "Allow microphone access when your browser asks.",
  unavailable: "Voice calls are unavailable right now. You can keep typing here, book online, or call the clinic.",
  failed: "I couldn't get a reply just now. Please say that again.",
  sessionLost: "Sorry, the conversation restarted. Please say that again.",
} as const;

function micMessage(error: unknown): string {
  if (!(error instanceof MicError)) return VOICE_MESSAGES.micFailed;
  if (error.kind === "denied") return VOICE_MESSAGES.micDenied;
  if (error.kind === "unsupported") return VOICE_MESSAGES.micUnsupported;
  if (error.kind === "no_device") return VOICE_MESSAGES.micMissing;
  return VOICE_MESSAGES.micFailed;
}

const wait = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

interface CallSession {
  recorder: Recorder;
  player: Player;
  active: boolean;
}

function usePrefersReducedMotion(): boolean {
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

/** Reveals the newest assistant line word by word; the full text is always in the DOM for
 * assistive technology. */
function RevealText({ text, animate }: { text: string; animate: boolean }) {
  const words = text.split(/(\s+)/);
  const [shown, setShown] = useState(animate ? 0 : words.length);
  useEffect(() => {
    if (!animate) return;
    if (shown >= words.length) return;
    const timer = window.setTimeout(() => setShown((n) => Math.min(words.length, n + 2)), 28);
    return () => window.clearTimeout(timer);
  }, [animate, shown, words.length]);
  if (!animate || shown >= words.length) return <>{text}</>;
  return (
    <>
      <span className="sr-only">{text}</span>
      <span aria-hidden="true">{words.slice(0, shown).join("")}</span>
    </>
  );
}

function Bubble({ line, animate }: { line: ChatLine; animate: boolean }) {
  if (line.role === "system") {
    return <p className="mx-auto rounded-full bg-sunk px-3 py-1 text-xs text-ink-2">{line.text}</p>;
  }
  const mine = line.role === "user";
  return (
    <div className={cx("flex", mine ? "justify-end" : "justify-start")}>
      <p
        className={cx(
          "max-w-[85%] rounded-lg px-3.5 py-2.5 text-[0.95rem] leading-relaxed whitespace-pre-wrap",
          mine ? "rounded-br-sm bg-ink text-paper" : "rounded-bl-sm border border-line bg-raised text-ink",
        )}
      >
        <span className="sr-only">{mine ? "You: " : "Assistant: "}</span>
        {mine ? line.text : <RevealText text={line.text} animate={animate} />}
      </p>
    </div>
  );
}

export function ChatPanel({
  clinicName,
  timeZone,
  callHref,
  voiceEnabled = false,
  open = true,
  callRequest = 0,
  onClose,
}: ChatWidgetProps & {
  onClose?: () => void;
  /** False while the launcher hides the panel: a voice call ends when it closes. */
  open?: boolean;
  /** Incremented to start a voice call (the "Talk to our AI receptionist" buttons). */
  callRequest?: number;
}) {
  const [state, dispatch] = useReducer(chatReducer, initialChat);
  const [draft, setDraft] = useState("");
  const router = useRouter();
  const reduced = usePrefersReducedMotion();
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const logRef = useRef<HTMLDivElement>(null);
  const sessionRef = useRef<string | null>(null);
  const confirmKey = useRef<Map<string, string>>(new Map());
  const [lastAnimated, setLastAnimated] = useState<string | null>(null);
  // One message at a time: set while a send is in flight, so Enter, a quick reply or Try again
  // can't send a second message before the first is answered.
  const inFlight = useRef(false);
  // The first start or resume, so a voice call started on open waits for it instead of
  // creating a second session.
  const sessionReady = useRef<Promise<unknown> | null>(null);
  const [call, setCall] = useState<{ phase: CallPhase; muted: boolean } | null>(null);
  // During a call: a brief note in the call bar. Otherwise: why the call ended or never began.
  const [callMessage, setCallMessage] = useState<string | null>(null);
  const callRef = useRef<CallSession | null>(null);

  const startSession = useCallback(async (): Promise<string | null> => {
    dispatch({ type: "starting" });
    try {
      const session = await api<ChatSessionCreated>("/api/chat/sessions", { method: "POST", body: {} });
      sessionRef.current = session.session_id;
      storeSession(session.session_id);
      dispatch({ type: "started", session });
      return session.session_id;
    } catch (error) {
      dispatch({ type: "startFailed", message: errorMessage(error) });
      return null;
    }
  }, []);

  /** Reload the stored conversation; a forgotten one (404) is replaced by a new session. */
  const resumeSession = useCallback(
    async (stored: string): Promise<void> => {
      sessionRef.current = stored;
      dispatch({ type: "starting" });
      try {
        const transcript = await api<ChatTranscript>(`/api/chat/sessions/${encodeURIComponent(stored)}`);
        dispatch({ type: "restored", sessionId: stored, transcript, booked: readStoredBooking(stored) });
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) {
          storeSession(null);
          sessionRef.current = null;
          await startSession();
          return;
        }
        // The transcript could not be fetched (offline, timeout): carry on in the same
        // session without the earlier messages.
        dispatch({ type: "resumed", sessionId: stored });
      }
    },
    [startSession],
  );

  // Start or resume exactly once per mount. The ref keeps StrictMode's double effect run (and
  // any re-render) from creating a second session.
  const initialised = useRef(false);
  useEffect(() => {
    if (initialised.current) return;
    initialised.current = true;
    const stored = readStoredSession();
    sessionReady.current = stored ? resumeSession(stored) : startSession();
  }, [resumeSession, startSession]);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Keep the newest message in view.
  useEffect(() => {
    const log = logRef.current;
    if (log) log.scrollTop = log.scrollHeight;
  }, [state.lines.length, state.typing, state.proposalState, callMessage]);

  // Expire the card when the server-side proposal does.
  useEffect(() => {
    if (!state.proposal || state.proposalState !== "open") return;
    const ms = Date.parse(state.proposal.expires_at) - Date.now();
    if (!Number.isFinite(ms)) return;
    const timer = window.setTimeout(() => dispatch({ type: "expire" }), Math.max(0, ms));
    return () => window.clearTimeout(timer);
  }, [state.proposal, state.proposalState]);

  useEffect(() => {
    const last = state.lines[state.lines.length - 1];
    if (last?.role === "assistant") setLastAnimated(last.id);
  }, [state.lines]);

  /* ---------- voice call ---------- */

  const setPhase = useCallback((phase: CallPhase) => setCall((current) => (current ? { ...current, phase } : current)), []);

  /** Ends the call and releases the microphone; `message` says why, when it ended itself. */
  const endCall = useCallback((message: string | null = null) => {
    const current = callRef.current;
    callRef.current = null;
    if (current) {
      current.active = false;
      current.recorder.stop();
      current.player.dispose();
    }
    setCall(null);
    setCallMessage(message);
  }, []);

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

  /** One spoken turn: the utterance goes up, the transcript and reply come back into the log. */
  const voiceTurn = useCallback(
    async (audio: string) => {
      const current = callRef.current;
      if (!current?.active) return;
      current.recorder.pause();
      if (inFlight.current) {
        // A typed message is still being answered; this utterance is dropped.
        setCallMessage(TURN_IN_PROGRESS);
        await wait(1500);
        listen();
        return;
      }
      inFlight.current = true;
      setCallMessage(null);
      setPhase("thinking");
      dispatch({ type: "voicePending" });
      let pause = 0;
      try {
        const id = sessionRef.current ?? (await startSession());
        if (!id) {
          dispatch({ type: "voiceIdle" });
          endCall(VOICE_MESSAGES.unavailable);
          return;
        }
        const reply = await api<VoiceTurnReply>(`/api/chat/sessions/${encodeURIComponent(id)}/voice`, {
          method: "POST",
          body: { audio },
        });
        dispatch({ type: "voiceTurn", transcript: reply.transcript ?? "", reply });
        inFlight.current = false;
        await speak(reply.audio, reply.audio_mime);
      } catch (error) {
        dispatch({ type: "voiceIdle" });
        const code = error instanceof ApiError ? error.code : "request_failed";
        const status = error instanceof ApiError ? error.status : 0;
        if (code === "no_speech") {
          setCallMessage(VOICE_MESSAGES.noSpeech);
        } else if (code === "turn_in_progress") {
          // The server is still answering an earlier turn: wait, then listen again.
          setCallMessage(TURN_IN_PROGRESS);
          pause = 1500;
        } else if (code === "conversation_limit") {
          endCall();
          dispatch({ type: "replyFailed", message: CONVERSATION_LIMIT, final: true });
          return;
        } else if (code === "voice_unavailable" || status === 503) {
          endCall(VOICE_MESSAGES.unavailable);
          return;
        } else if (status === 404) {
          storeSession(null);
          sessionRef.current = null;
          dispatch({ type: "sessionLost" });
          await startSession();
          setCallMessage(VOICE_MESSAGES.sessionLost);
        } else if (status === 429) {
          setCallMessage(error instanceof ApiError ? error.message : VOICE_MESSAGES.failed);
          pause = 3000;
        } else {
          setCallMessage(VOICE_MESSAGES.failed);
        }
      } finally {
        inFlight.current = false;
      }
      if (pause) await wait(pause);
      listen();
    },
    [endCall, listen, setPhase, speak, startSession],
  );

  const startCall = useCallback(async () => {
    if (callRef.current) return;
    // Unlocked inside the tap, so mobile browsers let the replies play later.
    const player = createPlayer();
    player.unlock();
    const session: CallSession = {
      player,
      active: true,
      recorder: createRecorder({
        onSpeechStart: () => {
          if (!session.active) return;
          setCallMessage(null);
          setPhase("hearing");
        },
        onUtterance: (audio) => void voiceTurn(audio),
      }),
    };
    callRef.current = session;
    setCallMessage(VOICE_MESSAGES.allowMic);
    setCall({ phase: "connecting", muted: false });
    try {
      await session.recorder.start();
    } catch (error) {
      if (callRef.current === session) endCall(micMessage(error));
      return;
    }
    if (!session.active) return;
    setCallMessage(null);
    session.recorder.pause();
    if (sessionReady.current) await sessionReady.current.catch(() => undefined);
    const id = sessionRef.current ?? (await startSession());
    if (!session.active) return;
    if (!id) {
      endCall(VOICE_MESSAGES.unavailable);
      return;
    }
    try {
      const greeting = await api<VoiceGreeting>(`/api/chat/sessions/${encodeURIComponent(id)}/voice/greeting`, { method: "POST" });
      if (!session.active) return;
      dispatch({ type: "voiceGreeting", text: greeting.text ?? "" });
      await speak(greeting.audio, greeting.audio_mime);
    } catch (error) {
      if (error instanceof ApiError && (error.code === "voice_unavailable" || error.status === 503)) {
        endCall(VOICE_MESSAGES.unavailable);
        return;
      }
      // Without a greeting the call still works: go straight to listening.
    }
    listen();
  }, [endCall, listen, setPhase, speak, startSession, voiceTurn]);

  function toggleMute() {
    const current = callRef.current;
    if (!current || !call) return;
    const muted = !call.muted;
    current.recorder.setMuted(muted);
    setCall({ ...call, muted });
  }

  // A call request from outside (the landing page's Talk buttons), once per request.
  const handledRequest = useRef(0);
  useEffect(() => {
    if (!callRequest || callRequest === handledRequest.current) return;
    handledRequest.current = callRequest;
    if (voiceEnabled) void startCall();
  }, [callRequest, voiceEnabled, startCall]);

  // Hiding the panel ends the call.
  useEffect(() => {
    if (!open && callRef.current) endCall();
  }, [open, endCall]);

  // Unmounting releases the microphone and stops playback.
  useEffect(
    () => () => {
      const current = callRef.current;
      callRef.current = null;
      if (current) {
        current.active = false;
        current.recorder.stop();
        current.player.dispose();
      }
    },
    [],
  );

  /** Sends one message; false when nothing was sent because a reply is still pending. */
  async function send(text: string, retried = false): Promise<boolean> {
    const message = validMessage(text);
    if (!message) return false;
    if (inFlight.current) return false;
    inFlight.current = true;
    try {
      await deliver(message, retried);
    } finally {
      inFlight.current = false;
    }
    return true;
  }

  async function deliver(message: string, retried: boolean): Promise<void> {
    let id = sessionRef.current;
    dispatch(retried ? { type: "retrying" } : { type: "send", text: message });
    if (!id) id = await startSession();
    if (!id) {
      dispatch({ type: "replyFailed", message: "The assistant is unavailable right now." });
      return;
    }
    try {
      const reply = await api<ChatReply>(`/api/chat/sessions/${encodeURIComponent(id)}/messages`, {
        method: "POST",
        body: { text: message },
      });
      dispatch({ type: "reply", reply });
    } catch (error) {
      if (error instanceof ApiError && error.status === 404 && !retried) {
        // The server forgot this conversation (expired or purged): start afresh once.
        storeSession(null);
        sessionRef.current = null;
        dispatch({ type: "sessionLost" });
        const fresh = await startSession();
        if (fresh) return deliver(message, true);
      }
      if (error instanceof ApiError && error.code === "turn_in_progress") {
        // The server is still answering an earlier message (another tab, or a reply that
        // outlived its request). Nothing was sent: the text goes back in the input.
        dispatch({ type: "turnBusy", unsend: !retried });
        if (!retried) setDraft((current) => (current.trim() ? current : message));
        return;
      }
      if (error instanceof ApiError && error.code === "conversation_limit") {
        dispatch({ type: "replyFailed", message: CONVERSATION_LIMIT, final: true });
        return;
      }
      dispatch({
        type: "replyFailed",
        message:
          error instanceof ApiError && error.status === 429
            ? error.message
            : "I couldn't get a reply just now. You can try again, book online, or call the clinic.",
      });
    }
  }

  function onQuickReply(text: string) {
    if (text === NEW_CHAT) {
      storeSession(null);
      sessionRef.current = null;
      dispatch({ type: "reset" });
      void startSession();
      return;
    }
    if (FALLBACK_REPLIES.includes(text)) {
      if (text === "Book online") router.push("/book");
      else if (text === "Request a call back") router.push("/callback");
      else if (callHref) window.location.href = callHref;
      return;
    }
    void send(text);
  }

  async function confirm(details: { name: string; phone: string; consent_contact: boolean }) {
    const proposal = state.proposal;
    const id = sessionRef.current;
    if (!proposal || !id) return;
    dispatch({ type: "confirming" });
    // One key per proposal and details: a retry of the same request replays it, while edited
    // details are a new request (the API answers idempotency_conflict to a reused key).
    const slot = `${proposal.id}\n${details.name}\n${details.phone}\n${details.consent_contact}`;
    let key = confirmKey.current.get(slot);
    if (!key) {
      key = newIdempotencyKey("chat");
      confirmKey.current.set(slot, key);
    }
    const after = visibleCount(state.lines);
    try {
      const result = await api<BookingResult>(
        `/api/chat/sessions/${encodeURIComponent(id)}/proposals/${encodeURIComponent(proposal.id)}/confirm`,
        { method: "POST", body: details, idempotencyKey: key },
      );
      storeBooking({ sessionId: id, proposal, booking: result, after });
      dispatch({ type: "confirmed", result });
    } catch (error) {
      const code = error instanceof ApiError ? error.code : "request_failed";
      if (code === "idempotency_conflict") confirmKey.current.delete(slot);
      dispatch({ type: "confirmFailed", message: CONFIRM_ERRORS[code] ?? errorMessage(error), code });
    }
  }

  function changeProposal() {
    if (inFlight.current) return;
    dispatch({ type: "withdraw" });
    void send("Show me a different time");
  }

  const busy = state.typing || state.status === "starting";

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    // Enter submits the form even while the send button is disabled: keep the text until the
    // pending reply has arrived.
    if (busy || inFlight.current) return;
    const text = draft;
    if (!validMessage(text)) return;
    setDraft("");
    void send(text);
  }

  // The card sits right after the message that offered it; if that line is gone, at the end.
  const card =
    state.proposal && state.proposalState ? (
      <ProposalCard
        proposal={state.proposal}
        state={state.proposalState}
        error={state.proposalError}
        booking={state.booking}
        timeZone={timeZone}
        onConfirm={confirm}
        onChange={changeProposal}
      />
    ) : null;
  const cardAnchor =
    state.proposalAfter && state.lines.some((line) => line.id === state.proposalAfter) ? state.proposalAfter : null;

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-3">
        <div className="flex items-center gap-3">
          <span className="grid size-10 shrink-0 place-items-center rounded-full bg-accent text-paper" aria-hidden="true">
            <Sparkles className="size-5" />
          </span>
          <div>
            <h2 id="chat-title" className="flex items-center gap-2 font-display text-lg leading-tight">
              AI booking assistant
              <span className="rounded-full bg-accent-tint px-1.5 py-0.5 font-sans text-[0.625rem] font-semibold tracking-wide text-accent uppercase">
                AI
              </span>
            </h2>
            <p className="text-xs text-ink-3">{clinicName} · books real open slots · no medical advice</p>
          </div>
        </div>
        <div className="flex items-center gap-1">
          {voiceEnabled && !call ? (
            <button
              type="button"
              onClick={() => void startCall()}
              className="rounded-md p-2 text-accent hover:bg-accent-tint hover:text-accent-strong"
              aria-label="Talk to the AI receptionist"
              title="Talk to the AI receptionist"
            >
              <AudioLines className="size-5" aria-hidden="true" />
            </button>
          ) : null}
          {callHref ? (
            <a href={callHref} className="rounded-md p-2 text-ink-2 hover:bg-sunk hover:text-ink" aria-label="Call the clinic">
              <Phone className="size-4" aria-hidden="true" />
            </a>
          ) : null}
          {onClose ? (
            <button type="button" onClick={onClose} className="rounded-md p-2 text-ink-2 hover:bg-sunk hover:text-ink" aria-label="Close chat">
              <X className="size-5" aria-hidden="true" />
            </button>
          ) : null}
        </div>
      </header>

      {call ? (
        <CallBar
          phase={call.phase}
          muted={call.muted}
          message={callMessage}
          reduced={reduced}
          onMute={toggleMute}
          onEnd={() => endCall()}
          onInterrupt={() => callRef.current?.player.stop()}
        />
      ) : null}

      <div ref={logRef} role="log" aria-live="polite" aria-label="Conversation" className="flex flex-1 flex-col gap-3 overflow-y-auto px-4 py-4">
        {state.status === "starting" && state.lines.length === 0 ? (
          <p className="text-sm text-ink-3">Connecting to the assistant…</p>
        ) : null}
        {state.status === "ready" && state.lines.length === 0 ? (
          <p className="mx-auto rounded-full bg-sunk px-3 py-1 text-xs text-ink-2">
            Continuing your conversation. Earlier messages couldn&apos;t be loaded.
          </p>
        ) : null}
        {state.lines.map((line) => (
          <Fragment key={line.id}>
            <Bubble line={line} animate={!reduced && line.id === lastAnimated} />
            {line.id === cardAnchor ? card : null}
          </Fragment>
        ))}
        {cardAnchor === null ? card : null}
        {state.typing ? (
          <div className="flex items-center gap-1 self-start rounded-lg border border-line bg-raised px-3.5 py-3" aria-label="Assistant is typing" role="status">
            {[0, 1, 2].map((i) => (
              <span key={i} className="size-1.5 animate-blink rounded-full bg-ink-3" style={{ animationDelay: `${i * 160}ms` }} />
            ))}
          </div>
        ) : null}
        {state.notice ? (
          <div role="status" className="self-start text-sm text-ink-3">
            <p>{state.notice}</p>
            {state.pending ? (
              <button type="button" disabled={busy} onClick={() => void send(state.pending!, true)} className="mt-1 inline-flex items-center gap-1 font-semibold text-accent hover:underline">
                <RotateCcw className="size-3.5" aria-hidden="true" /> Try again
              </button>
            ) : null}
          </div>
        ) : null}
        {!call && callMessage ? (
          <div role="alert" className="rounded-md border border-warn/30 bg-warn-tint/70 px-3 py-2 text-sm text-ink">
            <p>{callMessage}</p>
          </div>
        ) : null}
        {state.error ? (
          <div role="alert" className="rounded-md border border-warn/30 bg-warn-tint/70 px-3 py-2 text-sm text-ink">
            <p>{state.error}</p>
            {state.pending ? (
              <button type="button" disabled={busy} onClick={() => void send(state.pending!, true)} className="mt-1 inline-flex items-center gap-1 font-semibold text-accent hover:underline">
                <RotateCcw className="size-3.5" aria-hidden="true" /> Try again
              </button>
            ) : null}
          </div>
        ) : null}
      </div>

      {state.quickReplies.length ? (
        <div className="flex flex-wrap gap-2 border-t border-line px-4 pt-3" aria-label="Suggested replies">
          {state.quickReplies.map((reply) => (
            <button
              key={reply}
              type="button"
              disabled={busy}
              onClick={() => onQuickReply(reply)}
              className="rounded-full border border-line-strong bg-raised px-3 py-1.5 text-sm text-ink transition-colors hover:border-accent hover:text-accent disabled:opacity-50"
            >
              {reply}
            </button>
          ))}
        </div>
      ) : null}

      <form onSubmit={onSubmit} className="flex items-end gap-2 px-4 py-3">
        <label htmlFor="chat-input" className="sr-only">
          Message
        </label>
        <textarea
          id="chat-input"
          ref={inputRef}
          rows={1}
          maxLength={MAX_MESSAGE}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              (e.currentTarget.form as HTMLFormElement | null)?.requestSubmit();
            }
          }}
          placeholder="Ask about a time, a treatment or our hours"
          className="max-h-32 min-h-10 flex-1 resize-none rounded-md border border-line-strong bg-raised px-3 py-2 text-[0.95rem] placeholder:text-ink-3 focus:border-accent focus:outline-none"
        />
        <button
          type="submit"
          disabled={busy || !draft.trim()}
          className="inline-flex size-10 shrink-0 items-center justify-center rounded-md bg-accent text-white hover:bg-accent-strong disabled:opacity-45"
          aria-label="Send message"
        >
          <SendHorizontal className="size-4" aria-hidden="true" />
        </button>
      </form>
    </div>
  );
}

/** Below Tailwind's `sm` breakpoint the panel covers the screen and becomes a true modal. */
export const SMALL_SCREEN_QUERY = "(max-width: 639.98px)";

function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(false);
  useEffect(() => {
    const list = window.matchMedia?.(query);
    if (!list) return;
    setMatches(list.matches);
    const listener = () => setMatches(list.matches);
    list.addEventListener?.("change", listener);
    return () => list.removeEventListener?.("change", listener);
  }, [query]);
  return matches;
}

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * The floating launcher on every public page. The panel mounts on first open. On a phone
 * (under 640 px) it is a full-screen modal: aria-modal, Tab and Shift+Tab stay inside, the
 * page behind does not scroll, Escape closes it and focus goes back to the launcher. On larger
 * screens it is a non-modal panel beside the page; Escape still closes it.
 */
export function ChatLauncher(props: ChatWidgetProps & { enabled: boolean }) {
  const [open, setOpen] = useState(false);
  const [mounted, setMounted] = useState(false);
  const [callRequest, setCallRequest] = useState(0);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const wasOpen = useRef(false);
  const small = useMediaQuery(SMALL_SCREEN_QUERY);
  const modal = open && small;

  useEffect(() => {
    const onOpen = (event: Event) => {
      setMounted(true);
      setOpen(true);
      if ((event as CustomEvent<{ mode?: ChatOpenMode }>).detail?.mode === "call") setCallRequest((n) => n + 1);
    };
    window.addEventListener(OPEN_CHAT_EVENT, onOpen);
    return () => window.removeEventListener(OPEN_CHAT_EVENT, onOpen);
  }, []);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  // Focus moves into the panel on open and back to the launcher on close. This runs after the
  // render, when the launcher (hidden behind the modal on phones) is visible again.
  useEffect(() => {
    if (open && !wasOpen.current) {
      const input = dialogRef.current?.querySelector<HTMLElement>("#chat-input");
      (input ?? dialogRef.current?.querySelector<HTMLElement>(FOCUSABLE))?.focus();
    } else if (!open && wasOpen.current) {
      buttonRef.current?.focus();
    }
    wasOpen.current = open;
  }, [open]);

  // The page behind a full-screen modal must not scroll.
  useEffect(() => {
    if (!modal) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, [modal]);

  function trapFocus(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (!modal || event.key !== "Tab" || !dialogRef.current) return;
    const items = [...dialogRef.current.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((el) => !el.closest("[hidden]"));
    if (items.length === 0) {
      event.preventDefault();
      return;
    }
    const first = items[0]!;
    const last = items[items.length - 1]!;
    const active = document.activeElement as HTMLElement | null;
    const inside = active ? dialogRef.current.contains(active) : false;
    if (event.shiftKey && (active === first || !inside)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (active === last || !inside)) {
      event.preventDefault();
      first.focus();
    }
  }

  const { enabled, ...panelProps } = props;
  if (!enabled) return null;

  return (
    <>
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal={modal ? "true" : "false"}
        aria-labelledby="chat-title"
        hidden={!open}
        onKeyDown={trapFocus}
        className={cx(
          "fixed z-40 flex flex-col overflow-hidden border-line bg-paper shadow-float",
          "inset-0 sm:inset-auto sm:right-6 sm:bottom-24 sm:h-[min(40rem,calc(100dvh-8rem))] sm:w-[25rem] sm:rounded-lg sm:border",
          open && "animate-rise",
        )}
      >
        {mounted ? <ChatPanel {...panelProps} open={open} callRequest={callRequest} onClose={() => setOpen(false)} /> : null}
      </div>
      <button
        ref={buttonRef}
        type="button"
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => {
          setMounted(true);
          setOpen((value) => !value);
        }}
        className={cx(
          "fixed right-4 bottom-4 z-40 inline-flex h-12 items-center gap-2 rounded-full bg-ink px-5 font-semibold text-paper shadow-float transition-transform hover:-translate-y-0.5 sm:right-6 sm:bottom-6",
          open && "max-sm:hidden",
        )}
      >
        {open ? <X className="size-5" aria-hidden="true" /> : <Sparkles className="size-5" aria-hidden="true" />}
        {open ? "Close chat" : "Book with AI assistant"}
      </button>
    </>
  );
}
