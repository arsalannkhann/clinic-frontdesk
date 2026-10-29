/**
 * API errors mapped to user-safe messages.
 *
 * The response body is never shown as-is. Only a short plain-text `detail` string is used
 * (the backend's details are fixed operational phrases such as "A provisioning step is
 * already pending"), and only for statuses where it explains what to do. 422 validation
 * arrays contribute field names only, never submitted values. Callers render the message
 * with textContent.
 */

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export const NETWORK_MESSAGE = "Cannot reach the server. Check your connection and try again.";
export const SESSION_ENDED_MESSAGE = "Your session has ended. Sign in again.";
export const LOGIN_FAILED_MESSAGE = "Invalid email or password.";
export const UNREADABLE_MESSAGE = "The server sent an unexpected response.";

const STATUS_MESSAGES: Readonly<Record<number, string>> = {
  400: "The request was rejected.",
  403: "Your role does not allow this action.",
  404: "Not found. It may have been removed, or it belongs to another clinic.",
  409: "This action conflicts with the current state. Refresh and try again.",
  413: "The request is too large.",
  422: "Some values are invalid.",
  429: "Too many requests. Wait a moment and try again.",
  502: "A connected provider is unavailable. Try again later.",
  503: "The service is temporarily unavailable. Try again later.",
};

// Statuses whose backend detail is a fixed, actionable phrase worth showing.
const DETAIL_STATUSES = new Set([400, 409, 422]);
const MAX_DETAIL = 200;
const FIELD_NAME = /^[a-z][a-z0-9_]{0,63}$/;

function safeDetail(detail: unknown): string | null {
  if (typeof detail !== "string") return null;
  // eslint-disable-next-line no-control-regex
  const text = detail.replace(/[\u0000-\u001f\u007f]+/g, " ").trim();
  if (!text || text.length > MAX_DETAIL) return null;
  return text;
}

function invalidFields(detail: unknown): string[] {
  if (!Array.isArray(detail)) return [];
  const names = new Set<string>();
  for (const item of detail) {
    if (typeof item !== "object" || item === null) continue;
    const loc: unknown = (item as { loc?: unknown }).loc;
    if (!Array.isArray(loc)) continue;
    const last: unknown = loc[loc.length - 1];
    if (typeof last === "string" && FIELD_NAME.test(last) && last !== "body") names.add(last);
  }
  return [...names].slice(0, 10);
}

/** The user-facing message for a failed response. `hadToken` distinguishes a failed login
 * from an ended session. */
export function messageFor(status: number, body: unknown, hadToken: boolean): string {
  if (status === 401) return hadToken ? SESSION_ENDED_MESSAGE : LOGIN_FAILED_MESSAGE;
  const detail =
    typeof body === "object" && body !== null ? (body as { detail?: unknown }).detail : undefined;
  if (DETAIL_STATUSES.has(status)) {
    const text = safeDetail(detail);
    if (text) return text;
    const fields = invalidFields(detail);
    if (fields.length) return `Some values are invalid: ${fields.join(", ")}.`;
  }
  const known = STATUS_MESSAGES[status];
  if (known) return known;
  if (status >= 500) return "The server had a problem. Try again later.";
  return "The request failed.";
}

/** Build an ApiError from a failed response, reading at most a JSON `detail`. */
export async function toApiError(response: Response, hadToken: boolean): Promise<ApiError> {
  let body: unknown = null;
  const type = response.headers.get("content-type") ?? "";
  if (type.includes("application/json")) {
    try {
      body = await response.json();
    } catch {
      body = null;
    }
  }
  return new ApiError(response.status, messageFor(response.status, body, hadToken));
}

/** A user-safe message for anything thrown by an action. */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return "Something went wrong. Try again.";
}
