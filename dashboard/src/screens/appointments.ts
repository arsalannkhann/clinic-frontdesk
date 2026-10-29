import type { Schemas } from "../api/client";
import type { Context, Screen } from "../context";
import { h, mount } from "../dom";
import { APPOINTMENT_STATUSES, appointmentStatusLabel, syncLabel } from "../labels";
import { canWrite } from "../session";
import { formatInZone, zonedToIso } from "../time";
import {
  badge,
  button,
  confirmAction,
  field,
  input,
  loading,
  run,
  section,
  select,
  statusLine,
  table,
} from "../ui";

type Appointment = Schemas["StaffAppointmentOut"];
type AppointmentState = (typeof APPOINTMENT_STATUSES)[number];
type OptOut = Schemas["StaffOptOut"];

// Filters survive re-renders within this page load (memory only, no URL).
const filters: { from: string; to: string; status: AppointmentState | ""; limit: number } = {
  from: "",
  to: "",
  status: "",
  limit: 100,
};

interface Names {
  doctor: Map<string, string>;
  service: Map<string, string>;
}

function optOutPanel(ctx: Context, appointment: Appointment): HTMLElement {
  const status = statusLine();
  const purpose = select<NonNullable<OptOut["purpose"]>>(
    [
      ["all", "All contact purposes"],
      ["booking_confirmation", "Booking confirmations"],
      ["reminder", "Reminders"],
    ],
    "all",
  );
  const channel = select<NonNullable<OptOut["channel"]>>(
    [
      ["all", "All channels"],
      ["call", "Calls"],
      ["message", "Messages"],
    ],
    "all",
  );
  const body = (): OptOut => ({
    purpose: purpose.value as OptOut["purpose"],
    channel: channel.value as OptOut["channel"],
  });
  const optOut = button(
    "Opt out",
    () => {
      if (!confirmAction("Opt this patient out? Unsent contacts that match are suppressed.")) return;
      void run(
        status,
        async () => {
          const r = await ctx.api.request(
            "post",
            "/v1/admin/appointments/{appointment_id}/opt-out",
            { params: { appointment_id: appointment.id }, body: body() },
          );
          return `Opt-out recorded; ${r.suppressed} unsent contact(s) suppressed.`;
        },
        { control: optOut },
      );
    },
    { kind: "danger" },
  );
  const remove = button("Remove opt-out", () => {
    const message = "Remove this exact opt-out? Contacts already suppressed stay suppressed.";
    if (!confirmAction(message)) return;
    void run(
      status,
      async () => {
        const r = await ctx.api.request(
          "post",
          "/v1/admin/appointments/{appointment_id}/opt-out/remove",
          { params: { appointment_id: appointment.id }, body: body() },
        );
        return r.removed ? "Opt-out removed." : "No matching opt-out was found.";
      },
      { control: remove },
    );
  });
  return section(
    `Contact preferences: booking ${appointment.booking_reference}`,
    h("div", { class: "inline-form" }, field("Purpose", purpose), field("Channel", channel)),
    h("div", { class: "actions" }, optOut, remove),
    status,
  );
}

function actions(ctx: Context, a: Appointment, status: HTMLElement, panel: HTMLElement) {
  if (!canWrite(ctx.user)) return null;
  const act = (
    label: string,
    message: string,
    request: () => Promise<unknown>,
    kind: "danger" | "plain" = "plain",
  ) =>
    button(
      label,
      (event) => {
        if (!confirmAction(message)) return;
        void run(
          status,
          async () => {
            await request();
            ctx.refresh();
          },
          { control: event.currentTarget as HTMLButtonElement },
        );
      },
      { kind },
    );
  const params = { appointment_id: a.id };
  const resolve = (action: "requeue" | "cancel") => () =>
    ctx.api.request("post", "/v1/admin/appointments/{appointment_id}/resolve", {
      params,
      body: { action },
    });
  const controls: HTMLElement[] = [];
  if (a.status === "needs_review") {
    controls.push(
      act("Requeue sync", `Retry the calendar sync for ${a.booking_reference}?`, resolve("requeue")),
      act(
        "Resolve: cancel",
        `Cancel ${a.booking_reference}? The patient is not told automatically.`,
        resolve("cancel"),
        "danger",
      ),
    );
  } else if (a.status === "pending_sync" || a.status === "confirmed") {
    controls.push(
      act(
        "Cancel",
        `Cancel ${a.booking_reference}? The slot is freed under the sync policy.`,
        () =>
          ctx.api.request("post", "/v1/admin/appointments/{appointment_id}/cancel", { params }),
        "danger",
      ),
    );
  }
  controls.push(button("Opt-out…", () => mount(panel, optOutPanel(ctx, a))));
  return h("div", { class: "actions" }, controls);
}

