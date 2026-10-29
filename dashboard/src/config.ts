/**
 * Build-time configuration. VITE_API_BASE_URL is the API origin (optionally with a path
 * prefix); empty means the dashboard's own origin (same-origin serving or the dev proxy).
 */

export function apiBaseUrl(raw: string | undefined = import.meta.env.VITE_API_BASE_URL): string {
  const value = (raw ?? "").trim();
  if (!value) return "";
  const url = new URL(value);
  const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if (url.protocol !== "https:" && !(url.protocol === "http:" && loopback)) {
    throw new Error("VITE_API_BASE_URL must be https (http only for localhost)");
  }
  if (url.search || url.hash || url.username || url.password) {
    throw new Error("VITE_API_BASE_URL must not contain a query, fragment or credentials");
  }
  return `${url.origin}${url.pathname}`.replace(/\/+$/, "");
}
