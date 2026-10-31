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

function Gutter({ window }: { window: TimeWindow }) {
  return (
    <div className="relative border-r border-line" style={{ height: (window.end - window.start) * PX }} aria-hidden="true">
      {gridLines(window)
        .filter((line) => line.major)
        .map((line) => (
          <span key={line.minute} className="absolute right-2 -translate-y-1/2 text-[0.7rem] text-ink-3 tnum" style={{ top: (line.minute - window.start) * PX }}>
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

  function onClick(event: MouseEvent<HTMLDivElement>) {
    if (!canBook || !onBook || past || event.target !== event.currentTarget) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const minute = slotAt((event.clientY - rect.top) / PX, window);
    if (minute === null) return;
    if (isCovered(minute, dayKeyValue, timeZone, active) || isCovered(minute, dayKeyValue, timeZone, exceptions)) return;
    if (now !== null && Date.parse(zonedToIso(`${dayKeyValue}T${String(Math.floor(minute / 60)).padStart(2, "0")}:${String(minute % 60).padStart(2, "0")}`, timeZone)) < now) return;
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
          <span className="text-[0.7rem] font-semibold text-ink-2">
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
          <button type="button" onClick={() => onOpen(a)} className="flex w-full items-start gap-3 px-4 py-3 text-left hover:bg-sunk/50">
            <span aria-hidden="true" className="mt-1 h-9 w-1 shrink-0 rounded-full" style={{ background: a.colour ?? "var(--color-ink-3)" }} />
            <span className="w-16 shrink-0 font-semibold tnum">{formatTime(a.starts_at, timeZone)}</span>
            <span className="min-w-0 flex-1">
              <span className={cx("block truncate font-semibold", a.status === "cancelled" && "line-through opacity-70")}>{a.patient_name ?? "Patient"}</span>
              <span className="block truncate text-sm text-ink-2">{a.service_name}</span>
            </span>
            {a.status !== "confirmed" ? <LabelBadge label={appointmentStatus(a.status)} /> : null}
          </button>
        </li>
      ))}
    </ol>
  );
}

export function CalendarPage() {
  const { timeZone: clinicZone, canWrite } = useStaff();
  const catalog = useCatalog();
  const [view, setView] = useState<View>("day");
  const [day, setDay] = useState<string | null>(null);
  const [mobileDoctor, setMobileDoctor] = useState<string | null>(null);
  const [weekDoctor, setWeekDoctor] = useState<string>("all");
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
  const window = useMemo(() => visibleWindow([...appointments, ...exceptions.filter((e) => e.kind !== "holiday")], days, zone), [appointments, exceptions, days, zone]);

  const holiday = exceptions.find((e) => e.doctor_id === null && e.kind === "holiday" && days.some((d) => clipToDay(e, d, zone)));
  const selectedMobileDoctor = mobileDoctor ?? doctors[0]?.id ?? null;
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
  const bookableDoctors = (catalog.publicDoctors ?? []).filter((d) => doctors.some((c) => c.id === d.id));

  return (
    <div>
      <PageHeader
        title={title}
        description={`Clinic time (${zoneAbbreviation(zone)}). ${canWrite ? "Click an empty slot to book." : ""}`}
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
              <Button
                size="sm"
                disabled={!canBook || !day || bookableDoctors.length === 0}
                icon={<CalendarPlus className="size-4" aria-hidden="true" />}
                onClick={() => day && setDraft({ doctorId: selectedMobileDoctor ?? bookableDoctors[0]!.id, date: day, minute: 10 * 60 })}
              >
                New booking
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
          {/* Phones and small tablets: one dentist at a time. */}
          <div className="md:hidden">
            <Field label="Dentist" className="mb-4">
              {(props) => (
                <Select {...props} value={selectedMobileDoctor ?? ""} onChange={(e) => setMobileDoctor(e.target.value)}>
                  {doctors.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.name} ({appointments.filter((a) => a.doctor_id === d.id && a.status !== "cancelled").length})
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <MobileDayList
              appointments={appointments.filter((a) => a.doctor_id === selectedMobileDoctor && clipToDay(a, day, zone))}
              exceptions={exceptionsFor(exceptions, selectedMobileDoctor).filter((e) => clipToDay(e, day, zone))}
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
                          <span aria-hidden="true" className="size-2 shrink-0 rounded-full" style={{ background: d.colour ?? "var(--color-ink-3)" }} />
                          {d.name}
                        </p>
                        <p className="text-xs text-ink-3 tnum">{leave ? exceptionKind(leave.kind).text : `${count} booked`}</p>
                      </div>
                      {canBook ? (
                        <button
                          type="button"
                          onClick={() => setDraft({ doctorId: d.id, date: day, minute: window.start + 60 })}
                          className="rounded-sm p-1 text-ink-3 hover:bg-sunk hover:text-ink"
                          aria-label={`New booking for ${d.name}`}
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
                    canBook={canBook}
                    onOpen={setOpen}
                    onBook={(minute) => setDraft({ doctorId: d.id, date: day, minute })}
                  />
                ))}
              </div>
            </div>
          </div>
        </>
      ) : (
        <div className="overflow-hidden rounded-lg border border-line bg-raised">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-4 py-2">
            <Field label="Show" className="flex-row! items-center gap-2!">
              {(props) => (
                <Select {...props} value={weekDoctor} onChange={(e) => setWeekDoctor(e.target.value)} className="h-8 w-56 text-sm">
                  <option value="all">All dentists</option>
                  {doctors.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.name}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <p className="text-sm text-ink-3 tnum">
              {appointments.filter((a) => a.status !== "cancelled" && (weekDoctor === "all" || a.doctor_id === weekDoctor)).length} appointments this week
            </p>
          </div>
          <div className="max-h-[calc(100dvh-16rem)] overflow-auto">
            <div className="grid min-w-max" style={{ gridTemplateColumns: `4.25rem repeat(7, minmax(8.5rem, 1fr))` }}>
              <div className="sticky top-0 left-0 z-30 border-r border-b border-line bg-raised" />
              {days.map((d) => (
                <div key={d} className="sticky top-0 z-20 border-r border-b border-line bg-raised px-2 py-1.5 last:border-r-0">
                  <button
                    type="button"
                    onClick={() => {
                      setDay(d);
                      setView("day");
                    }}
                    className={cx("rounded-sm text-sm font-semibold hover:text-accent", now !== null && d === dayKey(now, zone) && "text-accent")}
                  >
                    {formatDayKey(d, "short")}
                  </button>
                </div>
              ))}
              <div className="sticky left-0 z-10 bg-raised">
                <Gutter window={window} />
              </div>
              {days.map((d) => (
                <Column
                  key={d}
                  label={formatDayKey(d, "long")}
                  dayKeyValue={d}
                  timeZone={zone}
                  window={window}
                  appointments={appointments.filter((a) => weekDoctor === "all" || a.doctor_id === weekDoctor)}
                  exceptions={exceptionsFor(exceptions, weekDoctor === "all" ? null : weekDoctor).filter((e) => weekDoctor !== "all" || e.doctor_id === null)}
                  now={now}
                  canBook={false}
                  onOpen={setOpen}
                  doctorNames={doctorNames}
                />
              ))}
            </div>
          </div>
        </div>
      )}

      <Legend />

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

function Legend() {
  return (
    <ul className="mt-4 flex flex-wrap gap-x-5 gap-y-2 text-xs text-ink-2" aria-label="Legend">
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
