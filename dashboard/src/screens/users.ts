import type { Schemas } from "../api/client";
import type { Context, Screen } from "../context";
import { h } from "../dom";
import { type Role, isOwner } from "../session";
import {
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
  yesNo,
} from "../ui";

const ROLES = [
  ["owner", "Owner"],
  ["admin", "Admin"],
  ["viewer", "Viewer"],
] as const;

function userRow(ctx: Context, user: Schemas["StaffUserDetail"], status: HTMLElement) {
  const self = user.id === ctx.user.id;
  const role = select<Role>(ROLES, user.role, { disabled: self });
  const update = (body: Schemas["StaffUserUpdate"], message: string) => (event: MouseEvent) => {
    if (!confirmAction(message)) return;
    void run(
      status,
      async () => {
        await ctx.api.request("patch", "/v1/staff/users/{user_id}", {
          params: { user_id: user.id },
          body,
        });
        ctx.refresh();
      },
      { control: event.currentTarget as HTMLButtonElement },
    );
  };
  return [
    user.email,
    role,
    yesNo(user.disabled),
    self
      ? h("span", { class: "muted" }, "You")
      : h(
          "div",
          { class: "actions" },
          button(
            "Set role",
            (event) =>
              update(
                { role: role.value as Role },
                `Change ${user.email} to ${role.value}? Their sessions keep the new role.`,
              )(event),
          ),
          button(
            user.disabled ? "Enable" : "Disable",
            update(
              { disabled: !user.disabled },
              user.disabled ? `Enable ${user.email}?` : `Disable ${user.email}? They are signed out.`,
            ),
            { kind: user.disabled ? "plain" : "danger" },
          ),
        ),
  ];
}

async function load(ctx: Context): Promise<HTMLElement> {
  const users = await ctx.api.request("get", "/v1/staff/users");
  const status = statusLine();
  const email = input({ type: "email", maxlength: 254, required: true, autocomplete: "off" });
  const role = select<Role>(ROLES, "viewer");
  const password = input({
    type: "password",
    minlength: 12,
    maxlength: 256,
    required: true,
    autocomplete: "new-password",
  });
  const create = button(
    "Add staff user",
    () =>
      void run(
        status,
        async () => {
          await ctx.api.request("post", "/v1/staff/users", {
            body: {
              email: email.value.trim(),
              role: role.value as Role,
              temporary_password: password.value,
            },
          });
          password.value = "";
          ctx.refresh();
        },
        { success: "User added. Share the temporary password out of band.", control: create },
      ),
    { kind: "primary" },
  );
  return h(
    "div",
    {},
    section(
      "Staff users",
      table(["Email", "Role", "Disabled", ""], users.map((u) => userRow(ctx, u, status)), "No users."),
      status,
    ),
    section(
      "Add a user",
      h(
        "div",
        { class: "inline-form" },
        field("Email", email),
        field("Role", role),
        field("Temporary password", password, "At least 12 characters."),
        create,
      ),
    ),
  );
}

export const usersScreen: Screen = {
  path: "users",
  title: "Staff users",
  visible: isOwner,
  render(ctx) {
    const root = h("div");
    loading(root, () => load(ctx));
    return root;
  },
};
