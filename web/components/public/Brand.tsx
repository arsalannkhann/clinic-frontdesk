import Link from "next/link";

import { cx } from "@/lib/cx";

/** The aster mark: eight petals around a centre, drawn from ellipses. */
export function AsterMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" aria-hidden="true" className={cx("size-7", className)}>
      <g transform="translate(16 16)">
        {Array.from({ length: 8 }, (_, i) => (
          <ellipse
            key={i}
            cx="0"
            cy="-8.5"
            rx="3.1"
            ry="6.6"
            transform={`rotate(${i * 45})`}
            fill={i % 2 ? "var(--color-accent-tint)" : "var(--color-accent)"}
            opacity={i % 2 ? 1 : 0.92}
          />
        ))}
        <circle r="3.6" fill="var(--color-ink)" />
      </g>
    </svg>
  );
}

/**
 * The wordmark. Below `sm` only the first word shows (the short wordmark), so the header
 * never wraps at 360 px; the link's accessible name is always the full clinic name.
 */
export function BrandLink({ name, href = "/", className }: { name: string; href?: string; className?: string }) {
  const [first, ...rest] = name.split(" ");
  return (
    <Link href={href} aria-label={name} className={cx("group inline-flex min-w-0 items-center gap-2.5 rounded-sm", className)}>
      <AsterMark className="shrink-0" />
      <span aria-hidden="true" className="font-display text-[1.35rem] leading-none tracking-[-0.01em] whitespace-nowrap text-ink">
        {first}
        {rest.length ? <span className="text-ink-2 italic max-sm:hidden"> {rest.join(" ")}</span> : null}
      </span>
    </Link>
  );
}
