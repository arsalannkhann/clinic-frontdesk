"use client";

import { useId, useRef, type KeyboardEvent, type ReactNode } from "react";

import { cx } from "@/lib/cx";

/** WAI-ARIA tabs with roving tabindex: arrow keys, Home and End move between tabs. */

export interface TabItem<K extends string> {
  key: K;
  label: ReactNode;
  count?: number;
}

interface TabsProps<K extends string> {
  items: readonly TabItem<K>[];
  value: K;
  onChange: (key: K) => void;
  label: string;
  className?: string;
  /** Render the panel; omitted when the tabs drive something outside (a segmented control). */
  children?: ReactNode;
}

export function Tabs<K extends string>({ items, value, onChange, label, className, children }: TabsProps<K>) {
  const base = useId();
  const refs = useRef<(HTMLButtonElement | null)[]>([]);

  function onKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    let next = index;
    if (event.key === "ArrowRight") next = (index + 1) % items.length;
    else if (event.key === "ArrowLeft") next = (index - 1 + items.length) % items.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = items.length - 1;
    else return;
    event.preventDefault();
    const item = items[next];
    if (!item) return;
    onChange(item.key);
    refs.current[next]?.focus();
  }

  return (
    <div className={className}>
      <div role="tablist" aria-label={label} className="flex gap-1 overflow-x-auto border-b border-line">
        {items.map((item, index) => {
          const selected = item.key === value;
          return (
            <button
              key={item.key}
              ref={(node) => {
                refs.current[index] = node;
              }}
              id={`${base}-tab-${item.key}`}
              role="tab"
              type="button"
              aria-selected={selected}
              aria-controls={children ? `${base}-panel` : undefined}
              tabIndex={selected ? 0 : -1}
              onClick={() => onChange(item.key)}
              onKeyDown={(event) => onKeyDown(event, index)}
              className={cx(
                "-mb-px inline-flex items-center gap-2 border-b-2 px-3 py-2 text-sm font-semibold whitespace-nowrap transition-colors",
                selected ? "border-accent text-ink" : "border-transparent text-ink-2 hover:text-ink",
              )}
            >
              {item.label}
              {item.count !== undefined ? (
                <span
                  className={cx(
                    "rounded-full px-1.5 py-px text-xs tnum",
                    selected ? "bg-accent-tint text-accent-strong" : "bg-sunk text-ink-2",
                  )}
                >
                  {item.count}
                </span>
              ) : null}
            </button>
          );
        })}
      </div>
      {children ? (
        <div id={`${base}-panel`} role="tabpanel" aria-labelledby={`${base}-tab-${value}`} className="pt-4">
          {children}
        </div>
      ) : null}
    </div>
  );
}

/** A compact two-to-four option switch (Day / Week), a radiogroup. */
export function Segmented<K extends string>({
  items,
  value,
  onChange,
  label,
}: {
  items: readonly { key: K; label: ReactNode }[];
  value: K;
  onChange: (key: K) => void;
  label: string;
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  return (
    <div role="radiogroup" aria-label={label} className="inline-flex rounded-md border border-line-strong bg-raised p-0.5">
      {items.map((item, index) => {
        const checked = item.key === value;
        return (
          <button
            key={item.key}
            ref={(node) => {
              refs.current[index] = node;
            }}
            type="button"
            role="radio"
            aria-checked={checked}
            tabIndex={checked ? 0 : -1}
            onClick={() => onChange(item.key)}
            onKeyDown={(event) => {
              if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
              event.preventDefault();
              const next = (index + (event.key === "ArrowRight" ? 1 : -1) + items.length) % items.length;
              onChange(items[next]!.key);
              refs.current[next]?.focus();
            }}
            className={cx(
              "rounded-sm px-3 py-1 text-sm font-semibold transition-colors",
              checked ? "bg-ink text-paper" : "text-ink-2 hover:text-ink",
            )}
          >
            {item.label}
          </button>
        );
      })}
    </div>
  );
}
