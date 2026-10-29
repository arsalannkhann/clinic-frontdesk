import type { Schemas } from "../api/client";
import type { Context, Screen } from "../context";
import { h, mount } from "../dom";
import { canWrite } from "../session";
import {
  button,
  checkbox,
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
  yesNo,
} from "../ui";

type Doctor = Schemas["DoctorOut"];
type Service = Schemas["ServiceOut"];
type Hours = Schemas["HoursWindow"];
type Account = Schemas["CalendarAccountOut"];

const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

function doctorEditor(ctx: Context, doctor: Doctor, accounts: Account[]): HTMLElement {
  const status = statusLine();
  const name = input({ value: doctor.name, maxlength: 120, required: true });
  const active = checkbox(doctor.active);
  const booking = input({ value: doctor.booking_calendar_id ?? "", maxlength: 255 });
  const busy = input({ value: doctor.busy_calendar_ids.join(", ") });
  const account = select(
    [
      ["", "Clinic's only active account"],
      ...accounts
        .filter((a) => a.status === "active")
        .map((a) => [a.id, `${a.provider} ${a.id.slice(0, 8)}`] as const),
    ],
    doctor.calendar_account_id ?? "",
  );
  const save = button(
    "Save doctor",
    () => {
      const body: Schemas["DoctorUpdate"] = {};
      if (name.value.trim() !== doctor.name) body.name = name.value.trim();
      if (active.checked !== doctor.active) body.active = active.checked;
      const bookingId = booking.value.trim() || null;
      if (bookingId !== doctor.booking_calendar_id) body.booking_calendar_id = bookingId;
      const busyIds = busy.value
        .split(",")
        .map((part) => part.trim())
        .filter(Boolean);
      if (busyIds.join(",") !== doctor.busy_calendar_ids.join(",")) body.busy_calendar_ids = busyIds;
      const accountId = account.value || null;
      if (accountId !== doctor.calendar_account_id) body.calendar_account_id = accountId;
      if (!Object.keys(body).length) return showStatus(status, "info", "No changes to save.");
      if (body.active === false && !confirmAction(`Deactivate ${doctor.name}? No new bookings.`)) {
        return;
      }
      void run(
        status,
        async () => {
          await ctx.api.request("patch", "/v1/admin/doctors/{doctor_id}", {
            params: { doctor_id: doctor.id },
            body,
          });
          ctx.refresh();
        },
        { success: "Doctor saved.", control: save },
      );
    },
    { kind: "primary" },
  );
  return h(
    "div",
    { class: "editor" },
    field("Name", name),
    field("Active", active),
    field("Booking calendar id", booking, "Empty unsets the mapping."),
    field("Busy calendar ids", busy, "Comma-separated, at most 10."),
    field("Calendar account", account),
    h("div", { class: "actions" }, save),
    status,
  );
}

function hoursEditor(ctx: Context, doctor: Doctor, hours: Hours[]): HTMLElement {
  const writer = canWrite(ctx.user);
  const status = statusLine();
  const rows = h("div", { class: "hours-rows" });
  const addRow = (window: Hours) => {
    const weekday = select(
      WEEKDAYS.map((day, index) => [String(index), day] as const),
      String(window.weekday),
    );
    const opens = input({ type: "time", value: window.opens, required: true });
    const closes = input({ type: "time", value: window.closes, required: true });
    const row: HTMLDivElement = h(
      "div",
      { class: "hours-row" },
      weekday,
      opens,
      closes,
      writer ? button("Remove", () => row.remove()) : null,
    );
    rows.append(row);
  };
  hours.forEach(addRow);
  const save = button(
    "Replace hours",
    () => {
      const windows: Hours[] = [...rows.querySelectorAll(".hours-row")].map((row) => {
        const [weekday, opens, closes] = [...row.querySelectorAll("select, input")] as [
          HTMLSelectElement,
          HTMLInputElement,
          HTMLInputElement,
        ];
        return { weekday: Number(weekday.value), opens: opens.value, closes: closes.value };
      });
      const message = `Replace all working hours for ${doctor.name} with ${windows.length} window(s)?`;
      if (!confirmAction(message)) return;
      void run(
        status,
        async () => {
          await ctx.api.request("put", "/v1/admin/doctors/{doctor_id}/hours", {
            params: { doctor_id: doctor.id },
            body: windows,
          });
        },
        { success: "Hours replaced.", control: save },
      );
    },
    { kind: "primary" },
  );
  return h(
    "fieldset",
    { attrs: { disabled: !writer } },
    h("legend", {}, `Working hours: ${doctor.name} (clinic time)`),
    rows,
    h(
      "div",
      { class: "actions" },
      button("Add window", () => addRow({ weekday: 0, opens: "09:00", closes: "17:00" })),
      save,
    ),
    status,
  );
}

