"use client";

import type { CalendarAppointment } from "@/lib/api/types";
import { safeColour, type Positioned } from "@/lib/calendar";
import { cx } from "@/lib/cx";
import { appointmentStatus, channel as channelLabel, syncState } from "@/lib/labels";
import { formatTime } from "@/lib/time";

const SYNC_DOT: Record<string, string> = { ok: "bg-ok", warn: "bg-warn", bad: "bg-bad", info: "bg-info", neutral: "bg-ink-3" };

export function AppointmentBlock({
  entry,
  pxPerMinute,
  timeZone,
  onOpen,
  showDoctor,
}: {
  entry: Positioned<CalendarAppointment>;
  pxPerMinute: number;
  timeZone: string;
  onOpen: (appointment: CalendarAppointment) => void;
  showDoctor?: string;
}) {
  const a = entry.item;
  const colour = safeColour(a.colour);
  const height = Math.max(entry.height * pxPerMinute - 2, 16);
  const compact = height < 34;
  const status = appointmentStatus(a.status);
  const sync = syncState(a.sync_state);
  const cancelled = a.status === "cancelled";
  const who = a.patient_name ?? "Patient";
  const width = 100 / entry.lanes;
  const time = `${formatTime(a.starts_at, timeZone)}–${formatTime(a.ends_at, timeZone)}`;
  const label = [time, a.service_name, who, showDoctor, status.text, `calendar: ${sync.text}`, channelLabel(a.channel).text].filter(Boolean).join(", ");

  return (
    <button
      type="button"
      onClick={(event) => {
        event.stopPropagation();
        onOpen(a);
      }}
      aria-label={label}
      title={label}
      className={cx(
        "absolute overflow-hidden rounded-sm border-l-[3px] px-1.5 text-left text-ink transition-shadow hover:z-10 hover:shadow-lift focus-visible:z-20",
        cancelled && "hatch opacity-70",
        a.status === "needs_review" && "ring-1 ring-bad ring-inset",
        a.status === "pending_sync" && "outline-dashed outline-1 -outline-offset-1 outline-warn/60",
        compact ? "py-0" : "py-1",
      )}
      style={{
        top: entry.top * pxPerMinute + 1,
        height,
        left: `calc(${entry.lane * width}% + 2px)`,
        width: `calc(${width}% - 4px)`,
        borderLeftColor: colour,
        background: cancelled ? undefined : `color-mix(in srgb, ${colour} 15%, var(--color-raised))`,
      }}
    >
      <span className={cx("flex items-center gap-1 text-[0.72rem] leading-tight tnum", compact ? "whitespace-nowrap" : "")}>
        <span aria-hidden="true" className={cx("size-1.5 shrink-0 rounded-full", SYNC_DOT[sync.tone])} />
        <span className="font-semibold">{formatTime(a.starts_at, timeZone)}</span>
        {compact ? <span className={cx("truncate", cancelled && "line-through")}>{who}</span> : null}
      </span>
      {!compact ? (
        <>
          <span className={cx("block truncate text-[0.8rem] leading-tight font-semibold", cancelled && "line-through")}>{who}</span>
          <span className="block truncate text-[0.72rem] leading-tight text-ink-2">{a.service_name}</span>
          {height > 64 && a.status !== "confirmed" ? (
            <span className="mt-0.5 block truncate text-[0.68rem] font-semibold tracking-wide text-ink-2 uppercase">{status.text}</span>
          ) : null}
        </>
      ) : null}
    </button>
  );
}
