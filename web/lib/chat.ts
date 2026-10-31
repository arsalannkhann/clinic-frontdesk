/**
 * Chat widget state as a pure reducer. The server keeps the history; the client sends only
 * its new message and renders what comes back, and after a reload it fetches the transcript
 * (GET /api/chat/sessions/{id}) to restore it. A proposal is only a suggestion: nothing is
 * booked until the patient presses Confirm on the card, which calls the confirm route with
 * the name, phone and consent typed into that card. The server stores the booking note as a
 * hidden message, so the booked card is kept in sessionStorage to survive a reload.
 */

import type { BookingResult, ChatProposal, ChatReply, ChatSessionCreated, ChatTranscript } from "./api/types";

export interface ChatLine {
  id: string;
  role: "user" | "assistant" | "system";
  text: string;
}

export type ProposalState = "open" | "confirming" | "booked" | "withdrawn" | "expired";

export interface ChatState {
  status: "idle" | "starting" | "ready" | "unavailable";
  sessionId: string | null;
  lines: ChatLine[];
  quickReplies: string[];
  typing: boolean;
  proposal: ChatProposal | null;
  proposalState: ProposalState | null;
  /** The line the proposal card follows, so it sits at its place in the transcript. */
  proposalAfter: string | null;
  proposalError: string | null;
  booking: BookingResult | null;
  error: string | null;
  /** The last user text, for a retry after a failure. */
  pending: string | null;
  /** A quiet status line (not an error), such as a turn the server is still answering. */
  notice: string | null;
}

export const initialChat: ChatState = {
  status: "idle",
  sessionId: null,
  lines: [],
  quickReplies: [],
  typing: false,
  proposal: null,
  proposalState: null,
  proposalAfter: null,
  proposalError: null,
  booking: null,
  error: null,
  pending: null,
  notice: null,
};

export type ChatAction =
  | { type: "starting" }
  | { type: "started"; session: ChatSessionCreated }
  | { type: "resumed"; sessionId: string }
  | { type: "restored"; sessionId: string; transcript: ChatTranscript; booked?: StoredBooking | null }
  | { type: "startFailed"; message: string }
  | { type: "send"; text: string }
  | { type: "retrying" }
  | { type: "reply"; reply: ChatReply }
  | { type: "replyFailed"; message: string; final?: boolean }
  /** 409 turn_in_progress: nothing was sent; `unsend` takes the new message back off screen. */
  | { type: "turnBusy"; unsend: boolean }
  | { type: "sessionLost" }
  | { type: "reset" }
  | { type: "confirming" }
  | { type: "confirmed"; result: BookingResult }
  | { type: "confirmFailed"; message: string; code: string }
  | { type: "withdraw" }
  | { type: "expire" };

export const MAX_MESSAGE = 1000;

/** Offered when the assistant cannot be reached: every option works without it. */
export const FALLBACK_REPLIES = ["Book online", "Request a call back", "Call the clinic"];

/** Offered once a conversation has reached the server's turn limit. */
export const NEW_CHAT = "Start a new chat";

/** What a refused confirm means, by code. */
export const CONFIRM_ERRORS: Record<string, string> = {
  proposal_expired: "This suggestion has expired. Ask for a new time.",
  proposal_used: "This suggestion has already been used. Ask for a new time if you need another booking.",
  slot_unavailable: "Someone has just taken this time. Ask for another time.",
  idempotency_conflict: "Your details changed after the first attempt. Check them and press Confirm booking again.",
  consent_required: "Tick the box to let the clinic contact you about this appointment. It's needed to book.",
};

/** Shown when the server is still answering an earlier message (409 turn_in_progress). */
export const TURN_IN_PROGRESS = "Still answering your last message…";

/** The reply to a message once the conversation is too long to continue. */
export const CONVERSATION_LIMIT =
  "This conversation has reached its limit. Start a new chat, book online, or call the clinic.";

let counter = 0;
const lineId = () => `l${Date.now().toString(36)}${(counter++).toString(36)}`;

const PROPOSAL_GONE = new Set(["proposal_expired", "proposal_used", "not_found", "slot_unavailable"]);

/** A booked card kept across a reload: the proposal, the result, and how many messages preceded it. */
export interface StoredBooking {
  sessionId: string;
  proposal: ChatProposal;
  booking: BookingResult;
  after: number;
}

