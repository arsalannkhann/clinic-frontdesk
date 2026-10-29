import type { ApiClient } from "./api/client";
import type { StaffUser } from "./session";

/** What a screen receives. The backend remains the authority on every permission. */
export interface Context {
  api: ApiClient;
  user: StaffUser;
  /** The clinic's IANA timezone (falls back to UTC until loaded). */
  timeZone: () => string;
  setTimeZone: (timeZone: string) => void;
  /** Re-render the current screen. */
  refresh: () => void;
}

export interface Screen {
  path: string;
  title: string;
  visible: (user: StaffUser) => boolean;
  render: (ctx: Context) => HTMLElement;
}
