/**
 * Times are shown and entered in the clinic's IANA timezone, not the browser's, and sent to
 * the API as UTC ISO 8601 instants.
 */

const LOCAL = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?$/;

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

/** "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM" wall time in `timeZone` -> UTC ISO string. */
export function zonedToIso(local: string, timeZone: string): string {
  const match = LOCAL.exec(local);
  if (!match) throw new Error("Invalid date/time");
  const [, y, mo, d, hh = "00", mm = "00"] = match;
  const naive = Date.UTC(Number(y), Number(mo) - 1, Number(d), Number(hh), Number(mm));
  const first = zoneOffsetMs(naive, timeZone);
  let utc = naive - first;
  const second = zoneOffsetMs(utc, timeZone);
  if (second !== first) utc = naive - second; // Across a DST transition.
  return new Date(utc).toISOString();
}

/** An instant -> "YYYY-MM-DDTHH:MM" wall time in `timeZone` (for datetime-local inputs). */
export function isoToZoned(iso: string, timeZone: string): string {
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return "";
  const local = new Date(ms + zoneOffsetMs(ms, timeZone));
  return local.toISOString().slice(0, 16);
}

/** Human-readable instant in the clinic's zone; the raw value if it cannot be parsed. */
export function formatInZone(iso: string | null | undefined, timeZone: string): string {
  if (!iso) return "";
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return iso;
  try {
    return new Intl.DateTimeFormat("en-IN", {
      timeZone,
      dateStyle: "medium",
      timeStyle: "short",
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
