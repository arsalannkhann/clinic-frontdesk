import type { Context, Screen } from "../context";
import { h, mount } from "../dom";
import { calendarAccountLabel } from "../labels";
import { canWrite } from "../session";
import { formatInZone } from "../time";
import { badge, button, confirmAction, loading, run, section, short, statusLine, table } from "../ui";

const GOOGLE_HOSTS = new Set(["accounts.google.com"]);

/** The backend's authorization URL, accepted only as https to Google's consent host. */
export function consentUrl(value: string): URL | null {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && GOOGLE_HOSTS.has(url.hostname) ? url : null;
  } catch {
    return null;
  }
}

async function load(ctx: Context): Promise<HTMLElement> {
  const tz = ctx.timeZone();
  const writer = canWrite(ctx.user);
  const accounts = await ctx.api.request("get", "/v1/admin/calendar/accounts");
  const hasActive = accounts.some((a) => a.status === "active");
  const status = statusLine();
  const next = h("div");

  const start = (replace: boolean) => (event: MouseEvent) => {
    if (replace) {
      const ok = confirmAction(
        "Replace the connected Google account? Once the new account connects, the current one " +
          "is retired (its token is forgotten, not revoked at Google) and doctors' calendar " +
          "mappings must point at calendars the new account can access.",
      );
      if (!ok) return;
    }
    void run(
      status,
      async () => {
        const { authorization_url } = await ctx.api.request(
          "post",
          "/v1/admin/calendar/google/start",
          { query: { replace } },
        );
        const url = consentUrl(authorization_url);
        if (!url) throw new Error("Unexpected authorization URL");
        // A new tab keeps this tab's in-memory session; the link is valid for ten minutes.
        mount(
          next,
          h(
            "p",
            {},
            h(
              "a",
              {
                class: "btn btn-primary",
                attrs: { href: url.href, target: "_blank", rel: "noopener noreferrer" },
              },
              "Continue to Google (opens a new tab)",
            ),
            " Then return here and refresh the list.",
          ),
        );
        return "Authorization started.";
      },
      { control: event.currentTarget as HTMLButtonElement },
    );
  };

  const rows = accounts.map((a) => [
    a.provider,
    badge(calendarAccountLabel(a.status)),
    a.scopes.join(" "),
    formatInZone(a.created_at, tz),
    formatInZone(a.revoked_at, tz),
    short(a.id),
    writer && a.status !== "revoked"
      ? button(
          "Disconnect",
          (event) => {
            const message =
              "Disconnect this calendar account? It stops being used immediately, its token is " +
              "revoked at Google by a background job, and doctors mapped to it sync no further.";
            if (!confirmAction(message)) return;
            void run(
              status,
              async () => {
                await ctx.api.request("delete", "/v1/admin/calendar/google/{account_id}", {
                  params: { account_id: a.id },
                });
                ctx.refresh();
              },
              { control: event.currentTarget as HTMLButtonElement },
            );
          },
          { kind: "danger" },
        )
      : null,
  ]);

  return section(
    "Calendar connection",
    table(
      ["Provider", "Status", "Scopes", "Connected", "Disconnected", "Id", ""],
      rows,
      "No calendar account connected.",
    ),
    writer
      ? h(
          "div",
          { class: "actions" },
          hasActive
            ? button("Replace Google account…", start(true), { kind: "danger" })
            : button("Connect Google Calendar", start(false), { kind: "primary" }),
          button("Refresh", () => ctx.refresh()),
        )
      : null,
    hasActive && writer
      ? h(
          "p",
          { class: "status status-error" },
          "Warning: replacing the account retires the current one after the new one connects.",
        )
      : null,
    status,
    next,
  );
}

export const calendarScreen: Screen = {
  path: "calendar",
  title: "Calendar",
  visible: () => true,
  render(ctx) {
    const root = h("div");
    loading(root, () => load(ctx));
    return root;
  },
};
