import type { Schemas } from "../api/client";
import type { Context, Screen } from "../context";
import { h } from "../dom";
import { canWrite, isOwner } from "../session";
import { timeZones } from "../time";
import {
  button,
  checkbox,
  confirmAction,
  field,
  input,
  loading,
  run,
  section,
  showStatus,
  statusLine,
  yesNo,
} from "../ui";

type Clinic = Schemas["ClinicConfig"];
type ClinicUpdate = Schemas["ClinicUpdate"];

export const OUTBOUND_OFF_WARNING =
  "Turn off outbound contact? Contacts due while outbound is off are suppressed permanently " +
  "(turning it back on does not resend them).";
export const BUDGET_ZERO_WARNING =
  "Set the daily contact budget to 0? This stops all outbound confirmations and reminders; " +
  "contacts that come due are suppressed.";

/** The confirmations a clinic update needs before it is sent, in order. */
export function confirmationsFor(update: ClinicUpdate, clinic: Clinic): string[] {
  const messages: string[] = [];
  if (update.outbound_enabled === true) {
    messages.push(
      "Enable outbound contact? Consented patients will receive confirmations and reminders.",
    );
  }
  if (update.outbound_enabled === false) messages.push(OUTBOUND_OFF_WARNING);
  if (update.daily_contact_budget === 0) messages.push(BUDGET_ZERO_WARNING);
  if (update.timezone !== undefined) {
    messages.push(
      `Change the clinic time zone from ${clinic.timezone} to ${update.timezone}? Working ` +
        "hours, quiet hours and reminder times are interpreted in the new zone.",
    );
  }
  if (
    (update.emergency_text !== undefined || update.handoff_text !== undefined) &&
    clinic.scripts_approved
  ) {
    messages.push("Changing a script clears its approval. Continue?");
  }
  return messages;
}

