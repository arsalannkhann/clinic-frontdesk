/**
 * Clinic-time helpers. Every time on screen is formatted in the clinic's IANA zone from the
 * API response, never the browser's zone, and instants go back to the API as UTC ISO 8601.
 * Day keys ("YYYY-MM-DD") always mean a calendar day in the clinic zone.
 *
 * zonedToIso follows the staff dashboard's rules (dashboard/src/time.ts): a local time in a
 * spring-forward gap is refused, an ambiguous fall-back time takes the earlier instant, and a
 * bare date means the first instant of that local day.
 */

export class TimeError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "TimeError";
  }
}

const LOCAL = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?$/;
const DAY_KEY = /^(\d{4})-(\d{2})-(\d{2})$/;
const DAY = 86_400_000;
const MINUTE = 60_000;

export function isValidTimeZone(timeZone: string | null | undefined): timeZone is string {
  if (!timeZone) return false;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone });
    return true;
  } catch {
    return false;
  }
}

const partsCache = new Map<string, Intl.DateTimeFormat>();
function partsFormatter(timeZone: string): Intl.DateTimeFormat {
  let formatter = partsCache.get(timeZone);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat("en-US", {
      timeZone,
      hourCycle: "h23",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
    partsCache.set(timeZone, formatter);
  }
  return formatter;
}

interface WallParts {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  second: number;
}

function wallParts(utcMs: number, timeZone: string): WallParts {
  const parts = partsFormatter(timeZone).formatToParts(new Date(utcMs));
  const get = (type: Intl.DateTimeFormatPartTypes): number =>
    Number(parts.find((part) => part.type === type)?.value ?? "0");
  return {
    year: get("year"),
    month: get("month"),
    day: get("day"),
    hour: get("hour") % 24,
    minute: get("minute"),
    second: get("second"),
  };
}

/** Offset of `timeZone` from UTC at `utcMs` (wall time minus UTC), in ms. */
export function zoneOffsetMs(utcMs: number, timeZone: string): number {
  const p = wallParts(utcMs, timeZone);
  const asUtc = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second);
  return asUtc - (utcMs - (((utcMs % 1000) + 1000) % 1000));
}

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

/** "YYYY-MM-DD" (start of day) or "YYYY-MM-DDTHH:MM" in `timeZone` -> UTC epoch ms. */
export function zonedToMs(local: string, timeZone: string): number {
  if (!isValidTimeZone(timeZone)) throw new TimeError("The clinic time zone is unknown.");
  const match = LOCAL.exec(local);
  if (!match) throw new TimeError("Enter a valid date and time.");
  const [, y, mo, d, hh, mm] = match;
  const naive = Date.UTC(Number(y), Number(mo) - 1, Number(d), Number(hh ?? 0), Number(mm ?? 0));
  const candidates = instantsFor(naive, timeZone);
  const earliest = candidates[0];
  if (earliest !== undefined) return earliest;
  if (hh === undefined) {
    const before = zoneOffsetMs(naive - DAY, timeZone);
    const after = zoneOffsetMs(naive + DAY, timeZone);
    return firstWithOffset(naive - after, naive - before, after, timeZone);
  }
  throw new TimeError(
    `${local.replace("T", " ")} does not exist in ${timeZone} (the clocks skip it). Choose another time.`,
  );
}

export function zonedToIso(local: string, timeZone: string): string {
  return new Date(zonedToMs(local, timeZone)).toISOString();
}

function toMs(value: string | number | Date): number {
  if (typeof value === "number") return value;
  if (value instanceof Date) return value.getTime();
  return Date.parse(value);
}

/** The clinic-zone day key of an instant. */
export function dayKey(value: string | number | Date, timeZone: string): string {
  const p = wallParts(toMs(value), timeZone);
  return `${p.year}-${String(p.month).padStart(2, "0")}-${String(p.day).padStart(2, "0")}`;
}

