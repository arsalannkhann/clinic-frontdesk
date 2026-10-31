import "server-only";

import { ConfigError, readConfig, type ServerConfig } from "./config";
import { errorJson, normaliseError } from "./errors";
import { clientIp, isSameOriginWrite, readSessionToken } from "./request";

/**
 * The BFF core: one place that talks to the FastAPI origin.
 *
 * - Only an allowlisted set of headers goes upstream: Accept, Content-Type, the client IP as
 *   `X-Client-IP` (and `X-Forwarded-For`), `X-Proxy-Secret` from the server-only
 *   PUBLIC_PROXY_SECRET, an `Idempotency-Key` when valid, and for staff routes the session
 *   token from the httpOnly cookie as `Authorization: Bearer`. Cookies are never forwarded.
 * - Every fetch has a timeout. Network failures and timeouts become a friendly 503.
 * - Error bodies are normalised to `{"error": {"code", "message"}}`.
 * - Nothing here logs request bodies, tokens or upstream bodies.
 */

export interface UpstreamRequest {
  method: string;
  /** API path starting with /v1/. */
  path: string;
  /** Query string including the leading "?", or "". */
  search: string;
  /** Raw JSON text, or null. */
  body: string | null;
  token: string | null;
  ip: string | null;
  idempotencyKey: string | null;
}

export interface UpstreamResult {
  status: number;
  body: unknown;
  retryAfter: string | null;
}

export class UpstreamUnavailable extends Error {
  constructor(readonly kind: "network" | "timeout") {
    super(kind === "timeout" ? "Upstream timed out" : "Upstream unreachable");
    this.name = "UpstreamUnavailable";
  }
}

export const UNAVAILABLE_MESSAGE =
  "We can't reach the clinic system right now. Please try again in a moment.";

const MAX_BODY_BYTES = 32 * 1024;
const IDEMPOTENCY = /^[A-Za-z0-9._:-]{8,128}$/;

export interface UpstreamDeps {
  config?: ServerConfig;
  fetchImpl?: typeof fetch;
}

async function mockResult(request: UpstreamRequest): Promise<UpstreamResult> {
  // The literal NODE_ENV test lets the production bundler drop this branch and the fixture
  // module with it; readConfig() also refuses mock mode in production.
  if (process.env.NODE_ENV !== "production") {
    const { mockUpstream } = await import("./mock/router");
    return mockUpstream(request);
  }
  throw new ConfigError("Mock mode is not available in production.");
}

