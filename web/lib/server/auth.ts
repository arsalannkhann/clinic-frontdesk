import "server-only";

import { NextResponse } from "next/server";

import type { StaffUser } from "@/lib/api/types";
import { ConfigError, readConfig, type ServerConfig } from "./config";
import { errorJson, normaliseError } from "./errors";
import { clientIp, isSameOriginWrite, readSessionToken, sessionCookieName, sessionCookieOptions } from "./request";
import {
  allowedOrigins,
  callUpstream,
  configErrorResponse,
  readJsonBody,
  unavailableResponse,
  UpstreamUnavailable,
  type UpstreamDeps,
} from "./upstream";

/**
 * Staff sign-in and sign-out. The API's session token is written to an httpOnly cookie and
 * never returned to JavaScript; the browser only learns the user and the expiry.
 */

const TOKEN = /^[A-Za-z0-9._~-]{8,512}$/;
const MAX_AGE_SECONDS = 12 * 60 * 60; // The API's absolute session lifetime.

function config(deps: UpstreamDeps): ServerConfig | Response {
  try {
    return deps.config ?? readConfig();
  } catch (error) {
    if (error instanceof ConfigError) return configErrorResponse(error);
    throw error;
  }
}

function parseCredentials(text: string | null): { email: string; password: string } | null {
  if (!text) return null;
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return null;
  }
  if (!value || typeof value !== "object") return null;
  const { email, password } = value as { email?: unknown; password?: unknown };
  if (typeof email !== "string" || typeof password !== "string") return null;
  if (!email.trim() || email.length > 320 || !password || password.length > 256) return null;
  return { email: email.trim(), password };
}

export async function login(request: Request, deps: UpstreamDeps = {}): Promise<Response> {
  if (!isSameOriginWrite(request, allowedOrigins())) {
    return errorJson(403, "forbidden_origin", "This request must come from the clinic website.");
  }
  const cfg = config(deps);
  if (cfg instanceof Response) return cfg;
  const raw = await readJsonBody(request);
  if (raw instanceof Response) return raw;
  const credentials = parseCredentials(raw);
  if (!credentials) return errorJson(422, "validation_error", "Enter your email and password.");

  let result;
  try {
    result = await callUpstream(
      {
        method: "POST",
        path: "/v1/staff/login",
        search: "",
        body: JSON.stringify(credentials),
        token: null,
        ip: clientIp(request.headers, cfg.trustForwardedFor),
        idempotencyKey: null,
      },
      cfg.timeoutMs,
      { ...deps, config: cfg },
    );
  } catch (error) {
    if (error instanceof UpstreamUnavailable) return unavailableResponse(error);
    throw error;
  }

  if (result.status === 401) {
    return errorJson(401, "invalid_credentials", "That email and password do not match an active account.");
  }
  if (result.status !== 200) {
    return Response.json(normaliseError(result.status, result.body), {
      status: result.status,
      headers: { "Cache-Control": "no-store" },
    });
  }
  const body = result.body as { token?: unknown; expires_at?: unknown; user?: unknown } | null;
  const token = body?.token;
  const user = body?.user as StaffUser | undefined;
  if (typeof token !== "string" || !TOKEN.test(token) || !user || typeof user.role !== "string") {
    return errorJson(502, "bad_gateway", "Sign-in failed. Try again shortly.");
  }
  const expiresAt = typeof body?.expires_at === "string" ? new Date(body.expires_at) : null;
  const expires =
    expiresAt && Number.isFinite(expiresAt.getTime())
      ? expiresAt
      : new Date(Date.now() + MAX_AGE_SECONDS * 1000);

  const response = NextResponse.json(
    { user: { id: user.id, email: user.email, role: user.role }, expires_at: expires.toISOString() },
    { status: 200, headers: { "Cache-Control": "no-store" } },
  );
  response.cookies.set(sessionCookieName(), token, sessionCookieOptions(expires));
  return response;
}

export async function logout(request: Request, deps: UpstreamDeps = {}): Promise<Response> {
  if (!isSameOriginWrite(request, allowedOrigins())) {
    return errorJson(403, "forbidden_origin", "This request must come from the clinic website.");
  }
  const token = readSessionToken(request.headers);
  const cfg = config(deps);
  if (token && !(cfg instanceof Response)) {
    try {
      // Revoke server-side; the cookie is cleared whatever the API answers.
      await callUpstream(
        { method: "POST", path: "/v1/staff/logout", search: "", body: null, token, ip: clientIp(request.headers, cfg.trustForwardedFor), idempotencyKey: null },
        cfg.timeoutMs,
        { ...deps, config: cfg },
      );
    } catch {
      // The API may be unreachable; the session expires on its own (12 h).
    }
  }
  const response = new NextResponse(null, { status: 204, headers: { "Cache-Control": "no-store" } });
  response.cookies.set(sessionCookieName(), "", { ...sessionCookieOptions(new Date(0)), maxAge: 0 });
  return response;
}
