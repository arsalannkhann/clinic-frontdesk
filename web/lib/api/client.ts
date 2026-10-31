/**
 * Browser client for the BFF (`/api/*`). Same-origin only: the staff session travels in the
 * httpOnly cookie, which this code can neither read nor set. Errors are always the envelope
 * `{"error": {"code", "message"}}`; the message is safe to show as text.
 */

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export const NETWORK_MESSAGE = "You seem to be offline. Check your connection and try again.";

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  body?: unknown;
  query?: Record<string, string | number | boolean | null | undefined>;
  idempotencyKey?: string;
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
}

export function withQuery(path: string, query?: RequestOptions["query"]): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  }
  const text = params.toString();
  return text ? `${path}${path.includes("?") ? "&" : "?"}${text}` : path;
}

async function envelopeFrom(response: Response): Promise<ApiError> {
  let code = "request_failed";
  let message = "Something went wrong. Please try again.";
  try {
    const body = (await response.json()) as { error?: { code?: unknown; message?: unknown } };
    if (typeof body?.error?.code === "string") code = body.error.code;
    if (typeof body?.error?.message === "string" && body.error.message.trim()) message = body.error.message;
  } catch {
    // Not JSON (a platform error page); keep the generic message.
  }
  if (response.status === 503 && code === "request_failed") {
    code = "unavailable";
    message = "The service is temporarily unavailable. Try again shortly.";
  }
  return new ApiError(response.status, code, message);
}

/** Listeners for an ended staff session (any 401 on a staff/admin route). */
const unauthorizedListeners = new Set<() => void>();
export function onUnauthorized(listener: () => void): () => void {
  unauthorizedListeners.add(listener);
  return () => unauthorizedListeners.delete(listener);
}

export async function api<T>(path: string, options: RequestOptions = {}): Promise<T> {
  if (!path.startsWith("/api/")) throw new Error("api() only calls same-origin /api/* routes");
  const headers: Record<string, string> = { Accept: "application/json" };
  const init: RequestInit = {
    method: options.method ?? (options.body === undefined ? "GET" : "POST"),
    headers,
    credentials: "same-origin",
    cache: "no-store",
    signal: options.signal,
  };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
  const fetchImpl = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
  let response: Response;
  try {
    response = await fetchImpl(withQuery(path, options.query), init);
  } catch (error) {
    if ((error as { name?: string })?.name === "AbortError") throw error;
    throw new ApiError(0, "network", NETWORK_MESSAGE);
  }
  if (!response.ok) {
    const error = await envelopeFrom(response);
    if (response.status === 401 && (path.startsWith("/api/staff/") || path.startsWith("/api/admin/"))) {
      if (!path.startsWith("/api/staff/login")) for (const listener of unauthorizedListeners) listener();
    }
    throw error;
  }
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  if (!text) return undefined as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new ApiError(response.status, "unreadable", "The server sent an unexpected response.");
  }
}

/** SWR fetcher. */
export const fetcher = <T,>(path: string) => api<T>(path);

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error && error.name === "TimeError") return error.message;
  return "Something went wrong. Please try again.";
}

/** A random key so a retried submission is recognised as the same action. */
export function newIdempotencyKey(prefix: string): string {
  const bytes = new Uint8Array(12);
  crypto.getRandomValues(bytes);
  return `${prefix}-${[...bytes].map((b) => b.toString(16).padStart(2, "0")).join("")}`;
}
