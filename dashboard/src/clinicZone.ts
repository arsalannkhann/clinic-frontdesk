import { ApiError, UserError } from "./api/errors";
import type { Context } from "./context";
import { isValidTimeZone } from "./time";

export const ZONE_UNAVAILABLE =
  "The clinic time zone could not be loaded, so times cannot be shown or entered. " +
  "Refresh to try again.";

/**
 * The clinic's IANA time zone, fetched fresh for screens that enter or filter by time.
 * There is deliberately no fallback: if the zone cannot be loaded (or is unknown to this
 * browser) the screen refuses to render its time inputs, so nothing is converted with a
 * wrong zone.
 */
export async function loadClinicTimeZone(ctx: Context): Promise<string> {
  let timeZone: string;
  try {
    timeZone = (await ctx.api.request("get", "/v1/admin/clinic")).timezone;
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) throw error; // Session ended.
    throw new UserError(ZONE_UNAVAILABLE);
  }
  if (!isValidTimeZone(timeZone)) throw new UserError(ZONE_UNAVAILABLE);
  ctx.setTimeZone(timeZone);
  return timeZone;
}
