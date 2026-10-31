"use client";

import { CalendarPlus, ChevronLeft, ChevronRight } from "lucide-react";
import { useEffect, useMemo, useState, type MouseEvent } from "react";
import useSWR from "swr";

import { withQuery } from "@/lib/api/client";
import type { CalendarAppointment, CalendarDoctor, CalendarResponse, ScheduleException } from "@/lib/api/types";
import {
  clipToDay,
  gridLines,
  isCovered,
  layoutColumn,
  nowOffset,
  safeColour,
  SLOT_MINUTES,
  slotAt,
  visibleWindow,
  type TimeWindow,
} from "@/lib/calendar";
import { useCatalog } from "@/lib/catalog";
import { cx } from "@/lib/cx";
import { appointmentStatus, exceptionKind } from "@/lib/labels";
import {
  addDays,
  dayKey,
  formatDayKey,
  formatTime,
  hhmm,
  minutesLabel,
  startOfWeek,
  todayKey,
  zonedToIso,
  zoneAbbreviation,
} from "@/lib/time";
import { LabelBadge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Field, Select } from "@/components/ui/Field";
import { EmptyState, ErrorState, Loading, Skeleton } from "@/components/ui/States";
import { Segmented } from "@/components/ui/Tabs";
import { AppointmentDrawer, type DrawerAppointment } from "../AppointmentDrawer";
import { PageHeader } from "../PageHeader";
import { StaffBookingDialog, type BookingDraft } from "../StaffBookingDialog";
import { useStaff } from "../StaffContext";
import { AppointmentBlock } from "./AppointmentBlock";

const HOUR_PX = 72;
const PX = HOUR_PX / 60;
const ALL = "all";
/** The staff booking dialog offers start times from 7 am to 9 pm. */
const BOOKING_START = 7 * 60;
const BOOKING_END = 21 * 60;

/** One calendar read returns at most 2,000 appointments (the earliest); `truncated` says more exist. */
export const TRUNCATED_NOTICE = "Showing the first 2,000 appointments — narrow the range";

type View = "day" | "week";

