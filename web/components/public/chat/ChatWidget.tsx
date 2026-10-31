"use client";

import { MessageCircle, Phone, RotateCcw, SendHorizontal, X } from "lucide-react";
import { useRouter } from "next/navigation";
import { Fragment, useCallback, useEffect, useReducer, useRef, useState, type FormEvent, type KeyboardEvent as ReactKeyboardEvent } from "react";

import { api, ApiError, errorMessage, newIdempotencyKey } from "@/lib/api/client";
import type { BookingResult, ChatReply, ChatSessionCreated, ChatTranscript } from "@/lib/api/types";
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
  validMessage,
  visibleCount,
  type ChatLine,
} from "@/lib/chat";
import { cx } from "@/lib/cx";
import { ProposalCard } from "./ProposalCard";

export const OPEN_CHAT_EVENT = "aster:open-chat";

export function openChat(prompt?: string) {
  window.dispatchEvent(new CustomEvent(OPEN_CHAT_EVENT, { detail: { prompt } }));
}

interface ChatWidgetProps {
  clinicName: string;
  timeZone: string;
  callHref: string | null;
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

export function ChatPanel({ clinicName, timeZone, callHref, onClose }: ChatWidgetProps & { onClose?: () => void }) {
  const [state, dispatch] = useReducer(chatReducer, initialChat);
  const [draft, setDraft] = useState("");
  const router = useRouter();
  const reduced = usePrefersReducedMotion();
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const logRef = useRef<HTMLDivElement>(null);
  const sessionRef = useRef<string | null>(null);
  const confirmKey = useRef<Map<string, string>>(new Map());
  const [lastAnimated, setLastAnimated] = useState<string | null>(null);

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
    if (stored) void resumeSession(stored);
    else void startSession();
  }, [resumeSession, startSession]);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Keep the newest message in view.
  useEffect(() => {
    const log = logRef.current;
    if (log) log.scrollTop = log.scrollHeight;
  }, [state.lines.length, state.typing, state.proposalState]);

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

  async function send(text: string, retried = false): Promise<void> {
    const message = validMessage(text);
    if (!message) return;
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
        if (fresh) return send(message, true);
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
    dispatch({ type: "withdraw" });
    void send("Show me a different time");
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    const text = draft;
    setDraft("");
    void send(text);
  }

  const busy = state.typing || state.status === "starting";

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
        <div>
          <h2 id="chat-title" className="font-display text-lg leading-tight">
            {clinicName}
          </h2>
          <p className="text-xs text-ink-3">Virtual assistant · replies in seconds · no medical advice</p>
        </div>
        <div className="flex items-center gap-1">
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
        {state.error ? (
          <div role="alert" className="rounded-md border border-warn/30 bg-warn-tint/70 px-3 py-2 text-sm text-ink">
            <p>{state.error}</p>
            {state.pending ? (
              <button type="button" onClick={() => void send(state.pending!, true)} className="mt-1 inline-flex items-center gap-1 font-semibold text-accent hover:underline">
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
  const buttonRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const wasOpen = useRef(false);
  const small = useMediaQuery(SMALL_SCREEN_QUERY);
  const modal = open && small;

  useEffect(() => {
    const onOpen = () => {
      setMounted(true);
      setOpen(true);
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

  if (!props.enabled) return null;

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
        {mounted ? <ChatPanel {...props} onClose={() => setOpen(false)} /> : null}
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
        {open ? <X className="size-5" aria-hidden="true" /> : <MessageCircle className="size-5" aria-hidden="true" />}
        {open ? "Close chat" : "Chat with us"}
      </button>
    </>
  );
}
