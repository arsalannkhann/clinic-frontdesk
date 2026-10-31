/**
 * Calendar grid maths, independent of React so it can be tested directly.
 *
 * A column is one clinic-zone day for one doctor (day view) or for the whole clinic (week
 * view). Rows are 15 minutes. Items are positioned by wall-clock minutes in the clinic zone;
 * an item that starts before or ends after the day is clipped to it. Overlapping items share
 * the column side by side in lanes: each cluster of transitively overlapping items gets as
 * many lanes as it needs, and every item in the cluster has the cluster's lane count, so
 * widths line up.
 */

import { dayKey, minutesOfDay } from "./time";

export const SLOT_MINUTES = 15;

export interface TimeWindow {
  /** Minutes since local midnight, inclusive. */
  start: number;
  /** Minutes since local midnight, exclusive. */
  end: number;
}

export interface Timed {
  id: string;
  starts_at: string;
  ends_at: string;
}

export interface Positioned<T extends Timed> {
  item: T;
  /** Clipped minutes of day. */
  startMin: number;
  endMin: number;
  /** Offset and size in minutes relative to the window start. */
  top: number;
  height: number;
  lane: number;
  lanes: number;
}

/** Clinic minutes of an item on `key`, or null when it does not touch that day. */
export function clipToDay(item: Timed, key: string, timeZone: string): { startMin: number; endMin: number } | null {
  const startKey = dayKey(item.starts_at, timeZone);
  const endKey = dayKey(item.ends_at, timeZone);
  if (startKey > key || endKey < key) return null;
  const startMin = startKey < key ? 0 : minutesOfDay(item.starts_at, timeZone);
  let endMin = endKey > key ? 24 * 60 : minutesOfDay(item.ends_at, timeZone);
  if (endKey === key && endMin === 0 && startKey < key) return null; // ends exactly at midnight
  if (endMin <= startMin) endMin = Math.min(24 * 60, startMin + SLOT_MINUTES);
  return { startMin, endMin };
}

const floorTo = (value: number, step: number) => Math.floor(value / step) * step;
const ceilTo = (value: number, step: number) => Math.ceil(value / step) * step;

/**
 * The visible window: the default clinic day, widened to whole hours to show anything
 * outside it, so nothing is ever hidden off-grid.
 */
export function visibleWindow(
  items: readonly Timed[],
  keys: readonly string[],
  timeZone: string,
  fallback: TimeWindow = { start: 9 * 60, end: 19 * 60 + 30 },
): TimeWindow {
  let start = fallback.start;
  let end = fallback.end;
  for (const item of items) {
    for (const key of keys) {
      const clip = clipToDay(item, key, timeZone);
      if (!clip) continue;
      start = Math.min(start, clip.startMin);
      end = Math.max(end, clip.endMin);
    }
  }
  return { start: floorTo(start, 60), end: Math.min(24 * 60, ceilTo(end, 30)) };
}

/** Lay out one column. Items outside `key` are dropped; the rest are clipped to `window`. */
export function layoutColumn<T extends Timed>(
  items: readonly T[],
  key: string,
  timeZone: string,
  window: TimeWindow,
): Positioned<T>[] {
  const placed: Omit<Positioned<T>, "lane" | "lanes">[] = [];
  for (const item of items) {
    const clip = clipToDay(item, key, timeZone);
    if (!clip) continue;
    const startMin = Math.max(clip.startMin, window.start);
    const endMin = Math.min(clip.endMin, window.end);
    if (endMin <= startMin) continue;
    placed.push({ item, startMin, endMin, top: startMin - window.start, height: endMin - startMin });
  }
  placed.sort((a, b) => a.startMin - b.startMin || b.endMin - a.endMin || a.item.id.localeCompare(b.item.id));

  const result: Positioned<T>[] = [];
  let cluster: Positioned<T>[] = [];
  let laneEnds: number[] = [];
  let clusterEnd = -1;
  const flush = () => {
    for (const entry of cluster) entry.lanes = laneEnds.length;
    result.push(...cluster);
    cluster = [];
    laneEnds = [];
  };
  for (const entry of placed) {
    if (cluster.length && entry.startMin >= clusterEnd) flush();
    let lane = laneEnds.findIndex((end) => end <= entry.startMin);
    if (lane < 0) {
      lane = laneEnds.length;
      laneEnds.push(entry.endMin);
    } else {
      laneEnds[lane] = entry.endMin;
    }
    cluster.push({ ...entry, lane, lanes: 1 });
    clusterEnd = cluster.length === 1 ? entry.endMin : Math.max(clusterEnd, entry.endMin);
  }
  if (cluster.length) flush();
  return result;
}

/** Hour and half-hour rows for the gutter. */
export function gridLines(window: TimeWindow): { minute: number; major: boolean }[] {
  const lines: { minute: number; major: boolean }[] = [];
  for (let minute = window.start; minute < window.end; minute += SLOT_MINUTES) {
    lines.push({ minute, major: minute % 60 === 0 });
  }
  return lines;
}

/** A pointer offset in a column (in minutes from the window top) -> the 15-minute slot. */
export function slotAt(offsetMinutes: number, window: TimeWindow): number | null {
  const minute = window.start + floorTo(Math.max(0, offsetMinutes), SLOT_MINUTES);
  return minute >= window.start && minute < window.end ? minute : null;
}

/** Where the current-time line sits on `key`, in minutes from the window top, or null. */
export function nowOffset(nowMs: number, key: string, timeZone: string, window: TimeWindow): number | null {
  if (dayKey(nowMs, timeZone) !== key) return null;
  const minute = minutesOfDay(nowMs, timeZone);
  if (minute < window.start || minute > window.end) return null;
  return minute - window.start;
}

/** Whether a 15-minute cell on `key` is covered by any of `items` (used to keep empty-slot
 * clicks off existing blocks and blocked time). */
export function isCovered(minute: number, key: string, timeZone: string, items: readonly Timed[]): boolean {
  return items.some((item) => {
    const clip = clipToDay(item, key, timeZone);
    return clip !== null && clip.startMin < minute + SLOT_MINUTES && clip.endMin > minute;
  });
}

/** Text colour that stays readable on a 14% tint of `hex` over paper: always ink. A solid
 * swatch of the service colour is used for the accent bar only. Validates the hex. */
export function safeColour(hex: string | null | undefined, fallback = "#6B6B6B"): string {
  return hex && /^#[0-9a-fA-F]{6}$/.test(hex) ? hex : fallback;
}
