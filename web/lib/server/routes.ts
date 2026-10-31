import "server-only";

import { NextResponse } from "next/server";

import { ConfigError, readConfig } from "./config";
import { configErrorResponse, forward, pickQuery, safeSegments } from "./upstream";
import { errorJson } from "./errors";
import { sessionCookieName, sessionCookieOptions } from "./request";

/**
 * Path allowlists for the catch-all route handlers. The browser can reach only these API
 * routes through the BFF; anything else is a 404 without an upstream call.
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

/** /api/chat/* -> /v1/chat/* (session creation is scoped to the configured clinic). */
export const chatHandler: Handler = async (request, context) => {
  const { path } = await context.params;
  const segments = safeSegments(path);
  if (!segments || request.method.toUpperCase() !== "POST") return notFound();
  const clinic = slug();
  if (clinic instanceof Response) return clinic;
  const [first, id, kind, pid, action] = segments;
  if (first !== "sessions") return notFound();
  if (segments.length === 1) {
    return forward(request, { path: `/v1/chat/clinics/${clinic}/sessions` });
  }
  if (segments.length === 3 && kind === "messages") {
    return forward(request, { path: `/v1/chat/sessions/${id}/messages`, chat: true });
  }
  if (segments.length === 5 && kind === "proposals" && action === "confirm") {
    return forward(request, { path: `/v1/chat/sessions/${id}/proposals/${pid}/confirm`, chat: true });
  }
  return notFound();
};

const STAFF_METHODS = new Set(["GET", "POST", "PATCH", "PUT", "DELETE"]);

function clearSessionOn401(response: Response): Response {
  if (response.status !== 401) return response;
  const next = new NextResponse(response.body, { status: response.status, headers: response.headers });
  next.cookies.set(sessionCookieName(), "", { ...sessionCookieOptions(new Date(0)), maxAge: 0 });
  return next;
}

function staffLike(prefix: "/v1/staff" | "/v1/admin", query: readonly string[]): Handler {
  return async (request, context) => {
    const { path } = await context.params;
    const segments = safeSegments(path);
    if (!segments || !STAFF_METHODS.has(request.method.toUpperCase())) return notFound();
    // Login and logout have their own handlers; never proxy them with a cookie.
    if (prefix === "/v1/staff" && (segments[0] === "login" || segments[0] === "logout")) return notFound();
    const response = await forward(request, {
      path: `${prefix}/${segments.join("/")}`,
      search: pickQuery(new URL(request.url), query),
      staff: true,
    });
    return clearSessionOn401(response);
  };
}

export const staffHandler = staffLike("/v1/staff", ["from", "to", "status", "channel", "limit"]);
export const adminHandler = staffLike("/v1/admin", ["from", "to", "status", "limit"]);
