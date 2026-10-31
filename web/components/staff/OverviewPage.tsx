"use client";

import { ArrowRight } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import useSWR from "swr";

import { withQuery } from "@/lib/api/client";
import type { CalendarResponse, CallbackItem, Channel, StaffAppointment, StaffStats } from "@/lib/api/types";
import { useCatalog } from "@/lib/catalog";
import { cx } from "@/lib/cx";
import { appointmentStatus, channel as channelLabel } from "@/lib/labels";
import { addDays, dayRange, formatDayKey, formatLongDate, formatTime, todayKey, zonedToIso } from "@/lib/time";
import { LabelBadge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { EmptyState, ErrorState, Loading, Skeleton } from "@/components/ui/States";
import { AppointmentDrawer, useAppointmentActions, type DrawerAppointment } from "./AppointmentDrawer";
import { PageHeader, Panel } from "./PageHeader";
import { useStaff } from "./StaffContext";

const CHANNELS: Channel[] = ["voice", "web", "chat", "staff"];

function Stat({ label, value, href, tone }: { label: string; value: number | undefined; href?: string; tone?: "bad" | "warn" }) {
  const body = (
    <>
      <p className="text-sm text-ink-2">{label}</p>
      <p className={cx("mt-1 font-display text-4xl tnum", tone === "bad" && value ? "text-bad" : tone === "warn" && value ? "text-warn" : "text-ink")}>
        {value === undefined ? <Skeleton className="h-10 w-12" /> : value}
      </p>
    </>
  );
  return href ? (
    <Link href={href} className="rounded-lg border border-line bg-raised p-4 transition-colors hover:border-line-strong">
      {body}
    </Link>
  ) : (
    <div className="rounded-lg border border-line bg-raised p-4">{body}</div>
  );
}

export function OverviewPage() {
  const { timeZone, canWrite } = useStaff();
  const catalog = useCatalog();
  const [today, setToday] = useState<string | null>(null);
  const [open, setOpen] = useState<DrawerAppointment | null>(null);
  useEffect(() => setToday(todayKey(timeZone)), [timeZone]);

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
  const doctorName = (id: string) => schedule.data?.doctors.find((d) => d.id === id)?.name ?? catalog.doctorNames.get(id) ?? "Unknown dentist";
  const totalByChannel = stats.data ? CHANNELS.reduce((sum, c) => sum + (stats.data!.by_channel[c] ?? 0), 0) : 0;

  return (
    <div>
      <PageHeader title="Today" description={today ? formatDayKey(today, "long") : undefined} />

      {stats.error ? <ErrorState className="mb-6" message="Counts couldn't be loaded." /> : null}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Appointments today" value={stats.data?.today} href="/staff/calendar" />
        <Stat label="Next 7 days" value={stats.data?.week} href="/staff/appointments" />
        <Stat label="Need review" value={stats.data?.needs_review} tone="bad" href="#review" />
        <Stat label="Open call-backs" value={stats.data?.open_callbacks} tone="warn" href="/staff/callbacks" />
      </div>

      {stats.data && totalByChannel > 0 ? (
        <section aria-labelledby="channels-title" className="mt-6 rounded-lg border border-line bg-raised p-4">
          <h2 id="channels-title" className="text-sm font-semibold">
            How this week&apos;s appointments were booked
          </h2>
          <div className="mt-3 flex h-2.5 overflow-hidden rounded-full bg-sunk" aria-hidden="true">
            {CHANNELS.map((c, i) => (
              <span key={c} style={{ width: `${((stats.data!.by_channel[c] ?? 0) / totalByChannel) * 100}%`, opacity: 1 - i * 0.2 }} className="bg-ink" />
            ))}
          </div>
          <ul className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-sm">
            {CHANNELS.map((c) => (
              <li key={c} className="text-ink-2">
                {channelLabel(c).text} <span className="font-semibold text-ink tnum">{stats.data!.by_channel[c] ?? 0}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <div className="mt-6 grid gap-6 xl:grid-cols-[1.4fr_1fr]">
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
                const ended = Date.parse(a.ends_at) < Date.now();
                return (
                  <li key={a.id}>
                    <button
                      type="button"
                      onClick={() => setOpen({ ...a, doctor_name: doctorName(a.doctor_id) })}
                      className={cx("flex w-full items-center gap-3 px-4 py-2.5 text-left hover:bg-sunk/50", ended && "text-ink-3")}
                    >
                      <span className="w-[4.5rem] shrink-0 text-sm font-semibold tnum">{formatTime(a.starts_at, timeZone)}</span>
                      <span aria-hidden="true" className="h-6 w-1 shrink-0 rounded-full" style={{ background: a.colour ?? "var(--color-ink-3)" }} />
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
                        <Button size="sm" variant="danger" loading={actions.busy === "cancel"} disabled={!!actions.busy} onClick={() => void actions.resolve(a.id, "cancel")}>
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
    </div>
  );
}