export async function callUpstream(
  request: UpstreamRequest,
  timeoutMs: number,
  deps: UpstreamDeps = {},
): Promise<UpstreamResult> {
  const config = deps.config ?? readConfig();
  if (config.mock) return mockResult(request);

  const headers = new Headers({ Accept: "application/json" });
  if (request.body !== null) headers.set("Content-Type", "application/json");
  if (request.ip) {
    // Vercel rewrites X-Forwarded-For between functions, so the API reads the browser IP from
    // X-Client-IP and trusts it only alongside the shared proxy secret.
    headers.set("X-Client-IP", request.ip);
    headers.set("X-Forwarded-For", request.ip);
  }
  if (config.proxySecret) headers.set("X-Proxy-Secret", config.proxySecret);
  if (request.token) headers.set("Authorization", `Bearer ${request.token}`);
  if (request.idempotencyKey) headers.set("Idempotency-Key", request.idempotencyKey);

  const fetchImpl = deps.fetchImpl ?? fetch;
  let response: Response;
  try {
    response = await fetchImpl(`${config.apiBaseUrl}${request.path}${request.search}`, {
      method: request.method,
      headers,
      body: request.body,
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (error) {
    const name = (error as { name?: string } | null)?.name;
    throw new UpstreamUnavailable(name === "TimeoutError" || name === "AbortError" ? "timeout" : "network");
  }

  let body: unknown = null;
  if (response.status !== 204) {
    const type = response.headers.get("content-type") ?? "";
    if (type.includes("json")) {
      try {
        body = await response.json();
      } catch {
        body = null;
      }
    } else {
      // Drain without reading into memory we would show.
      await response.body?.cancel().catch(() => undefined);
    }
  }
  return { status: response.status, body, retryAfter: response.headers.get("retry-after") };
}

/** Upstream result -> browser response. Errors become the envelope. */
export function toResponse(result: UpstreamResult): Response {
  const headers = new Headers({ "Cache-Control": "no-store" });
  if (result.retryAfter && /^\d{1,6}$/.test(result.retryAfter)) {
    headers.set("Retry-After", result.retryAfter);
  }
  if (result.status === 204 || result.status === 205 || result.status === 304) {
    return new Response(null, { status: result.status === 304 ? 204 : result.status, headers });
  }
  if (result.status >= 300 && result.status < 400) {
    // The API never redirects the BFF; treat one as a fault rather than following it.
    return errorJson(502, "bad_gateway", UNAVAILABLE_MESSAGE);
  }
  if (result.status >= 400) {
    return Response.json(normaliseError(result.status, result.body), {
      status: result.status,
      headers,
    });
  }
  return Response.json(result.body ?? null, { status: result.status, headers });
}

export function unavailableResponse(error: UpstreamUnavailable): Response {
  return errorJson(503, error.kind === "timeout" ? "upstream_timeout" : "upstream_unavailable", UNAVAILABLE_MESSAGE, {
    "Retry-After": "5",
  });
}

let configErrorLogged = false;
export function configErrorResponse(error: ConfigError): Response {
  if (!configErrorLogged) {
    configErrorLogged = true;
    console.error(`[web] Configuration error: ${error.message}`);
  }
  return errorJson(500, "misconfigured", "The site is not configured correctly. Please try again later.");
}

export interface ForwardTarget {
  /** API path starting with /v1/ (already validated by the caller). */
  path: string;
  /** Allowlisted query string including "?", or "". */
  search?: string;
  /** Attach the staff session from the cookie. */
  staff?: boolean;
  /** Use the longer chat timeout. */
  chat?: boolean;
}

/**
 * Read at most `limit` bytes of a body as UTF-8, stopping (and cancelling the stream) as soon
 * as the limit is passed, so an oversized or endless body is never buffered whole. Returns
 * null when the body is too large.
 */
export async function readLimitedText(body: ReadableStream<Uint8Array> | null, limit: number): Promise<string | null> {
  if (!body) return "";
  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > limit) {
        await reader.cancel().catch(() => undefined);
        return null;
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(bytes);
}

/** Read a JSON body with a size cap enforced while streaming. Returns a Response on rejection. */
export async function readJsonBody(request: Request): Promise<string | null | Response> {
  const method = request.method.toUpperCase();
  if (method === "GET" || method === "HEAD" || method === "DELETE") return null;
  const declared = Number(request.headers.get("content-length") ?? "0");
  if (declared > MAX_BODY_BYTES) return errorJson(413, "too_large", "The request is too large.");
  const text = await readLimitedText(request.body, MAX_BODY_BYTES);
  if (text === null) return errorJson(413, "too_large", "The request is too large.");
  if (!text.trim()) return null;
  const type = request.headers.get("content-type") ?? "";
  if (!type.toLowerCase().startsWith("application/json")) {
    return errorJson(415, "unsupported_media_type", "Send JSON.");
  }
  try {
    JSON.parse(text);
  } catch {
    return errorJson(400, "invalid_json", "The request body is not valid JSON.");
  }
  return text;
}

export function allowedOrigins(): string[] {
  const site = process.env.SITE_URL;
  if (!site) return [];
  try {
    return [new URL(site).origin];
  } catch {
    return [];
  }
}

/** Proxy one browser request to the API. */
export async function forward(request: Request, target: ForwardTarget, deps: UpstreamDeps = {}): Promise<Response> {
  if (!isSameOriginWrite(request, allowedOrigins())) {
    return errorJson(403, "forbidden_origin", "This request must come from the clinic website.");
  }
  let config: ServerConfig;
  try {
    config = deps.config ?? readConfig();
  } catch (error) {
    if (error instanceof ConfigError) return configErrorResponse(error);
    throw error;
  }
  const body = await readJsonBody(request);
  if (body instanceof Response) return body;

  let token: string | null = null;
  if (target.staff) {
    token = readSessionToken(request.headers);
    if (!token) return errorJson(401, "unauthorized", "Sign in to continue.");
  }
  const key = request.headers.get("idempotency-key");
  try {
    const result = await callUpstream(
      {
        method: request.method.toUpperCase(),
        path: target.path,
        search: target.search ?? "",
        body,
        token,
        ip: clientIp(request.headers, config.trustForwardedFor),
        idempotencyKey: key && IDEMPOTENCY.test(key) && !key.startsWith("bolna:") ? key : null,
      },
      target.chat ? config.chatTimeoutMs : config.timeoutMs,
      { ...deps, config },
    );
    return toResponse(result);
  } catch (error) {
    if (error instanceof UpstreamUnavailable) return unavailableResponse(error);
    if (error instanceof ConfigError) return configErrorResponse(error);
    console.error("[web] Unexpected proxy failure");
    return errorJson(502, "bad_gateway", UNAVAILABLE_MESSAGE);
  }
}

const SEGMENT = /^[A-Za-z0-9_-]{1,80}$/;

/** Validate catch-all segments: no dots, slashes or encoded separators can reach the API. */
export function safeSegments(segments: readonly string[] | undefined): string[] | null {
  if (!segments || segments.length === 0 || segments.length > 6) return null;
  return segments.every((segment) => SEGMENT.test(segment)) ? [...segments] : null;
}

/** Copy only allowlisted query parameters, each a short plain value. */
export function pickQuery(url: URL, allowed: readonly string[]): string {
  const out = new URLSearchParams();
  for (const name of allowed) {
    const value = url.searchParams.get(name);
    if (value !== null && value.length <= 64 && /^[A-Za-z0-9_:.+-]*$/.test(value)) {
      out.set(name, value);
    }
  }
  const text = out.toString();
  return text ? `?${text}` : "";
}
