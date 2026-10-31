import "server-only";

/**
 * Server-only configuration. `API_BASE_URL` never reaches the browser bundle: nothing here
 * is prefixed NEXT_PUBLIC_, and `server-only` makes importing this from a client component
 * a build error.
 */

export const DEFAULT_SLUG = "aster-dental";
const SLUG = /^[a-z0-9-]{3,40}$/;

export class ConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ConfigError";
  }
}

export interface ServerConfig {
  /** FastAPI origin without a trailing slash, or null in dev mock mode. */
  apiBaseUrl: string | null;
  slug: string;
  timeoutMs: number;
  chatTimeoutMs: number;
  mock: boolean;
  production: boolean;
}

function positiveInt(raw: string | undefined, fallback: number): number {
  const value = Number(raw);
  return Number.isInteger(value) && value > 0 && value <= 120_000 ? value : fallback;
}

/** Mock fixtures are allowed only outside production and only when no API is configured. */
export function isMockMode(env: NodeJS.ProcessEnv = process.env): boolean {
  return process.env.NODE_ENV !== "production" && !env.API_BASE_URL?.trim();
}

export function readConfig(env: NodeJS.ProcessEnv = process.env): ServerConfig {
  const production = process.env.NODE_ENV === "production";
  const raw = env.API_BASE_URL?.trim() ?? "";
  let apiBaseUrl: string | null = null;
  if (raw) {
    let url: URL;
    try {
      url = new URL(raw);
    } catch {
      throw new ConfigError("API_BASE_URL is not a valid URL.");
    }
    if (url.protocol !== "https:" && url.protocol !== "http:") {
      throw new ConfigError("API_BASE_URL must be an http(s) URL.");
    }
    if (production && url.protocol !== "https:" && !isLoopback(url.hostname)) {
      throw new ConfigError("API_BASE_URL must use https in production.");
    }
    apiBaseUrl = `${url.origin}${url.pathname.replace(/\/+$/, "")}`;
  } else if (production) {
    throw new ConfigError(
      "API_BASE_URL is not set. Set it to the FastAPI origin; mock fixtures are disabled in production.",
    );
  }
  const slug = env.CLINIC_SLUG?.trim() || DEFAULT_SLUG;
  if (!SLUG.test(slug)) throw new ConfigError("CLINIC_SLUG must match [a-z0-9-]{3,40}.");
  return {
    apiBaseUrl,
    slug,
    timeoutMs: positiveInt(env.API_TIMEOUT_MS, 10_000),
    chatTimeoutMs: positiveInt(env.CHAT_TIMEOUT_MS, 25_000),
    mock: apiBaseUrl === null,
    production,
  };
}

function isLoopback(hostname: string): boolean {
  return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "[::1]";
}

/** Canonical public origin for metadata; falls back to Vercel's URL or localhost. */
export function siteUrl(env: NodeJS.ProcessEnv = process.env): string {
  const candidates = [
    env.SITE_URL,
    env.VERCEL_PROJECT_PRODUCTION_URL && `https://${env.VERCEL_PROJECT_PRODUCTION_URL}`,
    "http://localhost:3000",
  ];
  for (const candidate of candidates) {
    if (!candidate) continue;
    try {
      return new URL(candidate).origin;
    } catch {
      // try the next one
    }
  }
  return "http://localhost:3000";
}