function form(ctx: Context, clinic: Clinic): HTMLElement {
  const writer = canWrite(ctx.user);
  const owner = isOwner(ctx.user);
  const status = statusLine();
  const zones = timeZones();

  const name = input({ value: clinic.name, required: true, maxlength: 120 });
  const timezone = input({ value: clinic.timezone, required: true, list: "tz-list" });
  const lead = input({ type: "number", min: 0, max: 10080, value: clinic.lead_time_minutes });
  const horizon = input({ type: "number", min: 1, max: 365, value: clinic.horizon_days });
  const outbound = checkbox(clinic.outbound_enabled);
  const quietStart = input({ type: "time", value: clinic.quiet_hours_start, required: true });
  const quietEnd = input({ type: "time", value: clinic.quiet_hours_end, required: true });
  const reminderLead = input({
    type: "number",
    min: 60,
    max: 10080,
    value: clinic.reminder_lead_minutes,
  });
  const budget = input({ type: "number", min: 0, max: 100000, value: clinic.daily_contact_budget });
  const emergency = h("textarea", { attrs: { maxlength: 1000, rows: 3 } }, clinic.emergency_text);
  const handoff = h("textarea", { attrs: { maxlength: 1000, rows: 3 } }, clinic.handoff_text);

  const changes = (): ClinicUpdate => {
    const update: ClinicUpdate = {};
    if (name.value.trim() !== clinic.name) update.name = name.value.trim();
    if (timezone.value !== clinic.timezone) update.timezone = timezone.value;
    if (lead.valueAsNumber !== clinic.lead_time_minutes) update.lead_time_minutes = lead.valueAsNumber;
    if (horizon.valueAsNumber !== clinic.horizon_days) update.horizon_days = horizon.valueAsNumber;
    if (outbound.checked !== clinic.outbound_enabled) update.outbound_enabled = outbound.checked;
    if (quietStart.value !== clinic.quiet_hours_start) update.quiet_hours_start = quietStart.value;
    if (quietEnd.value !== clinic.quiet_hours_end) update.quiet_hours_end = quietEnd.value;
    if (reminderLead.valueAsNumber !== clinic.reminder_lead_minutes) {
      update.reminder_lead_minutes = reminderLead.valueAsNumber;
    }
    if (budget.valueAsNumber !== clinic.daily_contact_budget) {
      update.daily_contact_budget = budget.valueAsNumber;
    }
    if (emergency.value.trim() !== clinic.emergency_text) update.emergency_text = emergency.value.trim();
    if (handoff.value.trim() !== clinic.handoff_text) update.handoff_text = handoff.value.trim();
    return update;
  };

  const save = button(
    "Save settings",
    () => {
      const update = changes();
      if (!Object.keys(update).length) return showStatus(status, "info", "No changes to save.");
      for (const message of confirmationsFor(update, clinic)) {
        if (!confirmAction(message)) return;
      }
      void run(
        status,
        async () => {
          await ctx.api.request("patch", "/v1/admin/clinic", { body: update });
          ctx.refresh();
        },
        { success: "Settings saved.", control: save },
      );
    },
    { kind: "primary", disabled: !writer },
  );

  const scriptsDirty = () =>
    emergency.value.trim() !== clinic.emergency_text || handoff.value.trim() !== clinic.handoff_text;
  const approve = button(
    "Approve scripts",
    () => {
      if (scriptsDirty()) {
        return showStatus(status, "error", "Save the script changes before approving them.");
      }
      if (!confirmAction("Approve the saved emergency and handoff scripts for live calls?")) return;
      void run(
        status,
        async () => {
          await ctx.api.request("patch", "/v1/admin/clinic", { body: { scripts_approved: true } });
          ctx.refresh();
        },
        { success: "Scripts approved.", control: approve },
      );
    },
    { disabled: !owner || clinic.scripts_approved, title: owner ? undefined : "Owner only" },
  );

  const testCalls = button(
    clinic.test_calls_enabled ? "Disable test calls" : "Enable test calls",
    () => {
      const enable = !clinic.test_calls_enabled;
      const message = enable
        ? "Allow booking-changing tools on verified (not live) provisioning for owner test calls?"
        : "Disable test calls?";
      if (!confirmAction(message)) return;
      void run(
        status,
        async () => {
          await ctx.api.request("patch", "/v1/admin/clinic", {
            body: { test_calls_enabled: enable },
          });
          ctx.refresh();
        },
        { control: testCalls },
      );
    },
    { disabled: !owner, title: owner ? undefined : "Owner only" },
  );

  const fields = h(
    "fieldset",
    { attrs: { disabled: !writer } },
    h("legend", {}, "Clinic"),
    field("Name", name),
    field("Timezone (IANA)", timezone, "For example Asia/Kolkata"),
    h("datalist", { attrs: { id: "tz-list" } }, zones.map((z) => h("option", { attrs: { value: z } }))),
    field("Booking lead time (minutes)", lead),
    field("Booking horizon (days)", horizon),
    h("h3", {}, "Outbound contact"),
    field("Outbound contact enabled", outbound, "Off by default. Applies to consented patients only."),
    field("Quiet hours start (clinic time)", quietStart),
    field("Quiet hours end (clinic time)", quietEnd),
    field("Reminder lead (minutes before)", reminderLead),
    field("Daily contact budget", budget),
    h("h3", {}, "Scripts"),
    field("Emergency script", emergency, "No braces, percent signs or line breaks."),
    field("Handoff script", handoff),
  );

  return h(
    "div",
    {},
    section("Clinic settings", fields, h("div", { class: "actions" }, save), status),
    section(
      "Approvals (owner)",
      h("p", {}, `Scripts approved: ${yesNo(clinic.scripts_approved)}`),
      h("p", {}, `Test calls enabled: ${yesNo(clinic.test_calls_enabled)}`),
      h("div", { class: "actions" }, approve, testCalls),
      owner ? null : h("p", { class: "muted" }, "Only the clinic owner can approve or enable these."),
    ),
  );
}

export const clinicScreen: Screen = {
  path: "clinic",
  title: "Clinic settings",
  visible: () => true,
  render(ctx) {
    const root = h("div");
    loading(root, async () => {
      const clinic = await ctx.api.request("get", "/v1/admin/clinic");
      ctx.setTimeZone(clinic.timezone);
      return form(ctx, clinic);
    });
    return root;
  },
};
