import type { ApiClient } from "../api/client";
import type { Screen } from "../context";
import { h } from "../dom";
import { startSession } from "../session";
import { button, field, input, run, section, statusLine } from "../ui";

export const accountScreen: Screen = {
  path: "account",
  title: "My account",
  visible: () => true,
  render(ctx) {
    const status = statusLine();
    const current = input({ type: "password", required: true, autocomplete: "current-password" });
    const next = input({
      type: "password",
      required: true,
      minlength: 12,
      maxlength: 256,
      autocomplete: "new-password",
    });
    const change = button(
      "Change password",
      () =>
        void run(
          status,
          async () => {
            await ctx.api.request("post", "/v1/staff/me/password", {
              body: { current_password: current.value, new_password: next.value },
            });
            current.value = "";
            next.value = "";
          },
          { success: "Password changed.", control: change },
        ),
      { kind: "primary" },
    );
    return h(
      "div",
      {},
      section(
        "Signed in",
        h("p", {}, `${ctx.user.email} (${ctx.user.role})`),
      ),
      section(
        "Change password",
        field("Current password", current),
        field("New password", next, "At least 12 characters."),
        h("div", { class: "actions" }, change),
        status,
      ),
    );
  },
};

/** The sign-in form. On success the session lives in memory only. */
export function loginView(api: ApiClient, onSignedIn: () => void): HTMLElement {
  const status = statusLine();
  const email = input({ type: "email", required: true, autocomplete: "username", maxlength: 254 });
  const password = input({
    type: "password",
    required: true,
    autocomplete: "current-password",
    maxlength: 256,
  });
  const submit = h("button", { class: "btn btn-primary", attrs: { type: "submit" } }, "Sign in");
  const form = h(
    "form",
    {
      class: "card login",
      on: {
        submit: (event) => {
          event.preventDefault();
          void run(
            status,
            async () => {
              const session = await api.request("post", "/v1/staff/login", {
                body: { email: email.value.trim(), password: password.value },
              });
              password.value = "";
              startSession(session);
              onSignedIn();
            },
            { success: "Signed in.", control: submit },
          );
        },
      },
    },
    h("h1", {}, "Clinic staff sign-in"),
    field("Email", email),
    field("Password", password),
    h("div", { class: "actions" }, submit),
    status,
    h(
      "p",
      { class: "muted" },
      "Your session is kept in this tab's memory only; reloading the page signs you out.",
    ),
  );
  return form;
}
