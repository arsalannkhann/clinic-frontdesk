import "server-only";

import { cache } from "react";

import type { ClinicProfile, PublicDoctor, PublicService } from "@/lib/api/types";
import { ConfigError, readConfig } from "./config";
import type { UpstreamResult } from "./upstream";

/**
 * Public clinic data for server components, fetched straight from the API (no BFF hop) with
 * time-based revalidation. Any failure returns null so pages render a graceful fallback
 * instead of an error page; the failure is logged without details.
 */

export const REVALIDATE_SECONDS = 300;

type Resource = "" | "services" | "doctors";

let configErrorLogged = false;

async function load(resource: Resource): Promise<unknown | null> {
  let config;
  try {
    config = readConfig();
  } catch (error) {
    if (error instanceof ConfigError && !configErrorLogged) {
      configErrorLogged = true;
      console.error(`[web] Configuration error: ${error.message} Public pages are showing fallback content.`);
    }
    return null;
  }
  const path = `/v1/public/clinics/${config.slug}${resource ? `/${resource}` : ""}`;
  try {
    if (config.mock) {
      if (process.env.NODE_ENV !== "production") {
        const { mockUpstream } = await import("./mock/router");
        const result: UpstreamResult = await mockUpstream({
          method: "GET",
          path,
          search: "",
          body: null,
          token: null,
          ip: null,
          idempotencyKey: null,
        });
        return result.status === 200 ? result.body : null;
      }
      return null;
    }
    // The API refuses /v1/public/* without the proxy secret (403), and answers 503 when it has
    // none configured; either way this returns null and the page shows its fallback.
    const headers: Record<string, string> = { Accept: "application/json" };
    if (config.proxySecret) headers["X-Proxy-Secret"] = config.proxySecret;
    const response = await fetch(`${config.apiBaseUrl}${path}`, {
      headers,
      next: { revalidate: REVALIDATE_SECONDS, tags: [`clinic:${config.slug}`] },
      signal: AbortSignal.timeout(config.timeoutMs),
    });
    if (!response.ok) {
      console.error(`[web] Public ${resource || "profile"} request failed with ${response.status}`);
      return null;
    }
    return await response.json();
  } catch {
    console.error(`[web] Public ${resource || "profile"} request failed`);
    return null;
  }
}

function isProfile(value: unknown): value is ClinicProfile {
  return !!value && typeof value === "object" && typeof (value as ClinicProfile).name === "string" && typeof (value as ClinicProfile).timezone === "string";
}

export const getClinic = cache(async (): Promise<ClinicProfile | null> => {
  const value = await load("");
  return isProfile(value) ? value : null;
});

export const getServices = cache(async (): Promise<PublicService[] | null> => {
  const value = await load("services");
  return Array.isArray(value) ? (value as PublicService[]).filter((s) => typeof s?.id === "string" && typeof s?.name === "string") : null;
});

export const getDoctors = cache(async (): Promise<PublicDoctor[] | null> => {
  const value = await load("doctors");
  return Array.isArray(value)
    ? (value as PublicDoctor[])
        .filter((d) => typeof d?.id === "string" && typeof d?.name === "string")
        .map((d) => ({ ...d, service_ids: Array.isArray(d.service_ids) ? d.service_ids : [] }))
    : null;
});

export function isMock(): boolean {
  try {
    return readConfig().mock;
  } catch {
    return false;
  }
}
