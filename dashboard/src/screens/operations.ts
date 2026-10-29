/** Reminders and outbox: owner/admin views (the backend refuses viewers). */
import type { QueryOf } from "../api/client";
import type { Context, Screen } from "../context";
import { h } from "../dom";
import { REMINDER_STATUSES, outboxStatusLabel, reminderStatusLabel } from "../labels";
import { canWrite } from "../session";
import { formatInZone } from "../time";
import {
  badge,
  button,
  confirmAction,
  field,
  loading,
  run,
  section,
  select,
  short,
  statusLine,
  table,
} from "../ui";

type ReminderStatus = NonNullable<QueryOf<"/v1/admin/reminders", "get">["status"]>;
type OutboxStatus = NonNullable<QueryOf<"/v1/admin/outbox", "get">["status"]>;

const reminderFilter: { status: ReminderStatus | "" } = { status: "" };
const outboxFilter: { status: OutboxStatus } = { status: "dead" };

async function reminders(ctx: Context): Promise<HTMLElement> {
  const tz = ctx.timeZone();
  const list = await ctx.api.request("get", "/v1/admin/reminders", {
    query: { status: reminderFilter.status || null, limit: 200 },
  });
  const filter = select<ReminderStatus | "">(
    [["", "Any status"], ...REMINDER_STATUSES.map((s) => [s, reminderStatusLabel(s).text] as const)],
    reminderFilter.status,
  );
  filter.addEventListener("change", () => {
    reminderFilter.status = filter.value as ReminderStatus | "";
    ctx.refresh();
  });
  const rows = list.map((r) => [
    formatInZone(r.scheduled_for, tz),
    r.purpose,
    r.channel,
    badge(reminderStatusLabel(r.status)),
    String(r.attempts),
    r.last_error ?? "",
    r.stop_outcome ?? "",
    formatInZone(r.sent_at, tz),
    short(r.appointment_id),
  ]);
  return section(
    "Reminders and confirmations",
    h(
      "p",
      { class: "muted" },
      "Planned and attempted patient contacts. No patient details or message text are shown.",
    ),
    h("div", { class: "inline-form" }, field("Status", filter)),
    table(
      ["Scheduled", "Purpose", "Channel", "Status", "Attempts", "Last error", "Stop", "Sent", "Appt"],
      rows,
      "No reminders match.",
    ),
  );
}

async function outbox(ctx: Context): Promise<HTMLElement> {
  const tz = ctx.timeZone();
  const list = await ctx.api.request("get", "/v1/admin/outbox", {
    query: { status: outboxFilter.status, limit: 200 },
  });
  const status = statusLine();
  const filter = select<OutboxStatus>(
    [
      ["dead", "Dead"],
      ["pending", "Pending"],
      ["processing", "Processing"],
    ],
    outboxFilter.status,
  );
  filter.addEventListener("change", () => {
    outboxFilter.status = filter.value as OutboxStatus;
    ctx.refresh();
  });
  const rows = list.map((job) => [
    job.kind,
    badge(outboxStatusLabel(job.status)),
    `${job.attempts}/${job.max_attempts}`,
    job.last_error ?? "",
    short(job.appointment_id),
    formatInZone(job.updated_at, tz),
    job.status === "dead"
      ? button("Requeue", (event) => {
          if (!confirmAction(`Requeue this ${job.kind} job? It will run again.`)) return;
          void run(
            status,
            async () => {
              await ctx.api.request("post", "/v1/admin/outbox/{job_id}/requeue", {
                params: { job_id: job.id },
              });
              ctx.refresh();
            },
            { control: event.currentTarget as HTMLButtonElement },
          );
        })
      : null,
  ]);
  return section(
    "Outbox",
    h("p", { class: "muted" }, "Durable jobs. Dead jobs exhausted their retries and need review."),
    h("div", { class: "inline-form" }, field("Status", filter)),
    table(["Kind", "Status", "Attempts", "Last error", "Appt", "Updated", ""], rows, "No jobs."),
    status,
  );
}

export const remindersScreen: Screen = {
  path: "reminders",
  title: "Reminders",
  visible: canWrite,
  render(ctx) {
    const root = h("div");
    loading(root, () => reminders(ctx));
    return root;
  },
};

export const outboxScreen: Screen = {
  path: "outbox",
  title: "Outbox",
  visible: canWrite,
  render(ctx) {
    const root = h("div");
    loading(root, () => outbox(ctx));
    return root;
  },
};
