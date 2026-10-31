/**
 * Startup check: a production server without API_BASE_URL or PUBLIC_PROXY_SECRET refuses to
 * start rather than serving fallbacks or calling the API without the proxy secret. Skipped
 * during `next build`, which has no runtime env.
 */
export function missingProductionSettings(env: NodeJS.ProcessEnv = process.env): string[] {
  if (env.NODE_ENV !== "production") return [];
  return ["API_BASE_URL", "PUBLIC_PROXY_SECRET"].filter((name) => !env[name]?.trim());
}

export async function register() {
  if (process.env.NEXT_RUNTIME !== "nodejs") return;
  if (process.env.NEXT_PHASE === "phase-production-build") return;
  const missing = missingProductionSettings();
  if (missing.length > 0) {
    throw new Error(
      `${missing.join(" and ")} must be set before the web app starts in production: mock fixtures are disabled and the API requires the proxy secret.`,
    );
  }
}