/** Minutes since local midnight of an instant in the clinic zone (wall clock). */
export function minutesOfDay(value: string | number | Date, timeZone: string): number {
  const p = wallParts(toMs(value), timeZone);
  return p.hour * 60 + p.minute;
}

/** "YYYY-MM-DD" + n calendar days (pure date arithmetic, no zone involved). */
export function addDays(key: string, days: number): string {
  const match = DAY_KEY.exec(key);
  if (!match) throw new TimeError("Invalid day.");
  const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]) + days));
  return date.toISOString().slice(0, 10);
}

/** Monday = 0 … Sunday = 6, for a day key. */
export function weekdayOf(key: string): number {
  return (new Date(`${key}T00:00:00Z`).getUTCDay() + 6) % 7;
}

/** The Monday of the week containing `key`. */
export function startOfWeek(key: string): string {
  return addDays(key, -weekdayOf(key));
}

export function todayKey(timeZone: string, now = Date.now()): string {
  return dayKey(now, timeZone);
}

/** [start, end) of a clinic-zone day as UTC ISO strings. */
export function dayRange(key: string, timeZone: string): { from: string; to: string } {
  return { from: zonedToIso(key, timeZone), to: zonedToIso(addDays(key, 1), timeZone) };
}

const formatters = new Map<string, Intl.DateTimeFormat>();
function formatter(timeZone: string, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `${timeZone}|${JSON.stringify(options)}`;
  let value = formatters.get(key);
  if (!value) {
    value = new Intl.DateTimeFormat("en-IN", { timeZone, ...options });
    formatters.set(key, value);
  }
  return value;
}

/** "9:30 am" */
export function formatTime(value: string | number | Date, timeZone: string): string {
  const ms = toMs(value);
  if (!Number.isFinite(ms)) return "";
  return formatter(timeZone, { hour: "numeric", minute: "2-digit", hour12: true })
    .format(ms)
    .replace(/\s?([AP])M$/i, (_, p: string) => ` ${p.toLowerCase()}m`);
}

/** "Monday, 2 November" */
export function formatLongDate(value: string | number | Date, timeZone: string): string {
  const ms = toMs(value);
  if (!Number.isFinite(ms)) return "";
  return formatter(timeZone, { weekday: "long", day: "numeric", month: "long" }).format(ms);
}

/** "Mon 2 Nov" */
export function formatShortDate(value: string | number | Date, timeZone: string): string {
  const ms = toMs(value);
  if (!Number.isFinite(ms)) return "";
  return formatter(timeZone, { weekday: "short", day: "numeric", month: "short" }).format(ms);
}

/** "Monday, 2 November at 9:30 am" */
export function formatDateTime(value: string | number | Date, timeZone: string): string {
  return `${formatLongDate(value, timeZone)} at ${formatTime(value, timeZone)}`;
}

/** Day-key label without needing an instant: noon UTC is the same calendar day everywhere
 * that matters for a label. */
export function formatDayKey(key: string, style: "long" | "short" = "short"): string {
  const ms = Date.parse(`${key}T12:00:00Z`);
  return style === "long" ? formatLongDate(ms, "UTC") : formatShortDate(ms, "UTC");
}

/** Short zone name, e.g. "IST". */
export function zoneAbbreviation(timeZone: string, at = Date.now()): string {
  const part = formatter(timeZone, { timeZoneName: "short" })
    .formatToParts(at)
    .find((p) => p.type === "timeZoneName");
  return part?.value ?? timeZone;
}

/** "HH:MM" from minutes of day. */
export function hhmm(minutes: number): string {
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

/** Human "9:30 am" label from minutes of day. */
export function minutesLabel(minutes: number): string {
  const h = Math.floor(minutes / 60) % 24;
  const m = minutes % 60;
  const suffix = h < 12 ? "am" : "pm";
  const hour = h % 12 === 0 ? 12 : h % 12;
  return m === 0 ? `${hour} ${suffix}` : `${hour}:${String(m).padStart(2, "0")} ${suffix}`;
}
