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

export function BrandLink({ name, href = "/", className }: { name: string; href?: string; className?: string }) {
  const [first, ...rest] = name.split(" ");
  return (
    <Link href={href} className={cx("group inline-flex items-center gap-2.5 rounded-sm", className)}>
      <AsterMark />
      <span className="font-display text-[1.35rem] leading-none tracking-[-0.01em] text-ink">
        {first}
        {rest.length ? <span className="text-ink-2 italic"> {rest.join(" ")}</span> : null}
      </span>
    </Link>
  );
}