function appointmentTable(
  ctx: Context,
  list: Appointment[],
  names: Names,
  empty: string,
): HTMLElement {
  const tz = ctx.timeZone();
  const status = statusLine();
  const panel = h("div");
  const rows = list.map((a) => [
    formatInZone(a.starts_at, tz),
    a.booking_reference,
    names.doctor.get(a.doctor_id) ?? "Unknown doctor",
    names.service.get(a.service_id) ?? "Unknown service",
    badge(appointmentStatusLabel(a.status)),
    badge(syncLabel(a.status)),
    a.patient_name ?? "",
    a.phone_last2 ? `••${a.phone_last2}` : "",
    actions(ctx, a, status, panel),
  ]);
  return h(
    "div",
    {},
    table(
      ["Starts", "Reference", "Doctor", "Service", "Status", "Calendar sync", "Patient", "Phone", ""],
      rows,
      empty,
    ),
    status,
    panel,
  );
}

async function load(ctx: Context): Promise<HTMLElement> {
  const tz = ctx.timeZone();
  const query = {
    from: filters.from ? zonedToIso(filters.from, tz) : null,
    to: filters.to ? zonedToIso(filters.to, tz) : null,
    status: filters.status || null,
    limit: filters.limit,
  };
  const [doctors, services, list, review] = await Promise.all([
    ctx.api.request("get", "/v1/admin/doctors"),
    ctx.api.request("get", "/v1/admin/services"),
    ctx.api.request("get", "/v1/admin/appointments", { query }),
    ctx.api.request("get", "/v1/admin/appointments", {
      query: { status: "needs_review", limit: 100 },
    }),
  ]);
  const names: Names = {
    doctor: new Map(doctors.map((d) => [d.id, d.name])),
    service: new Map(services.map((s) => [s.id, s.name])),
  };

  const from = input({ type: "date", value: filters.from });
  const to = input({ type: "date", value: filters.to });
  const state = select<AppointmentState | "">(
    [["", "Any status"], ...APPOINTMENT_STATUSES.map((s) => [s, appointmentStatusLabel(s).text] as const)],
    filters.status,
  );
  const limit = input({ type: "number", min: 1, max: 500, value: filters.limit });
  const apply = button(
    "Apply filters",
    () => {
      filters.from = from.value;
      filters.to = to.value;
      filters.status = state.value as AppointmentState | "";
      filters.limit = Math.min(500, Math.max(1, limit.valueAsNumber || 100));
      ctx.refresh();
    },
    { kind: "primary" },
  );

  return h(
    "div",
    {},
    section(
      `Needs review (${review.length})`,
      h(
        "p",
        { class: "muted" },
        "Calendar sync failed for these bookings. Requeue the sync or cancel. Never tell a " +
          "patient they are confirmed until the status shows Confirmed.",
      ),
      appointmentTable(ctx, review, names, "Nothing needs review."),
    ),
    section(
      "Appointments",
      h(
        "div",
        { class: "inline-form" },
        field(`From (clinic date, ${tz})`, from),
        field("To (exclusive)", to),
        field("Status", state),
        field("Limit", limit),
        apply,
      ),
      appointmentTable(ctx, list, names, "No appointments match."),
    ),
  );
}

export const appointmentsScreen: Screen = {
  path: "appointments",
  title: "Appointments",
  visible: () => true,
  render(ctx) {
    const root = h("div");
    loading(root, () => load(ctx));
    return root;
  },
};
