"use client";

import { ArrowRight } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import useSWR from "swr";

import { withQuery } from "@/lib/api/client";
import type { CalendarAppointment, CalendarResponse, CallbackItem, Channel, StaffAppointment, StaffStats } from "@/lib/api/types";
import { safeColour } from "@/lib/calendar";
import { useCatalog } from "@/lib/catalog";
import { cx } from "@/lib/cx";
import { appointmentStatus, channel as channelLabel } from "@/lib/labels";
import { addDays, dayRange, formatDayKey, formatLongDate, formatTime, todayKey, zonedToIso } from "@/lib/time";
import { LabelBadge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { useConfirm } from "@/components/ui/ConfirmDialog";
import { EmptyState, ErrorState, Loading, Skeleton } from "@/components/ui/States";
import { AppointmentDrawer, cancelConfirmation, useAppointmentActions, type DrawerAppointment } from "./AppointmentDrawer";
import { Panel } from "./PageHeader";
import { useStaff } from "./StaffContext";

const CHANNELS: Channel[] = ["voice", "web", "chat", "staff"];

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** "in 25 min", "in 2 h 10 min", or "now". */
export function startsIn(startsAt: string, now = Date.now()): string {
  const minutes = Math.round((Date.parse(startsAt) - now) / 60_000);
  if (minutes <= 0) return "now";
  if (minutes < 60) return `in ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `in ${hours} h ${rest} min` : `in ${hours} h`;
}

/** An inline figure in the summary sentence, linking to where it can be acted on. */
function Figure({ href, children, tone }: { href: string; children: ReactNode; tone?: "bad" | "warn" }) {
  return (
    <Link
      href={href}
      className={cx(
        "font-semibold underline decoration-1 underline-offset-[5px] tnum hover:decoration-2",
        tone === "bad" ? "text-bad decoration-bad/40" : tone === "warn" ? "text-warn decoration-warn/40" : "text-ink decoration-line-strong",
      )}
    >
      {children}
    </Link>
  );
}

/**
 * Today, editorially: one hero number (appointments today) with the next patient beside it,
 * and the other counts as a sentence rather than a row of tiles. Every number is a real count
 * from /api/staff/stats.
 */
function TodayHero({
  stats,
  statsError,
  next,
  anyToday,
  scheduleLoaded,
  doctorName,
  timeZone,
  onOpen,
}: {
  stats: StaffStats | undefined;
  statsError: boolean;
  next: CalendarAppointment | null;
  anyToday: boolean;
  scheduleLoaded: boolean;
  doctorName: (id: string) => string;
  timeZone: string;
  onOpen: (a: CalendarAppointment) => void;
}) {
  const channelTotal = stats ? CHANNELS.reduce((sum, c) => sum + (stats.by_channel[c] ?? 0), 0) : 0;
  return (
    <section aria-label="Today at a glance" className="border-b border-line pb-8">
      <div className="grid gap-8 md:grid-cols-[auto_1fr] md:items-end md:gap-14">
        <div>
          {stats ? (
            <p className="font-display text-[5.5rem] leading-[0.85] tracking-[-0.03em] text-ink tnum sm:text-[7rem]">{stats.today}</p>
          ) : statsError ? (
            <p className="font-display text-[5.5rem] leading-[0.85] text-ink-3">–</p>
          ) : (
            <Skeleton className="h-20 w-28 sm:h-24" />
          )}
          <p className="mt-3 text-lg text-ink-2">
            {stats ? (stats.today === 1 ? "appointment today" : "appointments today") : "appointments today"}
          </p>
        </div>

        <div className="max-w-xl border-l-2 border-accent pl-5">
          <p className="text-xs font-semibold tracking-[0.14em] text-accent uppercase">Next patient</p>
          {!scheduleLoaded ? (
            <Loading label="Loading the next patient" className="mt-2 flex flex-col gap-2">
              <Skeleton className="h-7 w-64" />
              <Skeleton className="h-5 w-48" />
            </Loading>
          ) : next ? (
            <button type="button" onClick={() => onOpen(next)} className="group mt-1 block text-left">
              <span className="block font-display text-2xl leading-snug text-ink sm:text-3xl">
                <span className="tnum">{formatTime(next.starts_at, timeZone)}</span>
                <span className="text-ink-3"> · </span>
                <span className="group-hover:underline group-hover:decoration-1 group-hover:underline-offset-4">{next.patient_name ?? "Patient"}</span>
              </span>
              <span className="mt-1 block text-ink-2">
                {next.service_name} with {doctorName(next.doctor_id)} · {startsIn(next.starts_at)}
              </span>
            </button>
          ) : (
            <p className="mt-1 font-display text-2xl text-ink-2">{anyToday ? "No one else is booked today." : "The diary is clear today."}</p>
          )}
        </div>
      </div>

      {statsError ? (
        <p className="mt-8 text-ink-2">The other counts couldn&apos;t be loaded just now.</p>
      ) : stats ? (
        <p className="mt-8 max-w-3xl text-lg leading-relaxed text-ink-2">
          <Figure href="/staff/appointments">{plural(stats.week, "appointment", "appointments")}</Figure> booked for the next seven days.{" "}
          {stats.needs_review ? (
            <>
              <Figure href="#review" tone="bad">
                {plural(stats.needs_review, "booking needs", "bookings need")} review
              </Figure>
              ,{" "}
            </>
          ) : (
            "Nothing needs review, "
          )}
          {stats.open_callbacks ? (
            <>
              and{" "}
              <Figure href="/staff/callbacks" tone="warn">
                {plural(stats.open_callbacks, "patient is", "patients are")} waiting
              </Figure>{" "}
              for a call-back.
            </>
          ) : (
            "and no one is waiting for a call-back."
          )}
        </p>
      ) : (
        <Skeleton className="mt-8 h-6 w-full max-w-2xl" />
      )}

      {stats && channelTotal > 0 ? (
        <p className="mt-3 text-sm text-ink-3">
          This week by channel:{" "}
          {CHANNELS.map((c, i) => (
            <span key={c}>
              {i > 0 ? <span aria-hidden="true"> · </span> : null}
              {channelLabel(c).text} <span className="font-semibold text-ink-2 tnum">{stats.by_channel[c] ?? 0}</span>
            </span>
          ))}
        </p>
      ) : null}
    </section>
  );
}

export function OverviewPage() {
  const { timeZone, canWrite } = useStaff();
  const catalog = useCatalog();
  const [today, setToday] = useState<string | null>(null);
  const [open, setOpen] = useState<DrawerAppointment | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [confirm, confirmDialog] = useConfirm();
  useEffect(() => setToday(todayKey(timeZone)), [timeZone]);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 60_000);
    return () => window.clearInterval(timer);
  }, []);

  const stats = useSWR<StaffStats>("/api/staff/stats", { refreshInterval: 60_000 });
  const range = today ? dayRange(today, timeZone) : null;
  const schedule = useSWR<CalendarResponse>(range ? withQuery("/api/staff/calendar", range) : null, { refreshInterval: 60_000 });
  const review = useSWR<StaffAppointment[]>(
    today ? withQuery("/api/admin/appointments", { status: "needs_review", from: zonedToIso(addDays(today, -1), timeZone), to: zonedToIso(addDays(today, 90), timeZone), limit: 50 }) : null,
  );
  const callbacks = useSWR<CallbackItem[]>(withQuery("/api/staff/callbacks", { status: "open" }));
  const actions = useAppointmentActions(() => {
    void review.mutate();
    void stats.mutate();
    void schedule.mutate();
  });

  const upcoming = useMemo(() => {
    const list = (schedule.data?.appointments ?? []).filter((a) => a.status !== "cancelled");
    return list.sort((a, b) => Date.parse(a.starts_at) - Date.parse(b.starts_at));
  }, [schedule.data]);
  const next = upcoming.find((a) => Date.parse(a.starts_at) > now) ?? null;
  const doctorName = (id: string) => schedule.data?.doctors.find((d) => d.id === id)?.name ?? catalog.doctorNames.get(id) ?? "Unknown dentist";
  const openDrawer = (a: CalendarAppointment) => setOpen({ ...a, doctor_name: doctorName(a.doctor_id) });

  async function cancelReviewed(a: StaffAppointment) {
    const service = catalog.serviceNames.get(a.service_id) ?? "Appointment";
    if (!(await confirm(cancelConfirmation({ ...a, service_name: service }, timeZone)))) return;
    await actions.resolve(a.id, "cancel");
  }

  return (
    <div>
      <div className="mb-8">
        <h1 className="font-display text-3xl leading-tight text-ink">Today</h1>
        <p className="mt-1 text-ink-2">{today ? formatDayKey(today, "long") : " "}</p>
      </div>

      <TodayHero
        stats={stats.data}
        statsError={!!stats.error}
        next={next}
        anyToday={upcoming.length > 0}
        scheduleLoaded={!!schedule.data || !!schedule.error}
        doctorName={doctorName}
        timeZone={timeZone}
        onOpen={openDrawer}
      />

      <div className="mt-8 grid gap-6 xl:grid-cols-[1.4fr_1fr]">
        <Panel
          title="Today's schedule"
          action={
            <Link href="/staff/calendar" className="inline-flex items-center gap-1 text-sm font-semibold text-accent">
              Calendar <ArrowRight className="size-3.5" aria-hidden="true" />
            </Link>
          }
        >
          {schedule.error ? (
            <ErrorState className="m-4" message="Today's schedule couldn't be loaded." />
          ) : !schedule.data ? (
            <Loading label="Loading today's schedule" className="flex flex-col gap-2 p-4">
              {Array.from({ length: 5 }, (_, i) => (
                <Skeleton key={i} className="h-10" />
              ))}
            </Loading>
          ) : upcoming.length === 0 ? (
            <EmptyState className="m-4" title="No appointments today">
              The diary is clear. New bookings from the website, chat and phone appear here as they come in.
            </EmptyState>
          ) : (
            <ol className="divide-y divide-line">
              {upcoming.map((a) => {
                const ended = Date.parse(a.ends_at) < now;
                return (
                  <li key={a.id}>
                    <button
                      type="button"
                      onClick={() => openDrawer(a)}
                      className={cx("flex w-full items-center gap-3 px-4 py-2.5 text-left hover:bg-sunk/50", ended && "text-ink-3")}
                    >
                      <span className="w-[4.5rem] shrink-0 text-sm font-semibold tnum">{formatTime(a.starts_at, timeZone)}</span>
                      <span aria-hidden="true" className="h-6 w-1 shrink-0 rounded-full" style={{ background: safeColour(a.colour) }} />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate font-semibold">{a.patient_name ?? "Patient"}</span>
                        <span className="block truncate text-sm text-ink-2">
                          {a.service_name} · {doctorName(a.doctor_id)}
                        </span>
                      </span>
                      <span className="hidden text-xs text-ink-3 sm:inline">{channelLabel(a.channel).text}</span>
                      {a.status !== "confirmed" ? <LabelBadge label={appointmentStatus(a.status)} /> : null}
                    </button>
                  </li>
                );
              })}
            </ol>
          )}
        </Panel>

        <div className="flex flex-col gap-6">
          <Panel title={<span id="review">Needs review</span>}>
            {review.error ? (
              <ErrorState className="m-4" message="The review queue couldn't be loaded." />
            ) : !review.data ? (
              <Loading label="Loading the review queue" className="p-4">
                <Skeleton className="h-16" />
              </Loading>
            ) : review.data.length === 0 ? (
              <p className="px-4 py-6 text-sm text-ink-2">Nothing to review. Every booking has reached the calendar or is on its way.</p>
            ) : (
              <ul className="divide-y divide-line">
                {review.data.map((a) => (
                  <li key={a.id} className="flex flex-col gap-2 px-4 py-3">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="truncate font-semibold">{a.patient_name ?? "Patient"}</p>
                        <p className="text-sm text-ink-2 tnum">
                          {formatLongDate(a.starts_at, timeZone)}, {formatTime(a.starts_at, timeZone)} · {catalog.serviceNames.get(a.service_id) ?? "Service"}
                        </p>
                      </div>
                      <span className="font-mono text-xs tracking-wider text-ink-3">{a.booking_reference}</span>
                    </div>
                    {canWrite ? (
                      <div className="flex gap-2">
                        <Button size="sm" variant="secondary" loading={actions.busy === "requeue"} disabled={!!actions.busy} onClick={() => void actions.resolve(a.id, "requeue")}>
                          Retry sync
                        </Button>
                        <Button size="sm" variant="danger" loading={actions.busy === "cancel"} disabled={!!actions.busy} onClick={() => void cancelReviewed(a)}>
                          Cancel
                        </Button>
                      </div>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </Panel>

          <Panel
            title="Open call-backs"
            action={
              <Link href="/staff/callbacks" className="inline-flex items-center gap-1 text-sm font-semibold text-accent">
                Queue <ArrowRight className="size-3.5" aria-hidden="true" />
              </Link>
            }
          >
            {!callbacks.data ? (
              <Loading label="Loading call-backs" className="p-4">
                <Skeleton className="h-12" />
              </Loading>
            ) : callbacks.data.length === 0 ? (
              <p className="px-4 py-6 text-sm text-ink-2">No one is waiting for a call.</p>
            ) : (
              <ul className="divide-y divide-line">
                {callbacks.data.slice(0, 4).map((c) => (
                  <li key={c.id} className="px-4 py-3">
                    <p className="font-semibold">{c.name}</p>
                    {c.reason ? <p className="truncate text-sm text-ink-2">{c.reason}</p> : null}
                  </li>
                ))}
              </ul>
            )}
          </Panel>
        </div>
      </div>

      <AppointmentDrawer appointment={open} onClose={() => setOpen(null)} onChanged={() => void Promise.all([schedule.mutate(), stats.mutate(), review.mutate()])} />
      {confirmDialog}
    </div>
  );
}
