/**
 * Minimal DOM builders. Every string child becomes a Text node, so data from the API is
 * never parsed as HTML. There is deliberately no way to set innerHTML, inline event-handler
 * attributes, inline styles or non-http(s) URLs through these helpers; a test scans the
 * sources for HTML-string sinks.
 */

export type Child = Node | string | number | null | undefined | false | readonly Child[];

type Handlers = {
  [K in keyof HTMLElementEventMap]?: (event: HTMLElementEventMap[K]) => void;
};

export interface Props {
  class?: string;
  attrs?: Readonly<Record<string, string | number | boolean | null | undefined>>;
  on?: Handlers;
}

const BLOCKED_ATTRIBUTE = /^(on|style$|srcdoc$|formaction$)/i;
const URL_ATTRIBUTES = new Set(["href", "src", "action", "xlink:href", "poster", "data"]);

/** True for absolute http(s) URLs and same-document relative ones. */
export function isSafeUrl(value: string): boolean {
  try {
    const url = new URL(value, document.baseURI);
    return url.protocol === "https:" || url.protocol === "http:";
  } catch {
    return false;
  }
}

export function append(parent: Node, children: readonly Child[]): void {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    if (Array.isArray(child)) append(parent, child);
    else if (child instanceof Node) parent.appendChild(child);
    else parent.appendChild(document.createTextNode(String(child)));
  }
}

export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  props: Props = {},
  ...children: Child[]
): HTMLElementTagNameMap[K] {
  const element = document.createElement(tag);
  if (props.class) element.className = props.class;
  for (const [name, value] of Object.entries(props.attrs ?? {})) {
    if (value === null || value === undefined || value === false) continue;
    if (BLOCKED_ATTRIBUTE.test(name)) throw new Error(`Attribute not allowed: ${name}`);
    const text = value === true ? "" : String(value);
    if (URL_ATTRIBUTES.has(name.toLowerCase()) && !isSafeUrl(text)) {
      throw new Error(`Unsafe URL for ${name}`);
    }
    element.setAttribute(name, text);
  }
  for (const [type, handler] of Object.entries(props.on ?? {})) {
    if (handler) element.addEventListener(type, handler as EventListener);
  }
  append(element, children);
  return element;
}

/** Replace all children of `parent`. */
export function mount(parent: Element, ...children: Child[]): void {
  parent.replaceChildren();
  append(parent, children);
}
