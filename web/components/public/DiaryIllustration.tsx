/**
 * Hero illustration: an abstract page of the clinic diary, drawn with rectangles and type.
 * It carries no data (no counts, no names) and is hidden from assistive technology.
 */

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
// Rows are half hours from 9:30; each entry is [day, startRow, rows, tone].
const BLOCKS: [number, number, number, "accent" | "ink" | "tint" | "line"][] = [
  [0, 0, 2, "tint"],
  [0, 3, 1, "line"],
  [0, 5, 2, "accent"],
  [1, 1, 2, "ink"],
  [1, 4, 1, "tint"],
  [2, 0, 1, "line"],
  [2, 2, 3, "tint"],
  [2, 6, 1, "accent"],
  [3, 1, 1, "accent"],
  [3, 3, 2, "line"],
  [4, 0, 2, "ink"],
  [4, 4, 2, "tint"],
  [5, 1, 2, "line"],
  [5, 4, 1, "accent"],
];

const fills = {
  accent: "fill-accent",
  ink: "fill-ink",
  tint: "fill-accent-tint",
  line: "fill-sunk",
};

export function DiaryIllustration() {
  const col = 58;
  const row = 34;
  const left = 46;
  const top = 40;
  const rows = 8;
  return (
    <svg viewBox={`0 0 ${left + col * 6 + 12} ${top + row * rows + 16}`} aria-hidden="true" className="h-auto w-full">
      <rect x="0.5" y="0.5" width={left + col * 6 + 11} height={top + row * rows + 15} rx="14" className="fill-raised stroke-line" />
      {DAYS.map((day, i) => (
        <text key={day} x={left + col * i + col / 2} y={26} textAnchor="middle" className="fill-ink-3 font-sans text-[11px] tracking-[0.08em] uppercase">
          {day}
        </text>
      ))}
      {Array.from({ length: rows + 1 }, (_, r) => (
        <line key={r} x1={left - 6} x2={left + col * 6} y1={top + r * row} y2={top + r * row} className={r % 2 ? "stroke-line/60" : "stroke-line"} />
      ))}
      {["9:30", "10:30", "11:30", "12:30"].map((label, i) => (
        <text key={label} x={left - 10} y={top + i * row * 2 + 4} textAnchor="end" className="fill-ink-3 font-sans text-[10px]">
          {label}
        </text>
      ))}
      {BLOCKS.map(([day, start, span, tone], i) => (
        <rect
          key={i}
          x={left + col * day + 4}
          y={top + start * row + 3}
          width={col - 8}
          height={span * row - 6}
          rx="5"
          className={fills[tone]}
        />
      ))}
      <line x1={left - 6} x2={left + col * 6} y1={top + row * 2.6} y2={top + row * 2.6} className="stroke-accent" strokeWidth="1.5" />
      <circle cx={left - 6} cy={top + row * 2.6} r="3.5" className="fill-accent" />
    </svg>
  );
}
