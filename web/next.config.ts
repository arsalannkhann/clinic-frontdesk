import type { NextConfig } from "next";

const dev = process.env.NODE_ENV !== "production";

/**
 * Content Security Policy. Every page is same-origin: next/font self-hosts the fonts under
 * /_next/static, the BFF is /api/*, and there are no third-party scripts, frames or images.
 * Next's App Router streams its payload in inline <script> tags and next/font and React set
 * inline styles, so script-src and style-src allow 'unsafe-inline' (a nonce would force every
 * page to render dynamically). The voice call's capture worklet is a same-origin static file
 * (/voice-capture-worklet.js), covered by script-src 'self'. Development adds 'unsafe-eval' for React Refresh and ws: for
 * hot reload.
 */
const csp = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${dev ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  "font-src 'self'",
  "img-src 'self' data: blob:",
  // The web voice call plays the assistant's spoken reply from a blob: URL.
  "media-src 'self' blob: data:",
  // The real-time call (ADR 0014) signals and reconnects through LiveKit Cloud.
  `connect-src 'self' wss://*.livekit.cloud https://*.livekit.cloud${dev ? " ws: wss:" : ""}`,
  "frame-src 'none'",
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "object-src 'none'",
  "manifest-src 'self'",
  "worker-src 'self' blob:",
].join("; ");

const securityHeaders = [
  { key: "Content-Security-Policy", value: csp },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "X-Frame-Options", value: "DENY" },
  {
    key: "Permissions-Policy",
    // The microphone is for the web voice call on this origin only; embedded frames get none.
    value: "camera=(), microphone=(self), geolocation=(), payment=(), usb=()",
  },
];

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default nextConfig;
