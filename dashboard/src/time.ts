/**
 * Times are shown and entered in the clinic's IANA timezone, not the browser's, and sent to
 * the API as UTC ISO 8601 instants.
 *
 * Wall-clock rules (zonedToIso):
 * - A local time inside a spring-forward gap does not exist and is refused (UserError).
 * - A local time repeated by a fall-back change is ambiguous; the EARLIER instant (the
 *   first occurrence, before the clocks go back) is chosen.
 * - A date without a time means the start of that day: the first instant whose local date
 *   is that date. When the clocks jump at midnight (e.g. America/Havana) midnight does not
 *   exist and the day starts at the transition instant; when they fall back at midnight the
 *   first midnight is used.
 */
import { UserError } from "./api/errors";

const LOCAL = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?$/;
const DAY = 86_400_000;
const MINUTE = 60_000;

/** True when `timeZone` is an IANA zone this browser knows. */
export function isValidTimeZone(timeZone: string): boolean {
  if (!timeZone) return false;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone });
    return true;
  } catch {
    return false;
  }
}

/** Offset of `timeZone` from UTC at the instant `utcMs` (local wall time minus UTC). */
function zoneOffsetMs(utcMs: number, timeZone: string): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).formatToParts(new Date(utcMs));
  const get = (type: Intl.DateTimeFormatPartTypes): number =>
    Number(parts.find((part) => part.type === type)?.value ?? "0");
  const asUtc = Date.UTC(
    get("year"),
    get("month") - 1,
    get("day"),
    get("hour"),
    get("minute"),
    get("second"),
  );
  return asUtc - (utcMs - (((utcMs % 1000) + 1000) % 1000));
}

/** Every instant whose wall time in `timeZone` equals `naive` (wall time encoded as UTC). */
function instantsFor(naive: number, timeZone: string): number[] {
  const offsets = new Set([
    zoneOffsetMs(naive - DAY, timeZone),
    zoneOffsetMs(naive, timeZone),
    zoneOffsetMs(naive + DAY, timeZone),
  ]);
  const found = new Set<number>();
  for (const offset of offsets) {
    const utc = naive - offset;
    if (zoneOffsetMs(utc, timeZone) === offset) found.add(utc);
  }
  return [...found].sort((a, b) => a - b);
}

/** The first instant (to the minute) in (lo, hi] whose offset equals `offset`. */
function firstWithOffset(lo: number, hi: number, offset: number, timeZone: string): number {
  let low = Math.floor(lo / MINUTE);
  let high = Math.ceil(hi / MINUTE);
  while (high - low > 1) {
    const mid = Math.floor((low + high) / 2);
    if (zoneOffsetMs(mid * MINUTE, timeZone) === offset) high = mid;
    else low = mid;
  }
  return high * MINUTE;
}

function parse(local: string): { naive: number; dateOnly: boolean } {
  const match = LOCAL.exec(local);
  if (!match) throw new UserError("Enter a valid date and time.");
  const [, y, mo, d, hh, mm] = match;
  const naive = Date.UTC(Number(y), Number(mo) - 1, Number(d), Number(hh ?? 0), Number(mm ?? 0));
  return { naive, dateOnly: hh === undefined };
}

/** "YYYY-MM-DD" (start of day) or "YYYY-MM-DDTHH:MM" wall time in `timeZone` -> UTC ISO. */
export function zonedToIso(local: string, timeZone: string): string {
  if (!isValidTimeZone(timeZone)) {
    throw new UserError("The clinic time zone is unknown; times cannot be converted.");
  }
  const { naive, dateOnly } = parse(local);
  const candidates = instantsFor(naive, timeZone);
  const earliest = candidates[0];
  if (earliest !== undefined) return new Date(earliest).toISOString(); // Earlier if ambiguous.
  if (dateOnly) {
    // Midnight falls in a spring-forward gap: the day starts at the transition.
    const before = zoneOffsetMs(naive - DAY, timeZone);
    const after = zoneOffsetMs(naive + DAY, timeZone);
    return new Date(firstWithOffset(naive - after, naive - before, after, timeZone)).toISOString();
  }
  throw new UserError(
    `${local.replace("T", " ")} does not exist in ${timeZone} (the clocks skip it). ` +
      "Choose a time after the change.",
  );
}

/** An instant -> "YYYY-MM-DDTHH:MM" wall time in `timeZone` (for datetime-local inputs). */
export function isoToZoned(iso: string, timeZone: string): string {
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return "";
  const local = new Date(ms + zoneOffsetMs(ms, timeZone));
  return local.toISOString().slice(0, 16);
}

/** Human-readable instant with its zone name; the raw value if it cannot be formatted. */
export function formatInZone(iso: string | null | undefined, timeZone: string): string {
  if (!iso) return "";
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return iso;
  try {
    return new Intl.DateTimeFormat("en-IN", {
      timeZone,
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      timeZoneName: "short",
    }).format(new Date(ms));
  } catch {
    return new Date(ms).toISOString();
  }
}

export function timeZones(): string[] {
  const intl = Intl as unknown as { supportedValuesOf?: (key: string) => string[] };
  try {
    return intl.supportedValuesOf?.("timeZone") ?? [];
  } catch {
    return [];
  }
}
