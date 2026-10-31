/**
 * Chat widget state as a pure reducer. The server keeps the history; the client sends only
 * its new message and renders what comes back. A proposal is only a suggestion: nothing is
 * booked until the patient presses Confirm on the card, which calls the confirm route with
 * the name, phone and consent typed into that card.
 */

import type { BookingResult, ChatProposal, ChatReply, ChatSessionCreated } from "./api/types";

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
  proposalError: string | null;
  booking: BookingResult | null;
  error: string | null;
  /** The last user text, for a retry after a failure. */
  pending: string | null;
}

export const initialChat: ChatState = {
  status: "idle",
  sessionId: null,
  lines: [],
  quickReplies: [],
  typing: false,
  proposal: null,
  proposalState: null,
  proposalError: null,
  booking: null,
  error: null,
  pending: null,
};

export type ChatAction =
  | { type: "starting" }
  | { type: "started"; session: ChatSessionCreated }
  | { type: "resumed"; sessionId: string }
  | { type: "startFailed"; message: string }
  | { type: "send"; text: string }
  | { type: "retrying" }
  | { type: "reply"; reply: ChatReply }
  | { type: "replyFailed"; message: string }
  | { type: "sessionLost" }
  | { type: "confirming" }
  | { type: "confirmed"; result: BookingResult }
  | { type: "confirmFailed"; message: string; code: string }
  | { type: "withdraw" }
  | { type: "expire" };

export const MAX_MESSAGE = 1000;

/** Offered when the assistant cannot be reached: every option works without it. */
export const FALLBACK_REPLIES = ["Book online", "Request a call back", "Call the clinic"];

let counter = 0;
const lineId = () => `l${Date.now().toString(36)}${(counter++).toString(36)}`;

const PROPOSAL_GONE = new Set(["proposal_expired", "proposal_used", "not_found", "slot_unavailable"]);

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
    case "startFailed":
      return { ...state, status: "unavailable", error: action.message, quickReplies: FALLBACK_REPLIES };
    case "send":
      return {
        ...state,
        lines: [...state.lines, { id: lineId(), role: "user", text: action.text }],
        typing: true,
        quickReplies: [],
        error: null,
        pending: action.text,
      };
    case "retrying":
      return { ...state, typing: true, error: null, quickReplies: [] };
    case "reply": {
      const lines = action.reply.messages
        .filter((m) => m.role === "assistant" && m.text.trim())
        .map((m) => ({ id: lineId(), role: "assistant" as const, text: m.text }));
      const proposal = action.reply.proposal ?? null;
      return {
        ...state,
        lines: [...state.lines, ...lines],
        typing: false,
        quickReplies: action.reply.quick_replies ?? [],
        // A new proposal replaces an unconfirmed one; a booked card stays as it is.
        proposal: proposal ?? (state.proposalState === "booked" ? state.proposal : null),
        proposalState: proposal ? "open" : state.proposalState === "booked" ? "booked" : null,
        proposalError: null,
        booking: proposal ? null : state.booking,
        pending: null,
      };
    }
    case "replyFailed":
      return { ...state, typing: false, error: action.message, quickReplies: FALLBACK_REPLIES };
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
    else window.sessionStorage.removeItem(SESSION_KEY);
  } catch {
    // Storage unavailable: the conversation simply won't survive a reload.
  }
}
