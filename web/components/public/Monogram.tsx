import { safeColour } from "@/lib/calendar";
import { cx } from "@/lib/cx";

export function initials(name: string): string {
  const words = name
    .replace(/^(dr\.?|prof\.?)\s+/i, "")
    .split(/\s+/)
    .filter(Boolean);
  const first = words[0]?.[0] ?? "";
  const last = words.length > 1 ? (words[words.length - 1]?.[0] ?? "") : "";
  return `${first}${last}`.toUpperCase();
}

/** A typographic stand-in for a portrait: initials on a tint of the doctor's colour. */
export function Monogram({ name, colour, className }: { name: string; colour: string | null; className?: string }) {
  const hex = safeColour(colour);
  return (
    <span
      aria-hidden="true"
      className={cx("inline-flex shrink-0 items-center justify-center rounded-full font-display text-ink", className)}
      style={{
        background: `color-mix(in srgb, ${hex} 18%, var(--color-raised))`,
        boxShadow: `inset 0 0 0 1px color-mix(in srgb, ${hex} 35%, transparent)`,
      }}
    >
      {initials(name)}
    </span>
  );
}
