import { CircleAlert } from "lucide-react";
import type { ReactNode } from "react";

import { cx } from "@/lib/cx";

export function Skeleton({ className }: { className?: string }) {
  return <span aria-hidden="true" className={cx("block animate-pulse rounded-sm bg-sunk", className)} />;
}

/** A labelled loading region: screen readers hear "Loading …" once. */
export function Loading({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    <div role="status" aria-label={label} className={className}>
      <span className="sr-only">{label}</span>
      {children}
    </div>
  );
}

export function EmptyState({
  title,
  children,
  action,
  icon,
  className,
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
  icon?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cx(
        "flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-line-strong px-6 py-10 text-center",
        className,
      )}
    >
      {icon ? <div className="mb-1 text-ink-3">{icon}</div> : null}
      <p className="font-display text-lg text-ink">{title}</p>
      {children ? <div className="max-w-md text-sm text-ink-2">{children}</div> : null}
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  );
}

export function ErrorState({
  title = "This didn't load",
  message,
  action,
  className,
}: {
  title?: string;
  message: string;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div role="alert" className={cx("flex items-start gap-3 rounded-lg border border-bad/25 bg-bad-tint/50 px-4 py-3", className)}>
      <CircleAlert className="mt-0.5 size-5 shrink-0 text-bad" aria-hidden="true" />
      <div className="flex-1">
        <p className="font-semibold text-ink">{title}</p>
        <p className="text-sm text-ink-2">{message}</p>
        {action ? <div className="mt-2">{action}</div> : null}
      </div>
    </div>
  );
}

export function Notice({ tone = "info", children, className }: { tone?: "info" | "warn"; children: ReactNode; className?: string }) {
  return (
    <div
      className={cx(
        "rounded-md border px-4 py-3 text-sm",
        tone === "warn" ? "border-warn/25 bg-warn-tint/60 text-ink" : "border-info/20 bg-info-tint/60 text-ink",
        className,
      )}
    >
      {children}
    </div>
  );
}
