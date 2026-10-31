import "server-only";

import type { ErrorEnvelope } from "@/lib/api/types";

/**
 * Error envelopes. New platform routes already answer `{"error": {"code", "message"}}`; the
 * existing staff/admin routes answer FastAPI's `{"detail": ...}`. The BFF passes the first
 * through and normalises the second, so the browser only ever sees the envelope. Messages are
 * cleaned of control characters and capped; 422 arrays contribute field names only, never
 * submitted values.
 */

const MAX_MESSAGE = 300;
const CODE = /^[a-z][a-z0-9_]{0,63}$/;
const FIELD = /^[a-z][a-z0-9_]{0,63}$/;

const STATUS_CODES: Record<number, [string, string]> = {
  400: ["bad_request", "The request was rejected."],
  401: ["unauthorized", "Your session has ended. Sign in again."],
  403: ["forbidden", "You do not have permission to do this."],
  404: ["not_found", "We could not find that."],
  409: ["conflict", "This conflicts with a recent change. Refresh and try again."],
  413: ["too_large", "The request is too large."],
  415: ["unsupported_media_type", "Send JSON."],
  422: ["validation_error", "Some values are invalid."],
  429: ["rate_limited", "Too many requests. Wait a moment and try again."],
  502: ["bad_gateway", "A connected service is unavailable. Try again later."],
  503: ["unavailable", "The service is temporarily unavailable. Try again shortly."],
  504: ["upstream_timeout", "The clinic system took too long to answer. Try again shortly."],
};

export function envelope(code: string, message: string): ErrorEnvelope {
  return { error: { code, message } };
}

export function errorJson(status: number, code: string, message: string, headers?: HeadersInit) {
  const response = Response.json(envelope(code, message), { status, headers });
  response.headers.set("Cache-Control", "no-store");
  return response;
}

function clean(text: unknown): string | null {
  if (typeof text !== "string") return null;
  const value = text.replace(/[\u0000-\u001f\u007f]+/g, " ").trim();
  if (!value) return null;
  return value.length > MAX_MESSAGE ? `${value.slice(0, MAX_MESSAGE - 1)}…` : value;
}

function fieldNames(detail: unknown): string[] {
  if (!Array.isArray(detail)) return [];
  const names = new Set<string>();
  for (const item of detail) {
    const loc = (item as { loc?: unknown } | null)?.loc;
    if (!Array.isArray(loc)) continue;
    const last: unknown = loc[loc.length - 1];
    if (typeof last === "string" && FIELD.test(last) && last !== "body") names.add(last);
  }
  return [...names].slice(0, 8);
}

export function defaultFor(status: number): [string, string] {
  return (
    STATUS_CODES[status] ??
    (status >= 500
      ? ["server_error", "Something went wrong on our side. Try again shortly."]
      : ["request_failed", "The request failed."])
  );
}

/** Any upstream error body -> the envelope. */
export function normaliseError(status: number, body: unknown): ErrorEnvelope {
  const [fallbackCode, fallbackMessage] = defaultFor(status);
  if (body && typeof body === "object") {
    const error = (body as { error?: unknown }).error;
    if (error && typeof error === "object") {
      const code = (error as { code?: unknown }).code;
      // A 5xx message can describe internals: keep the code, never the text.
      const message = status >= 500 ? null : clean((error as { message?: unknown }).message);
      return envelope(
        typeof code === "string" && CODE.test(code) ? code : fallbackCode,
        message ?? fallbackMessage,
      );
    }
    const detail = (body as { detail?: unknown }).detail;
    // 401 and 5xx details are never shown: they can describe internals.
    if (status !== 401 && status < 500) {
      const message = clean(detail);
      if (message) return envelope(fallbackCode, message);
      const fields = fieldNames(detail);
      if (fields.length) {
        return envelope("validation_error", `Some values are invalid: ${fields.join(", ")}.`);
      }
    }
  }
  return envelope(fallbackCode, fallbackMessage);
}