const visible = (role: string) => role === "assistant" || role === "user";

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "starting":
      return { ...state, status: "starting", error: null };
    case "started":
      return {
        ...state,
        status: "ready",
        sessionId: action.session.session_id,
        lines: [...state.lines, { id: lineId(), role: "assistant", text: action.session.greeting }],
        quickReplies: action.session.quick_replies,
        error: null,
      };
    case "resumed":
      return {
        ...state,
        status: "ready",
        sessionId: action.sessionId,
        quickReplies: state.quickReplies.length ? state.quickReplies : ["Book a check-up", "Opening hours"],
      };
    case "restored": {
      const lines: ChatLine[] = action.transcript.messages
        .filter((m) => visible(m.role) && m.text.trim())
        .map((m) => ({ id: lineId(), role: m.role as "assistant" | "user", text: m.text }));
      const proposal = action.transcript.proposal ?? null;
      const booked = action.booked && action.booked.sessionId === action.sessionId ? action.booked : null;
      if (!proposal && booked) {
        // The card goes back where it was: after the messages that preceded the booking.
        const at = Math.min(Math.max(booked.after, 0), lines.length);
        const before = lines.slice(0, at);
        const note: ChatLine = { id: lineId(), role: "system", text: `Booking reference ${booked.booking.reference}` };
        return {
          ...initialChat,
          status: "ready",
          sessionId: action.sessionId,
          lines: [...before, note, ...lines.slice(at)],
          quickReplies: action.transcript.quick_replies ?? [],
          proposal: booked.proposal,
          proposalState: "booked",
          proposalAfter: before.at(-1)?.id ?? null,
          booking: booked.booking,
        };
      }
      return {
        ...initialChat,
        status: "ready",
        sessionId: action.sessionId,
        lines,
        quickReplies: action.transcript.quick_replies ?? [],
        proposal,
        proposalState: proposal ? "open" : null,
        proposalAfter: proposal ? (lines.at(-1)?.id ?? null) : null,
      };
    }
    case "startFailed":
      return { ...state, status: "unavailable", error: action.message, quickReplies: FALLBACK_REPLIES };
    case "send":
      return {
        ...state,
        lines: [...state.lines, { id: lineId(), role: "user", text: action.text }],
        typing: true,
        quickReplies: [],
        error: null,
        notice: null,
        pending: action.text,
      };
    case "retrying":
      return { ...state, typing: true, error: null, notice: null, quickReplies: [] };
    case "reply": {
      const lines = action.reply.messages
        .filter((m) => m.role === "assistant" && m.text.trim())
        .map((m) => ({ id: lineId(), role: "assistant" as const, text: m.text }));
      const proposal = action.reply.proposal ?? null;
      const all = [...state.lines, ...lines];
      const keepBooked = !proposal && state.proposalState === "booked";
      return {
        ...state,
        lines: all,
        typing: false,
        quickReplies: action.reply.quick_replies ?? [],
        // A new proposal replaces an unconfirmed one and follows the reply that offered it; a
        // booked card stays where it was, with later messages below it.
        proposal: proposal ?? (keepBooked ? state.proposal : null),
        proposalState: proposal ? "open" : keepBooked ? "booked" : null,
        proposalAfter: proposal ? (all.at(-1)?.id ?? null) : keepBooked ? state.proposalAfter : null,
        proposalError: null,
        booking: proposal ? null : state.booking,
        pending: null,
        notice: null,
      };
    }
    case "replyFailed":
      return {
        ...state,
        typing: false,
        error: action.message,
        quickReplies: action.final ? [NEW_CHAT, ...FALLBACK_REPLIES] : FALLBACK_REPLIES,
        pending: action.final ? null : state.pending,
      };
    case "turnBusy": {
      // The server refused the message unread, so it comes off the transcript (the widget puts
      // it back in the input) unless it was a retry of one already shown.
      const last = state.lines.findLastIndex((line) => line.role === "user");
      const lines = action.unsend && last >= 0 ? [...state.lines.slice(0, last), ...state.lines.slice(last + 1)] : state.lines;
      return {
        ...state,
        lines,
        typing: false,
        error: null,
        notice: TURN_IN_PROGRESS,
        pending: action.unsend ? null : state.pending,
      };
    }
    case "reset":
      return initialChat;
    case "sessionLost":
      return { ...state, sessionId: null, status: "idle", typing: false };
    case "confirming":
      return state.proposalState === "open" ? { ...state, proposalState: "confirming", proposalError: null } : state;
    case "confirmed":
      return {
        ...state,
        proposalState: "booked",
        booking: action.result,
        proposalError: null,
        // Suggestions offered alongside the proposal ("Show a later time") no longer apply.
        quickReplies: [],
        lines: [
          ...state.lines,
          { id: lineId(), role: "system", text: `Booking reference ${action.result.reference}` },
        ],
      };
    case "confirmFailed":
      return {
        ...state,
        proposalState: PROPOSAL_GONE.has(action.code) ? "expired" : "open",
        proposalError: action.message,
      };
    case "withdraw":
      return state.proposalState === "open" ? { ...state, proposalState: "withdrawn" } : state;
    case "expire":
      return state.proposalState === "open" ? { ...state, proposalState: "expired" } : state;
  }
}

export function validMessage(text: string): string | null {
  const value = text.trim();
  if (!value) return null;
  return value.length > MAX_MESSAGE ? value.slice(0, MAX_MESSAGE) : value;
}

/** sessionStorage access that never throws (private mode, blocked storage, SSR). */
export const SESSION_KEY = "aster.chat.session";
export function readStoredSession(): string | null {
  try {
    const value = window.sessionStorage.getItem(SESSION_KEY);
    return value && /^[A-Za-z0-9_-]{16,128}$/.test(value) ? value : null;
  } catch {
    return null;
  }
}
export function storeSession(id: string | null): void {
  try {
    if (id) window.sessionStorage.setItem(SESSION_KEY, id);
    else {
      window.sessionStorage.removeItem(SESSION_KEY);
      window.sessionStorage.removeItem(BOOKED_KEY);
    }
  } catch {
    // Storage unavailable: the conversation simply won't survive a reload.
  }
}

export const BOOKED_KEY = "aster.chat.booked";
export function readStoredBooking(sessionId: string): StoredBooking | null {
  try {
    const raw = window.sessionStorage.getItem(BOOKED_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<StoredBooking>;
    if (value.sessionId !== sessionId || !value.proposal?.id || !value.booking?.reference || typeof value.after !== "number") return null;
    return value as StoredBooking;
  } catch {
    return null;
  }
}
export function storeBooking(value: StoredBooking): void {
  try {
    window.sessionStorage.setItem(BOOKED_KEY, JSON.stringify(value));
  } catch {
    // Storage unavailable: after a reload the booked card is not shown again.
  }
}

/** How many user or assistant messages are on screen, for placing a stored card. */
export function visibleCount(lines: ChatLine[]): number {
  return lines.filter((line) => visible(line.role)).length;
}
