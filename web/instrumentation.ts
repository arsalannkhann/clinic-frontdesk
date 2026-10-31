/**
 * Startup check: a production server without API_BASE_URL refuses to start rather than
 * silently serving fallbacks. Skipped during `next build`, which has no runtime env.
 */
export async function register() {
  if (process.env.NEXT_RUNTIME !== "nodejs") return;
  if (process.env.NEXT_PHASE === "phase-production-build") return;
  if (process.env.NODE_ENV === "production" && !process.env.API_BASE_URL?.trim()) {
    throw new Error(
      "API_BASE_URL is not set. Set it to the FastAPI origin before starting the web app; mock fixtures are disabled in production.",
    );
  }
}