function services(ctx: Context, doctors: Doctor[], list: Service[]): HTMLElement {
  const writer = canWrite(ctx.user);
  const doctorName = new Map(doctors.map((d) => [d.id, d.name]));
  const status = statusLine();
  const toggle = (service: Service) =>
    button(service.active ? "Deactivate" : "Activate", (event) => {
      if (service.active && !confirmAction(`Deactivate ${service.name}? No new bookings.`)) return;
      void run(
        status,
        async () => {
          await ctx.api.request("patch", "/v1/admin/services/{service_id}", {
            params: { service_id: service.id },
            body: { active: !service.active },
          });
          ctx.refresh();
        },
        { control: event.currentTarget as HTMLButtonElement },
      );
    });
  const rows = list.map((s) => [
    s.name,
    doctorName.get(s.doctor_id) ?? "Unknown doctor",
    `${s.duration_minutes} min`,
    `${s.buffer_after_minutes} min`,
    yesNo(s.active),
    writer ? toggle(s) : null,
  ]);

  const doctor = select(
    doctors.map((d) => [d.id, d.name] as const),
    doctors[0]?.id ?? "",
  );
  const name = input({ maxlength: 120, required: true });
  const duration = input({ type: "number", min: 5, max: 240, value: 30 });
  const buffer = input({ type: "number", min: 0, max: 120, value: 0 });
  const create = button(
    "Add service",
    () =>
      void run(
        status,
        async () => {
          await ctx.api.request("post", "/v1/admin/services", {
            body: {
              doctor_id: doctor.value,
              name: name.value.trim(),
              duration_minutes: duration.valueAsNumber,
              buffer_after_minutes: buffer.valueAsNumber,
            },
          });
          ctx.refresh();
        },
        { success: "Service added.", control: create },
      ),
    { kind: "primary" },
  );
  return section(
    "Services",
    table(["Name", "Doctor", "Duration", "Buffer after", "Active", ""], rows, "No services yet."),
    writer && doctors.length
      ? h(
          "div",
          { class: "inline-form" },
          field("Doctor", doctor),
          field("Name", name),
          field("Duration (min)", duration),
          field("Buffer after (min)", buffer),
          create,
        )
      : null,
    status,
  );
}

async function load(ctx: Context): Promise<HTMLElement> {
  const [doctors, serviceList, accounts] = await Promise.all([
    ctx.api.request("get", "/v1/admin/doctors"),
    ctx.api.request("get", "/v1/admin/services"),
    ctx.api.request("get", "/v1/admin/calendar/accounts"),
  ]);
  const writer = canWrite(ctx.user);
  const status = statusLine();
  const detail = h("div");

  const showHours = (doctor: Doctor) =>
    loading(detail, async () =>
      hoursEditor(
        ctx,
        doctor,
        await ctx.api.request("get", "/v1/admin/doctors/{doctor_id}/hours", {
          params: { doctor_id: doctor.id },
        }),
      ),
    );
  const rows = doctors.map((d) => [
    d.name,
    yesNo(d.active),
    d.booking_calendar_id ?? "Not mapped",
    d.busy_calendar_ids.length ? d.busy_calendar_ids.join(", ") : "None",
    h(
      "div",
      { class: "actions" },
      button("Hours", () => showHours(d)),
      writer ? button("Edit", () => mount(detail, doctorEditor(ctx, d, accounts))) : null,
    ),
  ]);

  const name = input({ maxlength: 120, required: true, placeholder: "Doctor name" });
  const create = button(
    "Add doctor",
    () =>
      void run(
        status,
        async () => {
          await ctx.api.request("post", "/v1/admin/doctors", { body: { name: name.value.trim() } });
          ctx.refresh();
        },
        { success: "Doctor added.", control: create },
      ),
    { kind: "primary" },
  );

  return h(
    "div",
    {},
    section(
      "Doctors",
      table(["Name", "Active", "Booking calendar", "Busy calendars", ""], rows, "No doctors yet."),
      writer ? h("div", { class: "inline-form" }, field("New doctor", name), create) : null,
      status,
      detail,
    ),
    services(ctx, doctors, serviceList),
  );
}

export const scheduleScreen: Screen = {
  path: "schedule",
  title: "Doctors, services & hours",
  visible: () => true,
  render(ctx) {
    const root = h("div");
    loading(root, () => load(ctx));
    return root;
  },
};
