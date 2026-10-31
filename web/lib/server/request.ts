import "server-only";

/**
 * Request helpers shared by the route handlers and middleware: the staff session cookie,
 * the CSRF same-origin rule and the client IP forwarded to the API.
 */

import { sessionCookieName } from "@/lib/session-cookie";

import { configuredSiteOrigin, trustForwardedFor } from "./config";

export { sessionCookieName };

export interface CookieOptions {
  httpOnly: true;
  secure: boolean;
  sameSite: "lax";
  path: "/";
  expires?: Date;
  maxAge?: number;
}

export function sessionCookieOptions(
  expires: Date | undefined,
  production = process.env.NODE_ENV === "production",
): CookieOptions {
  return { httpOnly: true, secure: production, sameSite: "lax", path: "/", expires };
}

const TOKEN = /^[A-Za-z0-9._~-]{8,512}$/;

/** The staff session token from the Cookie header, or null. Never logged. */
export function readSessionToken(headers: Headers): string | null {
  const name = sessionCookieName();
  const raw = headers.get("cookie");
  if (!raw) return null;
  for (const part of raw.split(";")) {
    const index = part.indexOf("=");
    if (index < 0) continue;
    if (part.slice(0, index).trim() !== name) continue;
    let value = part.slice(index + 1).trim();
    try {
      value = decodeURIComponent(value);
    } catch {
      return null;
    }
    return TOKEN.test(value) ? value : null;
  }
  return null;
}

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

/**
 * This site's own origin for the CSRF check. `SITE_URL` wins when it is set, so nothing the
 * client sends can change it. Without it (local development) the `Host` header is used;
 * `X-Forwarded-Host` is never consulted, and `X-Forwarded-Proto` only when forwarded headers
 * are trusted (Vercel).
 */
export function requestOrigin(request: Request, env: NodeJS.ProcessEnv = process.env): string | null {
  const configured = configuredSiteOrigin(env);
  if (configured) return configured;
  const url = new URL(request.url);
  const host = request.headers.get("host") || url.host;
  const forwardedProto = trustForwardedFor(env)
    ? request.headers.get("x-forwarded-proto")?.split(",")[0]?.trim()
    : undefined;
  const proto = forwardedProto || url.protocol.replace(/:$/, "");
  if (!host || (proto !== "http" && proto !== "https")) return null;
  return `${proto}://${host}`.toLowerCase();
}

/**
 * CSRF: a state-changing request must come from this origin. Browsers send `Origin` on every
 * cross-origin request and on same-origin POST/PUT/PATCH/DELETE; `Sec-Fetch-Site` is the
 * fallback. A request with neither (a script, not a browser) is refused: the BFF is for this
 * site's pages only.
 */
export function isSameOriginWrite(request: Request, allowedOrigins: readonly string[] = []): boolean {
  if (SAFE_METHODS.has(request.method.toUpperCase())) return true;
  const origin = request.headers.get("origin");
  if (origin) {
    if (origin === "null") return false;
    const normalised = origin.toLowerCase().replace(/\/+$/, "");
    const own = requestOrigin(request);
    if (own && normalised === own) return true;
    return allowedOrigins.some((allowed) => allowed.toLowerCase() === normalised);
  }
  return request.headers.get("sec-fetch-site") === "same-origin";
}

const IPV4 = /^(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)$/;
const IPV6 = /^[0-9a-f:.]{2,45}$/i;

function validIp(value: string | undefined | null): string | null {
  const ip = value?.trim();
  if (!ip) return null;
  if (IPV4.test(ip)) return ip;
  if (ip.includes(":") && IPV6.test(ip)) return ip.toLowerCase();
  return null;
}

/**
 * The client IP, or null when forwarded headers are not trusted (`TRUST_FORWARDED_FOR`,
 * default on Vercel only): then no `X-Client-IP` is sent at all. Vercel sets
 * `x-vercel-forwarded-for` itself, so it is preferred; then the first `x-forwarded-for` hop
 * (Vercel's edge overwrites that header with the connecting client first), then `x-real-ip`.
 * Anything that is not an IP literal is dropped rather than forwarded.
 */
export function clientIp(headers: Headers, trustForwarded: boolean): string | null {
  if (!trustForwarded) return null;
  return (
    validIp(headers.get("x-vercel-forwarded-for")?.split(",")[0]) ??
    validIp(headers.get("x-forwarded-for")?.split(",")[0]) ??
    validIp(headers.get("x-real-ip"))
  );
}
