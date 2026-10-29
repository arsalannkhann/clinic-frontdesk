import { describe, expect, it } from "vitest";
import { UserError } from "../src/api/errors";
import { formatInZone, isoToZoned, isValidTimeZone, zonedToIso } from "../src/time";

describe("zonedToIso", () => {
  it("converts clinic wall time to UTC and back", () => {
    expect(zonedToIso("2030-01-01T10:00", "Asia/Kolkata")).toBe("2030-01-01T04:30:00.000Z");
    expect(zonedToIso("2030-01-01", "Asia/Kolkata")).toBe("2029-12-31T18:30:00.000Z");
    expect(isoToZoned("2030-01-01T04:30:00Z", "Asia/Kolkata")).toBe("2030-01-01T10:00");
  });

  it("America/New_York: refuses the spring-forward gap, picks the earlier fall-back time", () => {
    expect(() => zonedToIso("2030-03-10T02:30", "America/New_York")).toThrow(UserError);
    expect(() => zonedToIso("2030-03-10T02:30", "America/New_York")).toThrow(/does not exist/);
    expect(zonedToIso("2030-03-10T03:00", "America/New_York")).toBe("2030-03-10T07:00:00.000Z");
    expect(zonedToIso("2030-03-10", "America/New_York")).toBe("2030-03-10T05:00:00.000Z");
    // 01:30 happens twice on 2030-11-03: EDT (05:30Z) then EST (06:30Z). Earlier wins.
    expect(zonedToIso("2030-11-03T01:30", "America/New_York")).toBe("2030-11-03T05:30:00.000Z");
  });

  it("Europe/London: refuses the gap, picks the earlier repeated time", () => {
    expect(() => zonedToIso("2030-03-31T01:30", "Europe/London")).toThrow(/does not exist/);
    expect(zonedToIso("2030-03-31T02:00", "Europe/London")).toBe("2030-03-31T01:00:00.000Z");
    expect(zonedToIso("2030-03-31", "Europe/London")).toBe("2030-03-31T00:00:00.000Z");
    expect(zonedToIso("2030-10-27T01:30", "Europe/London")).toBe("2030-10-27T00:30:00.000Z");
  });

  it("America/Havana: day start is correct when the clocks change at midnight", () => {
    // Spring forward 00:00 -> 01:00: midnight does not exist; the day starts at the change.
    expect(zonedToIso("2030-03-10", "America/Havana")).toBe("2030-03-10T05:00:00.000Z");
    expect(isoToZoned(zonedToIso("2030-03-10", "America/Havana"), "America/Havana")).toBe(
      "2030-03-10T01:00",
    );
    expect(() => zonedToIso("2030-03-10T00:30", "America/Havana")).toThrow(/does not exist/);
    // Fall back 01:00 -> 00:00: the day starts at the first midnight (CDT).
    expect(zonedToIso("2030-11-03", "America/Havana")).toBe("2030-11-03T04:00:00.000Z");
    expect(zonedToIso("2030-11-03T00:30", "America/Havana")).toBe("2030-11-03T04:30:00.000Z");
    expect(zonedToIso("2030-11-04", "America/Havana")).toBe("2030-11-04T05:00:00.000Z");
  });

  it("refuses malformed input and unknown zones instead of guessing", () => {
    expect(() => zonedToIso("2030-13-45T99:00x", "Asia/Kolkata")).toThrow(UserError);
    expect(() => zonedToIso("2030-01-01T10:00", "Mars/Olympus")).toThrow(/time zone/);
    expect(() => zonedToIso("2030-01-01T10:00", "")).toThrow(UserError);
    expect(isValidTimeZone("Asia/Kolkata")).toBe(true);
    expect(isValidTimeZone("Mars/Olympus")).toBe(false);
  });

  it("formats instants with the zone name", () => {
    expect(formatInZone("2030-01-01T04:30:00Z", "Asia/Kolkata")).toMatch(/10:00.*(IST|GMT\+5:30)/i);
    expect(formatInZone("2030-01-01T04:30:00Z", "UTC")).toMatch(/UTC/);
  });
});
