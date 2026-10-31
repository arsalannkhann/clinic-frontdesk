import "server-only";

/**
 * Request helpers shared by the route handlers and middleware: the staff session cookie,
 * the CSRF same-origin rule and the client IP forwarded to the API.
 */

import { sessionCookieName } from "@/lib/session-cookie";

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

/** The origin this request was addressed to, as the browser saw it. */
export function requestOrigin(request: Request): string | null {
  const url = new URL(request.url);
  const host = request.headers.get("x-forwarded-host")?.split(",")[0]?.trim() || request.headers.get("host") || url.host;
  const proto =
    request.headers.get("x-forwarded-proto")?.split(",")[0]?.trim() || url.protocol.replace(/:$/, "");
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
 * The client IP. Vercel's edge overwrites `x-forwarded-for` with the connecting client as
 * the first entry (and sets `x-real-ip`), so the first hop is the client. Anything that is
 * not an IP literal is dropped rather than forwarded.
 */
export function clientIp(headers: Headers): string | null {
  const first = headers.get("x-forwarded-for")?.split(",")[0];
  return validIp(first) ?? validIp(headers.get("x-real-ip"));
}
