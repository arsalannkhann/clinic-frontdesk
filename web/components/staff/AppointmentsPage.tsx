"use client";

import { useEffect, useState } from "react";
import useSWR from "swr";

import { withQuery } from "@/lib/api/client";
import type { StaffAppointment } from "@/lib/api/types";
import { useCatalog } from "@/lib/catalog";
import { cx } from "@/lib/cx";
import { appointmentStatus } from "@/lib/labels";
import { addDays, formatShortDate, formatTime, todayKey, zonedToIso } from "@/lib/time";
import { LabelBadge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Field, Input, Select } from "@/components/ui/Field";
import { EmptyState, ErrorState, Loading, Skeleton } from "@/components/ui/States";
import { AppointmentDrawer, type DrawerAppointment } from "./AppointmentDrawer";
import { PageHeader } from "./PageHeader";
import { useStaff } from "./StaffContext";

const STATUSES = [
  { value: "", label: "All statuses" },
  { value: "confirmed", label: "Confirmed" },
  { value: "pending_sync", label: "Pending sync" },
  { value: "needs_review", label: "Needs review" },
  { value: "cancelled", label: "Cancelled" },
];

export function AppointmentsPage() {
  const { timeZone } = useStaff();
  const catalog = useCatalog();
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [status, setStatus] = useState("");
  const [open, setOpen] = useState<DrawerAppointment | null>(null);

  useEffect(() => {
    const today = todayKey(timeZone);
    setFrom(today);
    setTo(addDays(today, 7));
  }, [timeZone]);

  const valid = /^\d{4}-\d{2}-\d{2}$/.test(from) && /^\d{4}-\d{2}-\d{2}$/.test(to) && to >= from;
  const key = valid
    ? withQuery("/api/admin/appointments", {
        from: zonedToIso(from, timeZone),
        to: zonedToIso(addDays(to, 1), timeZone),
        status: status || undefined,
        limit: 500,
      })
    : null;
  const list = useSWR<StaffAppointment[]>(key);

  function drawer(a: StaffAppointment): DrawerAppointment {
    return {
      ...a,
      reference: a.booking_reference,
      service_name: catalog.serviceNames.get(a.service_id) ?? "Service",
      doctor_name: catalog.doctorNames.get(a.doctor_id) ?? "Unknown dentist",
    };
  }

  return (
    <div>
      <PageHeader title="Appointments" description="Every channel's bookings, in clinic time." />
      <div className="mb-5 flex flex-wrap items-end gap-3">
        <Field label="From">{(props) => <Input {...props} type="date" value={from} onChange={(e) => setFrom(e.target.value)} className="w-44" />}</Field>
        <Field label="To" error={from && to && to < from ? "End before start" : null}>
          {(props) => <Input {...props} type="date" value={to} min={from} onChange={(e) => setTo(e.target.value)} className="w-44" />}
        </Field>
        <Field label="Status">
          {(props) => (
            <Select {...props} value={status} onChange={(e) => setStatus(e.target.value)} className="w-48">
              {STATUSES.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </Select>
          )}
        </Field>
      </div>

      {list.error ? (
        <ErrorState
          message="Appointments couldn't be loaded."
          action={
            <Button size="sm" variant="secondary" onClick={() => void list.mutate()}>
              Try again
            </Button>
          }
        />
      ) : !list.data ? (
        <Loading label="Loading appointments" className="flex flex-col gap-2">
          {Array.from({ length: 8 }, (_, i) => (
            <Skeleton key={i} className="h-12" />
          ))}
        </Loading>
      ) : list.data.length === 0 ? (
        <EmptyState title="No appointments in this range">Try a wider date range or a different status.</EmptyState>
      ) : (
        <>
          <p className="mb-2 text-sm text-ink-3 tnum">
            {list.data.length} appointment{list.data.length === 1 ? "" : "s"}
            {list.data.length >= 500 ? " (showing the first 500; narrow the range)" : ""}
          </p>
          <div className="hidden overflow-hidden rounded-lg border border-line bg-raised md:block">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Appointments</caption>
              <thead className="border-b border-line bg-paper/60 text-xs tracking-wide text-ink-3 uppercase">
                <tr>
                  <th scope="col" className="px-4 py-2.5 font-semibold">When</th>
                  <th scope="col" className="px-4 py-2.5 font-semibold">Patient</th>
                  <th scope="col" className="px-4 py-2.5 font-semibold">Service</th>
                  <th scope="col" className="px-4 py-2.5 font-semibold">Dentist</th>
                  <th scope="col" className="px-4 py-2.5 font-semibold">Reference</th>
                  <th scope="col" className="px-4 py-2.5 font-semibold">Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {list.data.map((a) => (
                  <tr key={a.id} className={cx("hover:bg-sunk/40", a.status === "cancelled" && "text-ink-3")}>
                    <td className="px-4 py-2.5 whitespace-nowrap tnum">
                      <button type="button" onClick={() => setOpen(drawer(a))} className="text-left font-semibold underline-offset-4 hover:underline">
                        {formatShortDate(a.starts_at, timeZone)}, {formatTime(a.starts_at, timeZone)}
                      </button>
                    </td>
                    <td className="px-4 py-2.5">
                      {a.patient_name ?? <span className="text-ink-3">Hidden</span>}
                      {a.phone_last2 ? <span className="ml-2 text-xs text-ink-3 tnum">··{a.phone_last2}</span> : null}
                    </td>
                    <td className="px-4 py-2.5">{catalog.serviceNames.get(a.service_id) ?? "—"}</td>
                    <td className="px-4 py-2.5">{catalog.doctorNames.get(a.doctor_id) ?? "—"}</td>
                    <td className="px-4 py-2.5 font-mono text-xs tracking-wider">{a.booking_reference}</td>
                    <td className="px-4 py-2.5">
                      <LabelBadge label={appointmentStatus(a.status)} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <ul className="flex flex-col divide-y divide-line rounded-lg border border-line bg-raised md:hidden">
            {list.data.map((a) => (
              <li key={a.id}>
                <button type="button" onClick={() => setOpen(drawer(a))} className="flex w-full items-start justify-between gap-3 px-4 py-3 text-left">
                  <span className="min-w-0">
                    <span className="block font-semibold tnum">
                      {formatShortDate(a.starts_at, timeZone)}, {formatTime(a.starts_at, timeZone)}
                    </span>
                    <span className="block truncate text-sm text-ink-2">
                      {a.patient_name ?? "Patient"} · {catalog.serviceNames.get(a.service_id) ?? "Service"}
                    </span>
                  </span>
                  <LabelBadge label={appointmentStatus(a.status)} />
                </button>
              </li>
            ))}
          </ul>
        </>
      )}

      <AppointmentDrawer appointment={open} onClose={() => setOpen(null)} onChanged={() => void list.mutate()} />
    </div>
  );
}
