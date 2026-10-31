/**
 * The staff session cookie name, shared by route handlers and middleware (which runs on the
 * edge runtime, so this module has no server-only imports).
 *
 * `__Host-` binds the cookie to this exact origin (Secure, Path=/, no Domain). Browsers
 * refuse `__Host-` cookies without Secure, so plain-http development uses another name.
 */
export function sessionCookieName(production = process.env.NODE_ENV === "production"): string {
  return production ? "__Host-staff_session" : "staff_session";
}
