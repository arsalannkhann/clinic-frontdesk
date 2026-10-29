import { readFileSync } from "node:fs";
import { join } from "node:path";
import openapiTS, { astToString } from "openapi-typescript";
import { describe, expect, it } from "vitest";
import { contentSecurityPolicy } from "../vite.config";
import { apiBaseUrl } from "../src/config";
import { zonedToIso, isoToZoned } from "../src/time";

const root = join(__dirname, "..");
const specPath = join(root, "..", "docs", "openapi.json");
const schemaPath = join(root, "src", "api", "schema.ts");

describe("generated API types", () => {
  it("are current with docs/openapi.json (run npm run gen:api)", async () => {
    const spec = JSON.parse(readFileSync(specPath, "utf8")) as Record<string, unknown>;
    const generated = astToString(await openapiTS(spec as never));
    const committed = readFileSync(schemaPath, "utf8");
    const body = (text: string) => text.slice(text.indexOf("export interface paths"));
    expect(body(committed).trim()).toBe(body(generated).trim());
  });

  it("cover every path in the contract", () => {
    const spec = JSON.parse(readFileSync(specPath, "utf8")) as { paths: Record<string, unknown> };
    const committed = readFileSync(schemaPath, "utf8");
    for (const path of Object.keys(spec.paths)) expect(committed).toContain(`"${path}": {`);
  });
});

describe("configuration", () => {
  it("accepts https or loopback http API base URLs only", () => {
    expect(apiBaseUrl("")).toBe("");
    expect(apiBaseUrl("https://api.clinic.example/")).toBe("https://api.clinic.example");
    expect(apiBaseUrl("http://127.0.0.1:8000")).toBe("http://127.0.0.1:8000");
    expect(() => apiBaseUrl("http://api.clinic.example")).toThrow();
    expect(() => apiBaseUrl("https://api.clinic.example/?token=x")).toThrow();
    expect(() => apiBaseUrl("https://user:pw@api.clinic.example")).toThrow();
  });

  it("builds a CSP that allows only self and the API origin", () => {
    const csp = contentSecurityPolicy("https://api.clinic.example/base");
    expect(csp).toContain("connect-src 'self' https://api.clinic.example;");
    expect(csp).toContain("script-src 'self';");
    expect(csp).toContain("require-trusted-types-for 'script'");
    expect(csp).not.toContain("unsafe-inline");
    expect(csp).not.toContain("unsafe-eval");
    expect(contentSecurityPolicy("")).toContain("connect-src 'self';");
  });
});

describe("clinic time", () => {
  it("converts clinic wall time to UTC and back", () => {
    expect(zonedToIso("2030-01-01T10:00", "Asia/Kolkata")).toBe("2030-01-01T04:30:00.000Z");
    expect(zonedToIso("2030-01-01", "Asia/Kolkata")).toBe("2029-12-31T18:30:00.000Z");
    expect(isoToZoned("2030-01-01T04:30:00Z", "Asia/Kolkata")).toBe("2030-01-01T10:00");
    // Across a DST change (New York, 2030-03-10).
    expect(zonedToIso("2030-03-10T12:00", "America/New_York")).toBe("2030-03-10T16:00:00.000Z");
    expect(zonedToIso("2030-03-09T12:00", "America/New_York")).toBe("2030-03-09T17:00:00.000Z");
  });
});
