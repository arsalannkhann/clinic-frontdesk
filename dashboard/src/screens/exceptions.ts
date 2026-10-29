import type { Context, Screen } from "../context";
import { h, mount } from "../dom";
import { canWrite } from "../session";
import { formatInZone, zonedToIso } from "../time";
import {
  button,
  confirmAction,
  field,
  input,
  loading,
  run,
  section,
  select,
  showStatus,
  statusLine,
  table,
} from "../ui";

const KINDS = [
  ["holiday", "Holiday"],
  ["leave", "Leave"],
  ["break", "Break"],
] as const;

async function load(ctx: Context): Promise<HTMLElement> {
  const tz = ctx.timeZone();
  const [doctors, exceptions] = await Promise.all([
    ctx.api.request("get", "/v1/admin/doctors"),
    ctx.api.request("get", "/v1/admin/exceptions", {
      query: { from: new Date(Date.now() - 7 * 86_400_000).toISOString(), limit: 500 },
    }),
  ]);
  const writer = canWrite(ctx.user);
  const doctorName = new Map(doctors.map((d) => [d.id, d.name]));
  const status = statusLine();
  const result = h("div");

  const rows = exceptions.map((e) => [
    e.doctor_id ? (doctorName.get(e.doctor_id) ?? "Unknown doctor") : "All doctors",
    e.kind,
    formatInZone(e.starts_at, tz),
    formatInZone(e.ends_at, tz),
    e.label,
    writer
      ? button(
          "Delete",
          (event) => {
            if (!confirmAction("Delete this schedule exception? The time becomes bookable.")) return;
            void run(
              status,
              async () => {
                await ctx.api.request("delete", "/v1/admin/exceptions/{exception_id}", {
                  params: { exception_id: e.id },
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

  const doctor = select([["", "All doctors"], ...doctors.map((d) => [d.id, d.name] as const)], "");
  const kind = select(KINDS, "holiday");
  const starts = input({ type: "datetime-local", required: true });
  const ends = input({ type: "datetime-local", required: true });
  const label = input({ maxlength: 80, placeholder: "e.g. Diwali (no patient details)" });
  const create = button(
    "Add exception",
    () => {
      if (!starts.value || !ends.value) return showStatus(status, "error", "Enter start and end.");
      void run(
        status,
        async () => {
          const created = await ctx.api.request("post", "/v1/admin/exceptions", {
            body: {
              doctor_id: doctor.value || null,
              kind: kind.value as (typeof KINDS)[number][0],
              starts_at: zonedToIso(starts.value, tz),
              ends_at: zonedToIso(ends.value, tz),
              label: label.value.trim(),
            },
          });
          mount(
            result,
            created.conflict_count
              ? h(
                  "p",
                  { class: "status status-error" },
                  `${created.conflict_count} active appointment(s) overlap this exception. They were` +
                    " not cancelled; review them on the Appointments screen.",
                )
              : h("p", { class: "status status-ok" }, "No appointments overlap this exception."),
          );
        },
        { success: "Exception added. Refresh to see it in the list.", control: create },
      );
    },
    { kind: "primary" },
  );

  return h(
    "div",
    {},
    section(
      "Schedule exceptions",
      h("p", { class: "muted" }, `Times in clinic time (${tz}). Showing from 7 days ago.`),
      table(["Doctor", "Kind", "Starts", "Ends", "Label", ""], rows, "No exceptions."),
      writer
        ? h(
            "div",
            { class: "inline-form" },
            field("Doctor", doctor),
            field("Kind", kind),
            field("Starts", starts),
            field("Ends", ends),
            field("Label", label),
            create,
            button("Refresh", () => ctx.refresh()),
          )
        : null,
      status,
      result,
    ),
  );
}

export const exceptionsScreen: Screen = {
  path: "exceptions",
  title: "Schedule exceptions",
  visible: () => true,
  render(ctx) {
    const root = h("div");
    loading(root, () => load(ctx));
    return root;
  },
};
