import { describe, expect, it } from "vitest";

import { clipToDay, gridLines, isCovered, layoutColumn, nowOffset, safeColour, slotAt, visibleWindow } from "@/lib/calendar";
import {
  addDays,
  dayKey,
  dayRange,
  formatTime,
  minutesOfDay,
  startOfWeek,
  TimeError,
  weekdayOf,
  zonedToIso,
  zonedToMs,
} from "@/lib/time";

const IST = "Asia/Kolkata";
const NY = "America/New_York";
const at = (local: string) => zonedToIso(local, IST);
const item = (id: string, start: string, end: string) => ({ id, starts_at: at(start), ends_at: at(end) });
const DAY = "2026-11-02";
const WINDOW = { start: 9 * 60, end: 19 * 60 + 30 };

describe("clinic time zone helpers", () => {
  it("converts clinic wall time to an instant with the zone offset", () => {
    expect(zonedToIso("2026-11-02T09:30", IST)).toBe("2026-11-02T04:00:00.000Z");
    expect(zonedToMs("2026-11-02", IST)).toBe(Date.parse("2026-11-01T18:30:00Z"));
    // Ambiguous wall time at the DST fall-back resolves to the earlier instant.
    expect(zonedToIso("2026-11-01T01:30", NY)).toBe("2026-11-01T05:30:00.000Z");
  });

  it("rejects a wall time that does not exist (DST gap)", () => {
    expect(() => zonedToMs("2026-03-08T02:30", NY)).toThrow(TimeError);
  });

  it("reads day keys and minutes in the clinic zone, not the browser's", () => {
    const late = "2026-11-02T20:00:00Z"; // 01:30 on the 3rd in India
    expect(dayKey(late, IST)).toBe("2026-11-03");
    expect(minutesOfDay(late, IST)).toBe(90);
    expect(formatTime(late, IST)).toMatch(/^1:30\s?am$/i);
  });

  it("does day arithmetic on keys", () => {
    expect(addDays("2026-12-31", 1)).toBe("2027-01-01");
    expect(addDays("2026-03-01", -1)).toBe("2026-02-28");
    expect(weekdayOf("2026-11-02")).toBe(0); // Monday
    expect(startOfWeek("2026-11-08")).toBe("2026-11-02"); // Sunday -> Monday
    expect(dayRange("2026-11-02", IST)).toEqual({ from: "2026-11-01T18:30:00.000Z", to: "2026-11-02T18:30:00.000Z" });
  });
});

describe("calendar layout", () => {
  it("handles a day with no appointments", () => {
    expect(layoutColumn([], DAY, IST, WINDOW)).toEqual([]);
    expect(visibleWindow([], [DAY], IST)).toEqual({ start: 540, end: 1170 });
  });

  it("positions an item by clinic minutes relative to the window", () => {
    const [placed] = layoutColumn([item("a", "2026-11-02T10:00", "2026-11-02T10:45")], DAY, IST, WINDOW);
    expect(placed).toMatchObject({ startMin: 600, endMin: 645, top: 60, height: 45, lane: 0, lanes: 1 });
  });

  it("puts overlapping items side by side with a shared lane count", () => {
    const placed = layoutColumn(
      [
        item("a", "2026-11-02T10:00", "2026-11-02T11:00"),
        item("b", "2026-11-02T10:15", "2026-11-02T10:45"),
        item("c", "2026-11-02T10:50", "2026-11-02T11:30"), // overlaps a only; reuses b's lane
        item("d", "2026-11-02T12:00", "2026-11-02T12:30"), // separate cluster
      ],
      DAY,
      IST,
      WINDOW,
    );
    const by = Object.fromEntries(placed.map((p) => [p.item.id, p]));
    expect(by.a).toMatchObject({ lane: 0, lanes: 2 });
    expect(by.b).toMatchObject({ lane: 1, lanes: 2 });
    expect(by.c).toMatchObject({ lane: 1, lanes: 2 });
    expect(by.d).toMatchObject({ lane: 0, lanes: 1 });
  });

  it("treats back-to-back items as not overlapping", () => {
    const placed = layoutColumn(
      [item("a", "2026-11-02T10:00", "2026-11-02T10:30"), item("b", "2026-11-02T10:30", "2026-11-02T11:00")],
      DAY,
      IST,
      WINDOW,
    );
    expect(placed.map((p) => p.lanes)).toEqual([1, 1]);
  });

  it("clips items that cross midnight and drops those on other days", () => {
    const overnight = item("n", "2026-11-01T22:00", "2026-11-02T01:00");
    expect(clipToDay(overnight, "2026-11-01", IST)).toEqual({ startMin: 1320, endMin: 1440 });
    expect(clipToDay(overnight, "2026-11-02", IST)).toEqual({ startMin: 0, endMin: 60 });
    expect(clipToDay(overnight, "2026-11-03", IST)).toBeNull();
    expect(clipToDay(item("m", "2026-11-01T22:00", "2026-11-02T00:00"), "2026-11-02", IST)).toBeNull();
  });

  it("widens the window to whole hours so nothing is hidden", () => {
    const window = visibleWindow(
      [item("early", "2026-11-02T07:40", "2026-11-02T08:10"), item("late", "2026-11-02T20:05", "2026-11-02T20:20")],
      [DAY],
      IST,
    );
    expect(window).toEqual({ start: 420, end: 1230 });
  });

  it("maps clicks to 15-minute slots inside the window only", () => {
    expect(slotAt(0, WINDOW)).toBe(540);
    expect(slotAt(29, WINDOW)).toBe(555);
    expect(slotAt(-10, WINDOW)).toBe(540);
    expect(slotAt(WINDOW.end - WINDOW.start + 5, WINDOW)).toBeNull();
    expect(gridLines({ start: 540, end: 600 })).toEqual([
      { minute: 540, major: true },
      { minute: 555, major: false },
      { minute: 570, major: false },
      { minute: 585, major: false },
    ]);
  });

  it("knows which cells are taken", () => {
    const items = [item("a", "2026-11-02T10:00", "2026-11-02T10:30")];
    expect(isCovered(600, DAY, IST, items)).toBe(true);
    expect(isCovered(615, DAY, IST, items)).toBe(true);
    expect(isCovered(630, DAY, IST, items)).toBe(false);
    expect(isCovered(585, DAY, IST, items)).toBe(false);
  });

  it("places the current-time line only on today, inside the window", () => {
    const now = zonedToMs("2026-11-02T11:20", IST);
    expect(nowOffset(now, DAY, IST, WINDOW)).toBe(140);
    expect(nowOffset(now, "2026-11-03", IST, WINDOW)).toBeNull();
    expect(nowOffset(zonedToMs("2026-11-02T21:00", IST), DAY, IST, WINDOW)).toBeNull();
  });

  it("only accepts hex colours", () => {
    expect(safeColour("#3F7D6E")).toBe("#3F7D6E");
    expect(safeColour("red; background:url(x)")).toBe("#6B6B6B");
    expect(safeColour(null)).toBe("#6B6B6B");
  });

  it("lays out a week of days", () => {
    const days = Array.from({ length: 7 }, (_, i) => addDays("2026-11-02", i));
    const items = [item("a", "2026-11-04T09:30", "2026-11-04T10:00")];
    expect(days.map((d) => layoutColumn(items, d, IST, WINDOW).length)).toEqual([0, 0, 1, 0, 0, 0, 0]);
  });
});