function useNow(intervalMs = 60_000): number | null {
  // Null until mounted, so server and client markup agree.
  const [now, setNow] = useState<number | null>(null);
  useEffect(() => {
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);
  return now;
}

function exceptionsFor(exceptions: readonly ScheduleException[], doctorId: string | null) {
  return exceptions.filter((e) => e.doctor_id === null || doctorId === null || e.doctor_id === doctorId);
}

/**
 * The first free 15-minute start for `doctorId` on `day`: inside the visible window, not in
 * the past, not covered by an active appointment or blocked time. Null when the day is full.
 * Used as the default time for "Add booking", which is the keyboard route to booking.
 */
export function firstFreeMinute(
  day: string,
  doctorId: string,
  timeZone: string,
  window: TimeWindow,
  appointments: readonly CalendarAppointment[],
  exceptions: readonly ScheduleException[],
  now: number | null,
): number | null {
  const active = appointments.filter((a) => a.doctor_id === doctorId && a.status !== "cancelled");
  const blocked = exceptionsFor(exceptions, doctorId);
  // Within the booking dialog's time list (7 am to 9 pm), even when the grid shows more.
  const from = Math.max(window.start, BOOKING_START);
  const to = Math.min(window.end, BOOKING_END);
  for (let minute = from; minute < to; minute += SLOT_MINUTES) {
    if (now !== null && Date.parse(zonedToIso(`${day}T${hhmm(minute)}`, timeZone)) < now) continue;
    if (isCovered(minute, day, timeZone, active) || isCovered(minute, day, timeZone, blocked)) continue;
    return minute;
  }
  return null;
}

function Gutter({ window }: { window: TimeWindow }) {
  return (
    <div className="relative border-r border-line" style={{ height: (window.end - window.start) * PX }} aria-hidden="true">
      {gridLines(window)
        .filter((line) => line.major)
        .map((line) => (
          <span key={line.minute} className="absolute right-2 -translate-y-1/2 text-xs text-ink-2 tnum" style={{ top: (line.minute - window.start) * PX }}>
            {line.minute === window.start ? "" : minutesLabel(line.minute)}
          </span>
        ))}
    </div>
  );
}

function Column({
  dayKeyValue,
  timeZone,
  window,
  appointments,
  exceptions,
  now,
  canBook,
  onOpen,
  onBook,
  doctorNames,
  label,
}: {
  dayKeyValue: string;
  timeZone: string;
  window: TimeWindow;
  appointments: CalendarAppointment[];
  exceptions: ScheduleException[];
  now: number | null;
  canBook: boolean;
  onOpen: (a: CalendarAppointment) => void;
  onBook?: (minute: number) => void;
  doctorNames?: Map<string, string>;
  label: string;
}) {
  const positioned = useMemo(() => layoutColumn(appointments, dayKeyValue, timeZone, window), [appointments, dayKeyValue, timeZone, window]);
  const blocked = useMemo(() => layoutColumn(exceptions.map((e) => ({ ...e })), dayKeyValue, timeZone, window), [exceptions, dayKeyValue, timeZone, window]);
  const active = appointments.filter((a) => a.status !== "cancelled");
  const nowTop = now === null ? null : nowOffset(now, dayKeyValue, timeZone, window);
  const past = now !== null && dayKeyValue < dayKey(now, timeZone);

  // Pointer shortcut only; keyboard users book with the column's "+" button or Add booking.
  function onClick(event: MouseEvent<HTMLDivElement>) {
    if (!canBook || !onBook || past || event.target !== event.currentTarget) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const minute = slotAt((event.clientY - rect.top) / PX, window);
    if (minute === null) return;
    if (isCovered(minute, dayKeyValue, timeZone, active) || isCovered(minute, dayKeyValue, timeZone, exceptions)) return;
    if (now !== null && Date.parse(zonedToIso(`${dayKeyValue}T${hhmm(minute)}`, timeZone)) < now) return;
    onBook(minute);
  }

  return (
    <div
      role="group"
      aria-label={label}
      onClick={onClick}
      className={cx("cal-grid relative border-r border-line last:border-r-0", canBook && !past && "cursor-cell", past && "bg-sunk/35")}
      style={{ height: (window.end - window.start) * PX, ["--hour" as string]: HOUR_PX }}
    >
      {blocked.map((entry) => (
        <div
          key={entry.item.id}
          className="hatch pointer-events-none absolute inset-x-0 border-y border-line-strong/60 bg-sunk/50 px-2 py-1"
          style={{ top: entry.top * PX, height: entry.height * PX }}
        >
          <span className="text-xs font-semibold text-ink-2">
            {exceptionKind(entry.item.kind).text}
            {entry.item.label ? `: ${entry.item.label}` : ""}
          </span>
        </div>
      ))}
      {positioned.map((entry) => (
        <AppointmentBlock
          key={entry.item.id}
          entry={entry}
          pxPerMinute={PX}
          timeZone={timeZone}
          onOpen={onOpen}
          showDoctor={doctorNames?.get(entry.item.doctor_id)}
        />
      ))}
      {nowTop !== null ? (
        <div className="pointer-events-none absolute inset-x-0 z-10 border-t-2 border-accent" style={{ top: nowTop * PX }} aria-hidden="true">
          <span className="absolute -top-[5px] -left-[5px] size-2 rounded-full bg-accent" />
        </div>
      ) : null}
    </div>
  );
}

function AppointmentRow({
  a,
  timeZone,
  onOpen,
  doctor,
  dense,
}: {
  a: CalendarAppointment;
  timeZone: string;
  onOpen: (a: CalendarAppointment) => void;
  doctor?: CalendarDoctor;
  dense?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={() => onOpen(a)}
      className={cx("flex w-full items-start gap-3 text-left hover:bg-sunk/50", dense ? "px-3 py-2" : "px-4 py-3")}
    >
      <span aria-hidden="true" className={cx("mt-1 w-1 shrink-0 rounded-full", dense ? "h-7" : "h-9")} style={{ background: safeColour(a.colour) }} />
      <span className={cx("shrink-0 font-semibold tnum", dense ? "w-14 text-sm" : "w-16")}>{formatTime(a.starts_at, timeZone)}</span>
      <span className="min-w-0 flex-1">
        <span className={cx("block truncate font-semibold", dense && "text-sm", a.status === "cancelled" && "line-through opacity-70")}>
          {a.patient_name ?? "Patient"}
        </span>
        <span className={cx("block truncate text-ink-2", dense ? "text-xs" : "text-sm")}>
          {doctor ? (
            <>
              <span aria-hidden="true" className="mr-1 inline-block size-1.5 rounded-full align-middle" style={{ background: safeColour(doctor.colour) }} />
              {doctor.name}
            </>
          ) : (
            a.service_name
          )}
        </span>
      </span>
      {a.status !== "confirmed" && a.status !== "cancelled" ? <LabelBadge label={appointmentStatus(a.status)} /> : null}
    </button>
  );
}

function MobileDayList({
  appointments,
  exceptions,
  timeZone,
  onOpen,
}: {
  appointments: CalendarAppointment[];
  exceptions: ScheduleException[];
  timeZone: string;
  onOpen: (a: CalendarAppointment) => void;
}) {
  if (appointments.length === 0 && exceptions.length === 0) {
    return <EmptyState title="Nothing booked">No appointments for this dentist on this day.</EmptyState>;
  }
  return (
    <ol className="flex flex-col divide-y divide-line rounded-lg border border-line bg-raised">
      {exceptions.map((e) => (
        <li key={e.id} className="hatch px-4 py-3 text-sm">
          <span className="font-semibold">{exceptionKind(e.kind).text}</span>
          {e.label ? `: ${e.label}` : ""} · {formatTime(e.starts_at, timeZone)}–{formatTime(e.ends_at, timeZone)}
        </li>
      ))}
      {appointments.map((a) => (
        <li key={a.id}>
          <AppointmentRow a={a} timeZone={timeZone} onOpen={onOpen} />
        </li>
      ))}
    </ol>
  );
}

/**
 * The week as seven day cards: used for "All dentists" (where a 7-day grid with every
 * dentist's lanes is unreadable) and for one dentist on small screens. Each card has the
 * day's count, per-dentist counts when showing everyone, and the appointments as a list.
 */
function WeekList({
  days,
  appointments,
  exceptions,
  doctors,
  showDoctors,
  timeZone,
  now,
  onOpen,
  onDay,
  onBook,
}: {
  days: string[];
  appointments: CalendarAppointment[];
  exceptions: ScheduleException[];
  doctors: CalendarDoctor[];
  showDoctors: boolean;
  timeZone: string;
  now: number | null;
  onOpen: (a: CalendarAppointment) => void;
  onDay: (day: string) => void;
  onBook?: (day: string) => void;
}) {
  const byId = new Map(doctors.map((d) => [d.id, d]));
  const today = now === null ? null : dayKey(now, timeZone);
  return (
    <ol className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 2xl:grid-cols-7">
      {days.map((day) => {
        const list = appointments
          .filter((a) => a.status !== "cancelled" && clipToDay(a, day, timeZone))
          .sort((x, y) => Date.parse(x.starts_at) - Date.parse(y.starts_at));
        const blocks = exceptions.filter((e) => clipToDay(e, day, timeZone));
        const past = today !== null && day < today;
        return (
          <li key={day} className={cx("flex flex-col rounded-lg border border-line bg-raised", past && "bg-sunk/35")}>
            <div className="flex items-start justify-between gap-2 border-b border-line px-3 py-2">
              <div>
                <button
                  type="button"
                  onClick={() => onDay(day)}
                  className={cx("rounded-sm font-semibold hover:text-accent", day === today && "text-accent")}
                  aria-label={`${formatDayKey(day, "long")}: open the day view`}
                >
                  {formatDayKey(day, "short")}
                </button>
                <p className="text-xs text-ink-2 tnum">{list.length === 1 ? "1 appointment" : `${list.length} appointments`}</p>
              </div>
              {onBook && !past ? (
                <button
                  type="button"
                  onClick={() => onBook(day)}
                  className="rounded-sm p-1 text-ink-3 hover:bg-sunk hover:text-ink"
                  aria-label={`Add a booking on ${formatDayKey(day, "long")}`}
                >
                  <CalendarPlus className="size-4" aria-hidden="true" />
                </button>
              ) : null}
            </div>
            {showDoctors && list.length > 0 ? (
              <ul className="flex flex-wrap gap-x-3 gap-y-1 border-b border-line px-3 py-2 text-xs text-ink-2" aria-label="By dentist">
                {doctors.map((d) => {
                  const count = list.filter((a) => a.doctor_id === d.id).length;
                  if (!count) return null;
                  return (
                    <li key={d.id} className="flex items-center gap-1">
                      <span aria-hidden="true" className="size-1.5 rounded-full" style={{ background: safeColour(d.colour) }} />
                      {d.name.replace(/^Dr\.?\s+/, "")} <span className="font-semibold text-ink tnum">{count}</span>
                    </li>
                  );
                })}
              </ul>
            ) : null}
            {blocks.map((e) => (
              <p key={e.id} className="hatch border-b border-line px-3 py-2 text-xs">
                <span className="font-semibold">{exceptionKind(e.kind).text}</span>
                {e.label ? `: ${e.label}` : ""}
                {e.doctor_id && showDoctors ? ` · ${byId.get(e.doctor_id)?.name ?? ""}` : ""}
              </p>
            ))}
            {list.length === 0 ? (
              <p className="px-3 py-4 text-sm text-ink-3">Nothing booked.</p>
            ) : (
              <ol className="divide-y divide-line">
                {list.map((a) => (
                  <li key={a.id}>
                    <AppointmentRow a={a} timeZone={timeZone} onOpen={onOpen} doctor={showDoctors ? byId.get(a.doctor_id) : undefined} dense />
                  </li>
                ))}
              </ol>
            )}
          </li>
        );
      })}
    </ol>
  );
}

export function CalendarPage() {
  const { timeZone: clinicZone, canWrite } = useStaff();
  const catalog = useCatalog();
  const [view, setView] = useState<View>("day");
  const [day, setDay] = useState<string | null>(null);
  /** The dentist shown on the phone day list and in the week view; "all" is week-only. */
  const [focusDoctor, setFocusDoctor] = useState<string | null>(null);
  const [open, setOpen] = useState<CalendarAppointment | null>(null);
  const [draft, setDraft] = useState<BookingDraft | null>(null);
  const now = useNow();

  // The day is chosen after mount so "today" is the clinic's today, not the server's.
  useEffect(() => {
    if (day === null) setDay(todayKey(clinicZone));
  }, [day, clinicZone]);

  const rangeStart = day ? (view === "day" ? day : startOfWeek(day)) : null;
  const days = useMemo(() => (rangeStart ? Array.from({ length: view === "day" ? 1 : 7 }, (_, i) => addDays(rangeStart, i)) : []), [rangeStart, view]);
  const key =
    rangeStart && days.length
      ? withQuery("/api/staff/calendar", { from: zonedToIso(rangeStart, clinicZone), to: zonedToIso(addDays(days[days.length - 1]!, 1), clinicZone) })
      : null;
  const calendar = useSWR<CalendarResponse>(key, { refreshInterval: 60_000, keepPreviousData: true });
  const data = calendar.data;
  const zone = data?.timezone ?? clinicZone;
  const doctors: CalendarDoctor[] = useMemo(() => data?.doctors ?? [], [data]);
  const appointments = useMemo(() => data?.appointments ?? [], [data]);
  const exceptions = useMemo(() => data?.exceptions ?? [], [data]);
  // Only appointments widen the grid; blocked time (all-day leave, holidays) is clipped to it,
  // so one dentist's day off doesn't stretch everyone's diary to midnight.
  const window = useMemo(() => visibleWindow(appointments, days, zone), [appointments, days, zone]);

  const holiday = exceptions.find((e) => e.doctor_id === null && e.kind === "holiday" && days.some((d) => clipToDay(e, d, zone)));
  const knownFocus = focusDoctor === ALL || doctors.some((d) => d.id === focusDoctor) ? focusDoctor : null;
  // Week view defaults to one dentist: a seven-day grid of every dentist's lanes is unreadable.
  const weekDoctor = knownFocus ?? doctors[0]?.id ?? ALL;
  const dayDoctor = knownFocus && knownFocus !== ALL ? knownFocus : (doctors[0]?.id ?? null);
  const doctorNames = useMemo(() => new Map(doctors.map((d) => [d.id, d.name])), [doctors]);

  const drawerAppointment: DrawerAppointment | null = open
    ? {
        ...open,
        doctor_name: doctorNames.get(open.doctor_id) ?? catalog.doctorNames.get(open.doctor_id) ?? "Unknown dentist",
      }
    : null;

  function shift(direction: -1 | 1) {
    if (!day) return;
    setDay(addDays(day, direction * (view === "day" ? 1 : 7)));
  }

  const title = !day
    ? "Calendar"
    : view === "day"
      ? formatDayKey(day, "long")
      : `${formatDayKey(days[0]!, "short")} – ${formatDayKey(days[6]!, "short")}`;
  const canBook = canWrite && !!catalog.publicDoctors && !!catalog.publicServices;
  // Memoised: the booking dialog resets its form whenever its doctors or services change.
  const bookableDoctors = useMemo(() => (catalog.publicDoctors ?? []).filter((d) => doctors.some((c) => c.id === d.id)), [catalog.publicDoctors, doctors]);
  const isBookable = (doctorId: string | null): doctorId is string => !!doctorId && bookableDoctors.some((d) => d.id === doctorId);

  /**
   * Open the booking dialog (with its dentist, date and time pickers) at a given slot, or at
   * the first free slot from `targetDay` on: a past or full day moves on to the next one.
   */
  function book(targetDay: string, doctorId: string | null, minute?: number) {
    const doctor = isBookable(doctorId) ? doctorId : bookableDoctors[0]?.id;
    if (!doctor) return;
    if (minute !== undefined) {
      setDraft({ doctorId: doctor, date: targetDay, minute });
      return;
    }
    const today = todayKey(zone);
    const start = targetDay < today ? today : targetDay;
    for (let i = 0; i < 14; i++) {
      const date = addDays(start, i);
      const free = firstFreeMinute(date, doctor, zone, window, appointments, exceptions, now);
      if (free !== null) {
        setDraft({ doctorId: doctor, date, minute: free });
        return;
      }
    }
    setDraft({ doctorId: doctor, date: start, minute: Math.max(window.start, BOOKING_START) });
  }

  function addBooking() {
    if (!day) return;
    const today = todayKey(zone);
    const target = view === "day" ? day : days.includes(today) ? today : days[0]!;
    book(target, view === "week" ? (weekDoctor === ALL ? null : weekDoctor) : dayDoctor);
  }

  const singleWeek = view === "week" && weekDoctor !== ALL;
  const gridHint = canBook ? <span className="max-md:hidden"> Click an empty slot to book, or use Add booking.</span> : null;
  const description = (
    <>
      Clinic time ({zoneAbbreviation(zone)}).
      {view === "day" || singleWeek ? gridHint : canBook ? " Choose a dentist to see their week as a grid." : null}
    </>
  );

  const doctorPicker = (includeAll: boolean, value: string | null) => (
    <Field label="Dentist" className="flex-row! items-center gap-2!">
      {(props) => (
        <Select {...props} value={value ?? ""} onChange={(e) => setFocusDoctor(e.target.value)} className="h-9 w-full text-sm sm:w-60">
          {doctors.map((d) => (
            <option key={d.id} value={d.id}>
              {d.name} ({appointments.filter((a) => a.doctor_id === d.id && a.status !== "cancelled" && days.some((k) => clipToDay(a, k, zone))).length})
            </option>
          ))}
          {includeAll ? <option value={ALL}>All dentists (summary)</option> : null}
        </Select>
      )}
    </Field>
  );

  return (
    <div>
      <PageHeader
        title={title}
        description={description}
        actions={
          <>
            <div className="flex items-center gap-1">
              <Button variant="secondary" size="sm" onClick={() => shift(-1)} aria-label={view === "day" ? "Previous day" : "Previous week"}>
                <ChevronLeft className="size-4" aria-hidden="true" />
              </Button>
              <Button variant="secondary" size="sm" onClick={() => setDay(todayKey(clinicZone))}>
                Today
              </Button>
              <Button variant="secondary" size="sm" onClick={() => shift(1)} aria-label={view === "day" ? "Next day" : "Next week"}>
                <ChevronRight className="size-4" aria-hidden="true" />
              </Button>
            </div>
            <Segmented
              label="Calendar view"
              value={view}
              onChange={setView}
              items={[
                { key: "day", label: "Day" },
                { key: "week", label: "Week" },
              ]}
            />
            {canWrite ? (
              <Button size="sm" disabled={!canBook || !day || bookableDoctors.length === 0} icon={<CalendarPlus className="size-4" aria-hidden="true" />} onClick={addBooking}>
                Add booking
              </Button>
            ) : null}
          </>
        }
      />

      {holiday ? (
        <p className="mb-4 rounded-md border border-line bg-sunk/70 px-3 py-2 text-sm">
          <span className="font-semibold">Clinic closed</span>
          {holiday.label ? `: ${holiday.label}` : ""} ({formatDayKey(dayKey(holiday.starts_at, zone), "short")})
        </p>
      ) : null}

      {data?.truncated ? (
        <p role="status" className="mb-4 text-sm text-ink-3">
          {TRUNCATED_NOTICE}
        </p>
      ) : null}

      {calendar.error && !data ? (
        <ErrorState
          message="The calendar couldn't be loaded. Check your connection and try again."
          action={
            <Button size="sm" variant="secondary" onClick={() => void calendar.mutate()}>
              Try again
            </Button>
          }
        />
      ) : !data || !day ? (
        <Loading label="Loading the calendar" className="rounded-lg border border-line bg-raised p-4">
          <div className="grid grid-cols-5 gap-3">
            {Array.from({ length: 5 }, (_, i) => (
              <Skeleton key={i} className="h-6" />
            ))}
          </div>
          <Skeleton className="mt-4 h-[28rem]" />
        </Loading>
      ) : doctors.length === 0 ? (
        <EmptyState title="No active dentists">Add or reactivate a dentist in Settings to see their diary here.</EmptyState>
      ) : view === "day" ? (
        <>
          {/* Phones and small tablets: one dentist at a time, as a list. */}
          <div className="md:hidden">
            <div className="mb-4">{doctorPicker(false, dayDoctor)}</div>
            <MobileDayList
              appointments={appointments.filter((a) => a.doctor_id === dayDoctor && clipToDay(a, day, zone))}
              exceptions={exceptionsFor(exceptions, dayDoctor).filter((e) => clipToDay(e, day, zone))}
              timeZone={zone}
              onOpen={setOpen}
            />
          </div>

          <div className={cx("hidden overflow-hidden rounded-lg border border-line bg-raised md:block", calendar.isValidating && "opacity-95")}>
            {appointments.length === 0 && !holiday ? (
              <p className="border-b border-line bg-paper/60 px-4 py-2 text-sm text-ink-2">No appointments on this day yet.</p>
            ) : null}
            <div className="max-h-[calc(100dvh-14rem)] overflow-auto">
              <div className="grid min-w-max" style={{ gridTemplateColumns: `4.25rem repeat(${doctors.length}, minmax(11rem, 1fr))` }}>
                <div className="sticky top-0 left-0 z-30 border-r border-b border-line bg-raised" />
                {doctors.map((d) => {
                  const count = appointments.filter((a) => a.doctor_id === d.id && a.status !== "cancelled").length;
                  const leave = exceptions.find((e) => e.doctor_id === d.id && clipToDay(e, day, zone));
                  return (
                    <div key={d.id} className="sticky top-0 z-20 flex items-center justify-between gap-2 border-r border-b border-line bg-raised px-3 py-2 last:border-r-0">
                      <div className="min-w-0">
                        <p className="flex items-center gap-2 truncate text-sm font-semibold">
                          <span aria-hidden="true" className="size-2 shrink-0 rounded-full" style={{ background: safeColour(d.colour) }} />
                          {d.name}
                        </p>
                        <p className="text-xs text-ink-2 tnum">{leave ? exceptionKind(leave.kind).text : `${count} booked`}</p>
                      </div>
                      {canBook && isBookable(d.id) ? (
                        <button
                          type="button"
                          onClick={() => book(day, d.id)}
                          className="rounded-sm p-1 text-ink-3 hover:bg-sunk hover:text-ink"
                          aria-label={`Add a booking for ${d.name}`}
                        >
                          <CalendarPlus className="size-4" aria-hidden="true" />
                        </button>
                      ) : null}
                    </div>
                  );
                })}
                <div className="sticky left-0 z-10 bg-raised">
                  <Gutter window={window} />
                </div>
                {doctors.map((d) => (
                  <Column
                    key={d.id}
                    label={`${d.name}, ${formatDayKey(day, "long")}`}
                    dayKeyValue={day}
                    timeZone={zone}
                    window={window}
                    appointments={appointments.filter((a) => a.doctor_id === d.id)}
                    exceptions={exceptionsFor(exceptions, d.id)}
                    now={now}
                    canBook={canBook && isBookable(d.id)}
                    onOpen={setOpen}
                    onBook={(minute) => book(day, d.id, minute)}
                  />
                ))}
              </div>
            </div>
          </div>
        </>
      ) : (
        <div className="flex flex-col gap-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            {doctorPicker(true, weekDoctor)}
            <p className="text-sm text-ink-2 tnum">
              {appointments.filter((a) => a.status !== "cancelled" && (weekDoctor === ALL || a.doctor_id === weekDoctor)).length} appointments this week
              {weekDoctor === ALL ? " across all dentists" : ""}
            </p>
          </div>

          {singleWeek ? (
            <>
              <div className="md:hidden">
                <WeekList
                  days={days}
                  appointments={appointments.filter((a) => a.doctor_id === weekDoctor)}
                  exceptions={exceptionsFor(exceptions, weekDoctor)}
                  doctors={doctors}
                  showDoctors={false}
                  timeZone={zone}
                  now={now}
                  onOpen={setOpen}
                  onDay={(d) => {
                    setDay(d);
                    setView("day");
                  }}
                  onBook={canBook && isBookable(weekDoctor) ? (d) => book(d, weekDoctor) : undefined}
                />
              </div>
              <div className="hidden overflow-hidden rounded-lg border border-line bg-raised md:block">
                <div className="max-h-[calc(100dvh-16rem)] overflow-auto">
                  <div className="grid min-w-max" style={{ gridTemplateColumns: `4.25rem repeat(7, minmax(8.5rem, 1fr))` }}>
                    <div className="sticky top-0 left-0 z-30 border-r border-b border-line bg-raised" />
                    {days.map((d) => {
                      const past = now !== null && d < dayKey(now, zone);
                      return (
                        <div key={d} className="sticky top-0 z-20 flex items-center justify-between gap-1 border-r border-b border-line bg-raised px-2 py-1.5 last:border-r-0">
                          <button
                            type="button"
                            onClick={() => {
                              setDay(d);
                              setView("day");
                            }}
                            className={cx("rounded-sm text-sm font-semibold hover:text-accent", now !== null && d === dayKey(now, zone) && "text-accent")}
                            aria-label={`${formatDayKey(d, "long")}: open the day view`}
                          >
                            {formatDayKey(d, "short")}
                          </button>
                          {canBook && isBookable(weekDoctor) && !past ? (
                            <button
                              type="button"
                              onClick={() => book(d, weekDoctor)}
                              className="rounded-sm p-1 text-ink-3 hover:bg-sunk hover:text-ink"
                              aria-label={`Add a booking for ${doctorNames.get(weekDoctor) ?? "this dentist"} on ${formatDayKey(d, "long")}`}
                            >
                              <CalendarPlus className="size-4" aria-hidden="true" />
                            </button>
                          ) : null}
                        </div>
                      );
                    })}
                    <div className="sticky left-0 z-10 bg-raised">
                      <Gutter window={window} />
                    </div>
                    {days.map((d) => (
                      <Column
                        key={d}
                        label={`${doctorNames.get(weekDoctor) ?? "Dentist"}, ${formatDayKey(d, "long")}`}
                        dayKeyValue={d}
                        timeZone={zone}
                        window={window}
                        appointments={appointments.filter((a) => a.doctor_id === weekDoctor)}
                        exceptions={exceptionsFor(exceptions, weekDoctor)}
                        now={now}
                        canBook={canBook && isBookable(weekDoctor)}
                        onOpen={setOpen}
                        onBook={(minute) => book(d, weekDoctor, minute)}
                      />
                    ))}
                  </div>
                </div>
              </div>
            </>
          ) : (
            <WeekList
              days={days}
              appointments={appointments}
              exceptions={exceptions}
              doctors={doctors}
              showDoctors
              timeZone={zone}
              now={now}
              onOpen={setOpen}
              onDay={(d) => {
                setDay(d);
                setView("day");
              }}
              onBook={canBook ? (d) => book(d, null) : undefined}
            />
          )}
        </div>
      )}

      {/* The legend explains the time grid; the phone lists and the all-dentists week summary
          use plain words and badges instead. */}
      {data && doctors.length > 0 && (view === "day" || singleWeek) ? <Legend className="max-md:hidden" /> : null}

      <AppointmentDrawer appointment={drawerAppointment} onClose={() => setOpen(null)} onChanged={() => void calendar.mutate()} />
      {canBook ? (
        <StaffBookingDialog
          draft={draft}
          doctors={bookableDoctors}
          services={catalog.publicServices ?? []}
          onClose={() => setDraft(null)}
          onBooked={() => void calendar.mutate()}
        />
      ) : null}
    </div>
  );
}

function Legend({ className }: { className?: string }) {
  return (
    <ul className={cx("mt-4 flex flex-wrap gap-x-5 gap-y-2 text-xs text-ink-2", className)} aria-label="Legend">
      <li className="flex items-center gap-1.5">
        <span className="size-1.5 rounded-full bg-ok" aria-hidden="true" /> Synced to calendar
      </li>
      <li className="flex items-center gap-1.5">
        <span className="size-1.5 rounded-full bg-warn" aria-hidden="true" /> Sync pending (dashed)
      </li>
      <li className="flex items-center gap-1.5">
        <span className="size-1.5 rounded-full bg-bad" aria-hidden="true" /> Needs review (outlined)
      </li>
      <li className="flex items-center gap-1.5">
        <span className="hatch inline-block h-3 w-5 rounded-sm border border-line-strong" aria-hidden="true" /> Cancelled or blocked time
      </li>
      <li className="flex items-center gap-1.5">
        <span className="inline-block h-0.5 w-5 bg-accent" aria-hidden="true" /> Now
      </li>
    </ul>
  );
}
