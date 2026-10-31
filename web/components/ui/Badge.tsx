import type { ReactNode } from "react";

import { cx } from "@/lib/cx";
import type { Label, Tone } from "@/lib/labels";

const tones: Record<Tone, string> = {
  ok: "bg-ok-tint text-ok",
  warn: "bg-warn-tint text-warn",
  bad: "bg-bad-tint text-bad",
  info: "bg-info-tint text-info",
  neutral: "bg-sunk text-ink-2",
};

const dots: Record<Tone, string> = {
  ok: "bg-ok",
  warn: "bg-warn",
  bad: "bg-bad",
  info: "bg-info",
  neutral: "bg-ink-3",
};

export function Badge({
  tone = "neutral",
  children,
  className,
  dot = true,
}: {
  tone?: Tone;
  children: ReactNode;
  className?: string;
  dot?: boolean;
}) {
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-semibold whitespace-nowrap",
        tones[tone],
        className,
      )}
    >
      {dot ? <span aria-hidden="true" className={cx("size-1.5 rounded-full", dots[tone])} /> : null}
      {children}
    </span>
  );
}

export function LabelBadge({ label, className }: { label: Label; className?: string }) {
  return (
    <Badge tone={label.tone} className={className}>
      {label.text}
    </Badge>
  );
}
