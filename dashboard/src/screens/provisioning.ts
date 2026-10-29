import type { Schemas } from "../api/client";
import type { Context, Screen } from "../context";
import { h } from "../dom";
import { provisioningStateLabel } from "../labels";
import { isOwner } from "../session";
import { formatInZone } from "../time";
import {
  badge,
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
  table,
  yesNo,
} from "../ui";

type Provisioning = Schemas["ProvisioningOut"];

// Mirrors of backend rules, used only to hide controls; the backend re-checks all of them.
const ADVANCEABLE = new Set(["draft", "calendar_connected", "agent_created", "number_assigned"]);
const NUMBER_SELECTABLE = new Set(["draft", "calendar_connected", "agent_created"]);
const RESETTABLE = new Set(["failed", "manual_review"]);
const STRAY_AGENT_ERRORS = new Set(["create_uncertain", "lease_lost"]);
const PHONE_NUMBER_ID = /^[A-Za-z0-9-]{1,64}$/;

function ownerActions(ctx: Context, p: Provisioning, status: HTMLElement): HTMLElement {
  const post = (
    label: string,
    message: string | null,
    request: () => Promise<unknown>,
    kind: "primary" | "danger" | "plain" = "plain",
  ) => {
    const control: HTMLButtonElement = button(
      label,
      () => {
        if (message && !confirmAction(message)) return;
        void run(
          status,
          async () => {
            await request();
            ctx.refresh();
          },
          { control },
        );
      },
      { kind },
    );
    return control;
  };
  const parts: HTMLElement[] = [];
  if (ADVANCEABLE.has(p.state) && !p.step_pending) {
    parts.push(
      post(
        "Run next step",
        "Run the next provisioning step? It may create or update the clinic's Bolna agent.",
        () => ctx.api.request("post", "/v1/admin/provisioning/advance"),
        "primary",
      ),
    );
  }
  if (NUMBER_SELECTABLE.has(p.state) && !p.step_pending) {
    const numberId = input({
      value: p.phone_number_id ?? "",
      maxlength: 64,
      pattern: "[A-Za-z0-9\\-]{1,64}",
      placeholder: "Bolna phone number id",
    });
    const selectNumber: HTMLButtonElement = button("Select number", () => {
      const id = numberId.value.trim();
      if (!PHONE_NUMBER_ID.test(id)) {
        return showStatus(status, "error", "Enter the number's id (letters, digits, dashes).");
      }
      void run(
        status,
        async () => {
          await ctx.api.request("post", "/v1/admin/provisioning/number", {
            body: { phone_number_id: id },
          });
          ctx.refresh();
        },
        { control: selectNumber },
      );
    });
    parts.push(
      h(
        "div",
        { class: "inline-form" },
        field("Existing number id", numberId, "An existing number on the Bolna account; nothing is purchased."),
        selectNumber,
      ),
    );
  }
  if (p.state === "verified") {
    parts.push(
      post(
        "Activate (go live)",
        "Activate the line? The receptionist will answer real callers with all booking tools. " +
          "Outbound contact stays as configured in clinic settings.",
        () => ctx.api.request("post", "/v1/admin/provisioning/activate"),
        "danger",
      ),
    );
  }
  if (RESETTABLE.has(p.state) && !p.step_pending) {
    const strayPossible =
      p.agent_create_pending && p.last_error !== null && STRAY_AGENT_ERRORS.has(p.last_error);
    const confirmBox = checkbox(false);
    const reset: HTMLButtonElement = button(
      "Reset",
      () => {
        const confirm = strayPossible && confirmBox.checked;
        const message = confirm
          ? `Reset and clear the uncertain agent create? Only do this if you checked the Bolna ` +
            `account and no agent named "${p.agent_name ?? ""}" exists.`
          : `Reset provisioning to ${provisioningStateLabel(p.resume_state).text}?`;
        if (!confirmAction(message)) return;
        void run(
          status,
          async () => {
            await ctx.api.request("post", "/v1/admin/provisioning/reset", {
              body: { confirm_no_stray_agent: confirm },
            });
            ctx.refresh();
          },
          { control: reset },
        );
      },
      { kind: "danger" },
    );
    parts.push(
      h(
        "div",
        { class: "inline-form" },
        strayPossible
          ? field(
              `I checked the Bolna account: no agent named "${p.agent_name ?? ""}" exists`,
              confirmBox,
              "Leave unchecked to keep the uncertain create pending (the agent step will not create).",
            )
          : null,
        reset,
      ),
    );
  }
  if (!parts.length) parts.push(h("p", { class: "muted" }, "No owner action available now."));
  return h("div", { class: "stack" }, parts);
}

async function load(ctx: Context): Promise<HTMLElement> {
  const tz = ctx.timeZone();
  const p = await ctx.api.request("get", "/v1/admin/provisioning");
  const status = statusLine();
  const events = p.events.map((e) => [
    formatInZone(e.created_at, tz),
    e.step,
    badge(provisioningStateLabel(e.from_state)),
    badge(provisioningStateLabel(e.to_state)),
    e.outcome,
  ]);
  return h(
    "div",
    {},
    section(
      "Provisioning",
      h(
        "dl",
        { class: "facts" },
        h("dt", {}, "State"),
        h("dd", {}, badge(provisioningStateLabel(p.state))),
        h("dt", {}, "Resets to"),
        h("dd", {}, provisioningStateLabel(p.resume_state).text),
        h("dt", {}, "Step pending"),
        h("dd", {}, yesNo(p.step_pending)),
        h("dt", {}, "Agent"),
        h("dd", {}, p.agent_id ?? "None", p.agent_name ? ` (${p.agent_name})` : ""),
        h("dt", {}, "Uncertain agent create"),
        h("dd", {}, yesNo(p.agent_create_pending)),
        h("dt", {}, "Phone number id"),
        h("dd", {}, p.phone_number_id ?? "None"),
        h("dt", {}, "Attempts"),
        h(
          "dd",
          {},
          `calendar ${p.attempts.calendar}, agent ${p.attempts.agent}, ` +
            `number ${p.attempts.number}, verify ${p.attempts.verify}`,
        ),
        h("dt", {}, "Last error"),
        h("dd", {}, p.last_error ?? "None"),
        h("dt", {}, "Updated"),
        h("dd", {}, formatInZone(p.updated_at, tz) || "Not started"),
      ),
      p.step_pending
        ? h("p", { class: "muted" }, "A step is queued or running. Refresh to see progress.")
        : null,
      h("div", { class: "actions" }, button("Refresh", () => ctx.refresh())),
    ),
    section(
      "Owner actions",
      isOwner(ctx.user)
        ? ownerActions(ctx, p, status)
        : h("p", { class: "muted" }, "Only the clinic owner can change provisioning."),
      status,
    ),
    section("Events (newest first)", table(["When", "Step", "From", "To", "Outcome"], events, "No events.")),
  );
}

export const provisioningScreen: Screen = {
  path: "provisioning",
  title: "Provisioning",
  visible: () => true,
  render(ctx) {
    const root = h("div");
    loading(root, () => load(ctx));
    return root;
  },
};
