import { type Plugin, loadEnv } from "vite";
import { defineConfig } from "vitest/config";

/**
 * Content-Security-Policy for the built dashboard, injected as a meta tag. connect-src is
 * the dashboard's own origin plus the API origin from VITE_API_BASE_URL. Trusted Types are
 * enforced: the code never assigns HTML strings to DOM sinks. Headers that a meta tag
 * cannot carry (frame-ancestors, HSTS, ...) are documented in docs/SECURITY.md for the
 * server that hosts dist/.
 */
export function contentSecurityPolicy(apiBaseUrl: string): string {
  const connect = ["'self'"];
  if (apiBaseUrl.trim()) connect.push(new URL(apiBaseUrl).origin);
  return [
    "default-src 'none'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self'",
    "font-src 'self'",
    `connect-src ${connect.join(" ")}`,
    "base-uri 'none'",
    "form-action 'none'",
    "object-src 'none'",
    "require-trusted-types-for 'script'",
    "trusted-types 'none'",
  ].join("; ");
}

function cspMeta(apiBaseUrl: string): Plugin {
  return {
    name: "dashboard-csp",
    apply: "build", // The dev server injects inline client scripts; CSP applies to builds.
    transformIndexHtml() {
      return [
        {
          tag: "meta",
          attrs: { "http-equiv": "Content-Security-Policy", content: contentSecurityPolicy(apiBaseUrl) },
          injectTo: "head-prepend",
        },
      ];
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const apiBaseUrl = env["VITE_API_BASE_URL"] ?? "";
  const proxyTarget = env["DASHBOARD_API_PROXY_TARGET"] || "http://127.0.0.1:8000";
  return {
    plugins: [cspMeta(apiBaseUrl)],
    build: {
      modulePreload: { polyfill: false },
      sourcemap: false,
    },
    server: {
      host: "127.0.0.1",
      port: 5173,
      strictPort: true,
      // With VITE_API_BASE_URL empty, the browser calls /v1 on the dev server's origin and
      // Vite forwards it: no CORS configuration is needed for local development.
      proxy: { "/v1": { target: proxyTarget, changeOrigin: false } },
    },
    preview: { host: "127.0.0.1", port: 4173, strictPort: true },
    test: {
      environment: "jsdom",
      include: ["tests/**/*.test.ts"],
    },
  };
});
