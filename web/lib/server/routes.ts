import "server-only";

import { NextResponse } from "next/server";

import { ConfigError, readConfig } from "./config";
import { configErrorResponse, forward, pickQuery, safeSegments } from "./upstream";
import { errorJson } from "./errors";
import { sessionCookieName, sessionCookieOptions } from "./request";

/**
 * Explicit allowlists for the catch-all route handlers. Each table lists exactly the method
 * and path pattern the site's pages use, with the query parameters that route may carry.
 * Anything else (another path, another method, an id that is not a UUID) is a 404 answered
 * here, without an upstream call. Login and logout have their own handlers and are never
 * proxied with the session cookie.
 */

type Handler = (request: Request, context: { params: Promise<{ path?: string[] }> }) => Promise<Response>;

const notFound = () => errorJson(404, "not_found", "We could not find that.");

interface PublicRoute {
  method: string;
  path: string;
  query?: readonly string[];
}

const PUBLIC_ROUTES: readonly PublicRoute[] = [
  { method: "GET", path: "" },
  { method: "GET", path: "services" },
  { method: "GET", path: "doctors" },
  { method: "GET", path: "slots", query: ["service_id", "doctor_id", "date", "days"] },
  { method: "POST", path: "bookings" },
  { method: "POST", path: "bookings/lookup" },
  { method: "POST", path: "callbacks" },
];

function slug(): string | Response {
  try {
    return readConfig().slug;
  } catch (error) {
    if (error instanceof ConfigError) return configErrorResponse(error);
    throw error;
  }
}

/** /api/public/* -> /v1/public/clinics/{slug}/* */
export const publicHandler: Handler = async (request, context) => {
  const { path } = await context.params;
  const segments = path && path.length > 0 ? safeSegments(path) : [];
  if (segments === null) return notFound();
  const joined = segments.join("/");
  const method = request.method.toUpperCase();
  const route = PUBLIC_ROUTES.find((r) => r.path === joined && r.method === method);
  if (!route) {
    const exists = PUBLIC_ROUTES.some((r) => r.path === joined);
    return exists ? errorJson(405, "method_not_allowed", "Method not allowed.") : notFound();
  }
  const clinic = slug();
  if (clinic instanceof Response) return clinic;
  return forward(request, {
    path: `/v1/public/clinics/${clinic}${joined ? `/${joined}` : ""}`,
    search: pickQuery(new URL(request.url), route.query ?? []),
  });
};

/**
 * /api/chat/* -> /v1/chat/*. Session creation is scoped to the configured clinic; the other
 * routes carry the session id, a 128-bit bearer secret for that conversation only.
 */
export const chatHandler: Handler = async (request, context) => {
  const { path } = await context.params;
  const segments = safeSegments(path);
  if (!segments) return notFound();
  const method = request.method.toUpperCase();
  const [first, id, kind, pid, action] = segments;
  if (first !== "sessions") return notFound();
  let target: { path: string; chat?: boolean } | null = null;
  if (method === "POST" && segments.length === 1) {
    const clinic = slug();
    if (clinic instanceof Response) return clinic;
    target = { path: `/v1/chat/clinics/${clinic}/sessions` };
  } else if (method === "GET" && segments.length === 2) {
    target = { path: `/v1/chat/sessions/${id}` };
  } else if (method === "POST" && segments.length === 3 && kind === "messages") {
    target = { path: `/v1/chat/sessions/${id}/messages`, chat: true };
  } else if (method === "POST" && segments.length === 5 && kind === "proposals" && action === "confirm") {
    target = { path: `/v1/chat/sessions/${id}/proposals/${pid}/confirm`, chat: true };
  }
  return target ? forward(request, target) : notFound();
};

/* ---------- staff and admin ---------- */

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

interface StaffRoute {
  method: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  /** Slash-separated pattern; `{id}` matches one UUID segment. */
  pattern: string;
  query?: readonly string[];
}

export const STAFF_ROUTES: readonly StaffRoute[] = [
  { method: "GET", pattern: "me" },
  { method: "POST", pattern: "me/password" },
  { method: "GET", pattern: "calendar", query: ["from", "to"] },
  { method: "POST", pattern: "bookings" },
  { method: "GET", pattern: "callbacks", query: ["status"] },
  { method: "POST", pattern: "callbacks/{id}/call" },
  { method: "POST", pattern: "callbacks/{id}/resolve" },
  // Back to open after a placed, unknown or failed call (owner/admin). Never dials.
  { method: "POST", pattern: "callbacks/{id}/reopen" },
  { method: "GET", pattern: "conversations", query: ["channel"] },
  { method: "GET", pattern: "conversations/{id}" },
  { method: "GET", pattern: "stats" },
  { method: "GET", pattern: "outbound-status" },
];

export const ADMIN_ROUTES: readonly StaffRoute[] = [
  { method: "GET", pattern: "appointments", query: ["from", "to", "status", "limit"] },
  { method: "POST", pattern: "appointments/{id}/cancel" },
  { method: "POST", pattern: "appointments/{id}/resolve" },
  { method: "GET", pattern: "clinic" },
  { method: "PATCH", pattern: "clinic" },
  { method: "GET", pattern: "doctors" },
  { method: "POST", pattern: "doctors" },
  { method: "PATCH", pattern: "doctors/{id}" },
  { method: "GET", pattern: "doctors/{id}/hours" },
  { method: "PUT", pattern: "doctors/{id}/hours" },
  { method: "GET", pattern: "services" },
  { method: "POST", pattern: "services" },
  { method: "PATCH", pattern: "services/{id}" },
  { method: "GET", pattern: "exceptions", query: ["from", "to"] },
  { method: "POST", pattern: "exceptions" },
  { method: "DELETE", pattern: "exceptions/{id}" },
];

function matches(pattern: string, segments: readonly string[]): boolean {
  const parts = pattern.split("/");
  if (parts.length !== segments.length) return false;
  return parts.every((part, i) => (part === "{id}" ? UUID.test(segments[i]!) : part === segments[i]));
}

/** The allowlisted route for this method and path, or null. Exported for tests. */
export function matchStaffRoute(
  routes: readonly StaffRoute[],
  method: string,
  segments: readonly string[],
): StaffRoute | null {
  const upper = method.toUpperCase();
  return routes.find((route) => route.method === upper && matches(route.pattern, segments)) ?? null;
}

function clearSessionOn401(response: Response): Response {
  if (response.status !== 401) return response;
  const next = new NextResponse(response.body, { status: response.status, headers: response.headers });
  next.cookies.set(sessionCookieName(), "", { ...sessionCookieOptions(new Date(0)), maxAge: 0 });
  return next;
}

function staffLike(prefix: "/v1/staff" | "/v1/admin", routes: readonly StaffRoute[]): Handler {
  return async (request, context) => {
    const { path } = await context.params;
    const segments = safeSegments(path);
    if (!segments) return notFound();
    const route = matchStaffRoute(routes, request.method, segments);
    if (!route) return notFound();
    const response = await forward(request, {
      path: `${prefix}/${segments.join("/")}`,
      search: pickQuery(new URL(request.url), route.query ?? []),
      staff: true,
    });
    return clearSessionOn401(response);
  };
}

export const staffHandler = staffLike("/v1/staff", STAFF_ROUTES);
export const adminHandler = staffLike("/v1/admin", ADMIN_ROUTES);
