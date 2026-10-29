/**
 * App shell: sign-in, navigation by URL fragment (#/appointments; never carries data or
 * tokens), role-filtered menu and logout. Hidden controls are a convenience only; the
 * backend authorizes every request.
 */
import { ApiClient } from "./api/client";
import { SESSION_ENDED_MESSAGE } from "./api/errors";
import type { Context, Screen } from "./context";
import { h, mount } from "./dom";
import { accountScreen, loginView } from "./screens/account";
import { appointmentsScreen } from "./screens/appointments";
import { calendarScreen } from "./screens/calendar";
import { clinicScreen } from "./screens/clinic";
import { exceptionsScreen } from "./screens/exceptions";
import { outboxScreen, remindersScreen } from "./screens/operations";
import { provisioningScreen } from "./screens/provisioning";
import { scheduleScreen } from "./screens/schedule";
import { usersScreen } from "./screens/users";
import { endSession, endSessionIfCurrent, onSessionChange, sessionToken, sessionUser } from "./session";
import { setFlash, showStatus } from "./ui";

export const SCREENS: readonly Screen[] = [
  appointmentsScreen,
  clinicScreen,
  scheduleScreen,
  exceptionsScreen,
  remindersScreen,
  outboxScreen,
  calendarScreen,
  provisioningScreen,
  usersScreen,
  accountScreen,
];

const DEFAULT_SCREEN = appointmentsScreen.path;

function currentPath(): string {
  return window.location.hash.replace(/^#\/?/, "").split(/[/?]/)[0] ?? "";
}

export function startApp(root: HTMLElement, baseUrl: string): void {
  let timeZone = "UTC";
  // Survives re-renders so a result stays visible after the screen reloads its data.
  const flash = h("p", { class: "status", attrs: { role: "status", "aria-live": "polite" } });
  setFlash((kind, text) => showStatus(flash, kind, text));
  const api = new ApiClient({
    baseUrl,
    getToken: sessionToken,
    onUnauthorized: (token) => {
      // Clears the flash and shows the sign-in form, unless a newer session replaced it.
      if (endSessionIfCurrent(token)) showStatus(flash, "error", SESSION_ENDED_MESSAGE);
    },
  });

  const render = (): void => {
    const user = sessionUser();
    if (user === null || sessionToken() === null) {
      document.title = "Sign in · Clinic dashboard";
      mount(
        root,
        h(
          "main",
          { class: "login-wrap" },
          h(
            "div",
            {},
            flash, // E.g. "Your session has ended" after a 401.
            loginView(api, () => {
              flash.textContent = "";
              void loadClinicZone().then(render);
            }),
          ),
        ),
      );
      return;
    }
    const visible = SCREENS.filter((screen) => screen.visible(user));
    const path = currentPath();
    const screen = visible.find((s) => s.path === path) ?? visible.find((s) => s.path === DEFAULT_SCREEN);
    if (!screen) return;
    if (screen.path !== path) {
      history.replaceState(null, "", `#/${screen.path}`);
    }
    document.title = `${screen.title} · Clinic dashboard`;
    const ctx: Context = {
      api,
      user,
      timeZone: () => timeZone,
      setTimeZone: (value) => {
        timeZone = value || "UTC";
      },
      refresh: render,
    };
    const logout = h(
      "button",
      {
        class: "btn btn-plain",
        attrs: { type: "button" },
        on: {
          click: () => {
            // Best effort: the local session ends even if the server cannot be reached.
            void api.request("post", "/v1/staff/logout").catch(() => undefined).finally(endSession);
          },
        },
      },
      "Sign out",
    );
    mount(
      root,
      h(
        "header",
        { class: "topbar" },
        h("strong", {}, "Clinic dashboard"),
        h("span", { class: "who" }, `${user.email} · ${user.role}`),
        logout,
      ),
      h(
        "nav",
        { class: "nav", attrs: { "aria-label": "Sections" } },
        visible.map((s) =>
          h(
            "a",
            {
              class: s === screen ? "active" : "",
              attrs: { href: `#/${s.path}`, "aria-current": s === screen ? "page" : null },
            },
            s.title,
          ),
        ),
      ),
      h("main", { class: "content" }, h("h1", {}, screen.title), flash, screen.render(ctx)),
    );
  };

  const loadClinicZone = async (): Promise<void> => {
    try {
      timeZone = (await api.request("get", "/v1/admin/clinic")).timezone || "UTC";
    } catch {
      timeZone = "UTC";
    }
  };

  window.addEventListener("hashchange", () => {
    flash.textContent = "";
    render();
  });
  onSessionChange((user) => {
    if (user === null) {
      timeZone = "UTC";
      flash.textContent = "";
      render();
    }
  });
  render();
}
