/** Small render helpers shared by the screens. All text goes through dom.ts (Text nodes). */
import { errorMessage } from "./api/errors";
import { type Child, h, mount } from "./dom";
import type { Label } from "./labels";

export function badge(label: Label): HTMLSpanElement {
  return h("span", { class: `badge badge-${label.tone}` }, label.text);
}

/** A live region for action results. */
export function statusLine(): HTMLParagraphElement {
  return h("p", { class: "status", attrs: { role: "status", "aria-live": "polite" } });
}

export function showStatus(target: HTMLElement, kind: "ok" | "error" | "info", text: string) {
  target.className = `status status-${kind}`;
  target.textContent = text;
}

// Where results go when the action re-rendered the screen (its status line is detached).
let flash: (kind: "ok" | "error", text: string) => void = () => undefined;
export function setFlash(impl: (kind: "ok" | "error", text: string) => void): void {
  flash = impl;
}

/** Ask before a destructive or consequential action. Replaceable in tests. */
export let confirmAction = (message: string): boolean => window.confirm(message);
export function setConfirm(impl: (message: string) => boolean): void {
  confirmAction = impl;
}

/**
 * Run an action from a control: disables it while running, reports the outcome in
 * `status` (a returned string replaces the success text), and returns whether it succeeded. Error text is the mapped, user-safe message.
 */
export async function run(
  status: HTMLElement,
  action: () => Promise<string | void>,
  options: { success?: string; control?: HTMLButtonElement | HTMLFieldSetElement } = {},
): Promise<boolean> {
  const control = options.control;
  const wasDisabled = control?.disabled ?? false;
  if (control) control.disabled = true;
  showStatus(status, "info", "Working…");
  try {
    const text = (await action()) || options.success || "Done.";
    if (status.isConnected) showStatus(status, "ok", text);
    else flash("ok", text);
    return true;
  } catch (error) {
    if (status.isConnected) showStatus(status, "error", errorMessage(error));
    else flash("error", errorMessage(error));
    return false;
  } finally {
    if (control?.isConnected) control.disabled = wasDisabled;
  }
}

export function button(
  text: string,
  onClick: (event: MouseEvent) => void,
  options: { kind?: "primary" | "danger" | "plain"; disabled?: boolean; title?: string } = {},
): HTMLButtonElement {
  return h(
    "button",
    {
      class: `btn btn-${options.kind ?? "plain"}`,
      attrs: { type: "button", disabled: options.disabled ?? false, title: options.title },
      on: { click: onClick },
    },
    text,
  );
}

export function field(label: string, control: HTMLElement, hint?: string): HTMLLabelElement {
  return h(
    "label",
    { class: "field" },
    h("span", { class: "field-label" }, label),
    control,
    hint ? h("small", { class: "hint" }, hint) : null,
  );
}

export function input(attrs: Record<string, string | number | boolean | null | undefined>) {
  return h("input", { attrs });
}

export function select<T extends string>(
  options: readonly (readonly [T, string])[],
  value: T | "",
  attrs: Record<string, string | boolean> = {},
): HTMLSelectElement {
  return h(
    "select",
    { attrs },
    options.map(([key, text]) => h("option", { attrs: { value: key, selected: key === value } }, text)),
  );
}

export function checkbox(checked: boolean, attrs: Record<string, string | boolean> = {}) {
  return h("input", { attrs: { type: "checkbox", checked, ...attrs } });
}

export function table(headers: readonly string[], rows: readonly Child[][], empty: string) {
  if (!rows.length) return h("p", { class: "empty" }, empty);
  return h(
    "div",
    { class: "table-wrap" },
    h(
      "table",
      {},
      h("thead", {}, h("tr", {}, headers.map((text) => h("th", { attrs: { scope: "col" } }, text)))),
      h("tbody", {}, rows.map((cells) => h("tr", {}, cells.map((cell) => h("td", {}, cell))))),
    ),
  );
}

export function section(title: string, ...children: Child[]): HTMLElement {
  return h("section", { class: "card" }, h("h2", {}, title), ...children);
}

/** Render a screen body that loads asynchronously; failures show the mapped message. */
export function loading(target: HTMLElement, load: () => Promise<Child>): void {
  mount(target, h("p", { class: "muted" }, "Loading…"));
  load().then(
    (content) => mount(target, content),
    (error: unknown) => mount(target, h("p", { class: "status status-error" }, errorMessage(error))),
  );
}

export function short(id: string | null | undefined): string {
  return id ? id.slice(0, 8) : "";
}

export function yesNo(value: boolean): string {
  return value ? "Yes" : "No";
}
