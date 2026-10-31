import { NextResponse, type NextRequest } from "next/server";

import { sessionCookieName } from "@/lib/session-cookie";

/**
 * Unauthenticated visits to the staff console go to the sign-in page. This only checks that
 * a session cookie exists; the API validates it on every call, and a 401 clears it.
 */
export function middleware(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  if (pathname === "/staff/login") return NextResponse.next();
  if (request.cookies.has(sessionCookieName())) return NextResponse.next();
  const url = request.nextUrl.clone();
  url.pathname = "/staff/login";
  url.search = "";
  const next = `${pathname}${search}`;
  if (next !== "/staff") url.searchParams.set("next", next);
  return NextResponse.redirect(url);
}

export const config = {
  matcher: ["/staff", "/staff/:path*"],
};
