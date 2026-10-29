/**
 * The staff session, held in memory only (this module's variables). It is never written to
 * localStorage, sessionStorage, cookies or the URL, so a reload or a new tab signs out.
 */
import type { Schemas } from "./api/client";

export type StaffUser = Schemas["StaffUser"];
export type Role = StaffUser["role"];

interface Session {
  token: string;
  expiresAt: number;
  user: StaffUser;
}

let current: Session | null = null;
let expiryTimer: ReturnType<typeof setTimeout> | undefined;
const listeners = new Set<(user: StaffUser | null) => void>();
const MAX_TIMEOUT = 2_147_483_647;

function notify(): void {
  const user = current?.user ?? null;
  for (const listener of listeners) listener(user);
}

export function startSession(response: Schemas["SessionResponse"]): void {
  const expiresAt = Date.parse(response.expires_at);
  current = {
    token: response.token,
    expiresAt: Number.isFinite(expiresAt) ? expiresAt : 0,
    user: response.user,
  };
  clearTimeout(expiryTimer);
  const remaining = current.expiresAt - Date.now();
  expiryTimer = setTimeout(endSession, Math.max(0, Math.min(remaining, MAX_TIMEOUT)));
  notify();
}

/** The bearer token, or null when signed out or expired (which also ends the session). */
export function sessionToken(): string | null {
  if (current === null) return null;
  if (Date.now() >= current.expiresAt) {
    endSession();
    return null;
  }
  return current.token;
}

export function sessionUser(): StaffUser | null {
  return current?.user ?? null;
}

export function endSession(): void {
  clearTimeout(expiryTimer);
  expiryTimer = undefined;
  const had = current !== null;
  current = null;
  if (had) notify();
}

export function onSessionChange(listener: (user: StaffUser | null) => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export const canWrite = (user: StaffUser | null): boolean =>
  user?.role === "owner" || user?.role === "admin";
export const isOwner = (user: StaffUser | null): boolean => user?.role === "owner";
