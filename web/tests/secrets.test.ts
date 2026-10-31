// @vitest-environment node
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * Server-only values must never reach the browser bundle. A module can only be bundled for the
 * client if it is reachable from client code; everything under lib/server/ imports
 * `server-only`, which makes such an import a build error. These checks keep it that way.
 */

const ROOT = join(__dirname, "..");
const SERVER_ONLY_NAMES = ["PUBLIC_PROXY_SECRET", "API_BASE_URL"];

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return files(path);
    return /\.(ts|tsx|mjs|js)$/.test(name) ? [path] : [];
  });
}

const source = ["app", "components", "lib", "middleware.ts", "instrumentation.ts", "next.config.ts"].flatMap((p) => {
  const path = join(ROOT, p);
  return statSync(path).isDirectory() ? files(path) : [path];
});
const rel = (path: string) => relative(ROOT, path);

describe("server-only secrets", () => {
  it("are read only inside lib/server", () => {
    const offenders = source.filter(
      (path) => !rel(path).startsWith("lib/server/") && SERVER_ONLY_NAMES.some((name) => readFileSync(path, "utf8").includes(name)),
    );
    // instrumentation.ts runs only on the server at startup and checks API_BASE_URL presence.
    expect(offenders.map(rel).filter((p) => p !== "instrumentation.ts")).toEqual([]);
  });

  it("are never exposed with a NEXT_PUBLIC_ prefix or next.config env", () => {
    for (const path of source) {
      const text = readFileSync(path, "utf8");
      expect(text, rel(path)).not.toMatch(/NEXT_PUBLIC_(PUBLIC_PROXY_SECRET|PROXY_SECRET|API_BASE_URL)/);
    }
    expect(readFileSync(join(ROOT, "next.config.ts"), "utf8")).not.toMatch(/\benv\s*:/);
  });

  it("live in modules that import server-only", () => {
    for (const path of files(join(ROOT, "lib/server"))) {
      const text = readFileSync(path, "utf8");
      if (SERVER_ONLY_NAMES.some((name) => text.includes(name)) || rel(path).endsWith("config.ts")) {
        expect(text, rel(path)).toMatch(/^import "server-only";/m);
      }
    }
  });

  it("are not imported by client components", () => {
    for (const path of source) {
      const text = readFileSync(path, "utf8");
      if (!/^["']use client["']/.test(text)) continue;
      expect(text, rel(path)).not.toMatch(/from ["']@\/lib\/server\//);
    }
  });
});
