"use client";

import { useState } from "react";

import { api, errorMessage } from "@/lib/api/client";
import type { BookingStatus, StaffAppointment } from "@/lib/api/types";
import { appointmentStatus, channel as channelLabel, syncState } from "@/lib/labels";
import { formatLongDate, formatTime, zoneAbbreviation } from "@/lib/time";
import { LabelBadge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { useConfirm, type ConfirmOptions } from "@/components/ui/ConfirmDialog";
import { Drawer } from "@/components/ui/Modal";
import { useToast } from "@/components/ui/Toast";
import { useStaff } from "./StaffContext";

export interface DrawerAppointment {
  id: string;
  starts_at: string;
  ends_at: string;
  status: BookingStatus;
  sync_state?: string | null;
  channel?: string | null;
  service_name: string;
  doctor_name: string;
  patient_name?: string | null;
  phone_last2?: string | null;
  reference?: string | null;
}

/** Cancel and resolve (existing /v1/admin/appointments routes), with toasts. */
export function useAppointmentActions(onChanged: () => void) {
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);

  async function run(label: string, path: string, body?: unknown): Promise<boolean> {
    setBusy(label);
    try {
      await api<StaffAppointment>(path, { method: "POST", body: body ?? {} });
      onChanged();
      return true;
    } catch (error) {
      toast.show(errorMessage(error), "error");
      return false;
    } finally {
      setBusy(null);
    }
  }

  return {
    busy,
    cancel: async (id: string) => {
      const ok = await run("cancel", `/api/admin/appointments/${encodeURIComponent(id)}/cancel`);
      if (ok) toast.show("Appointment cancelled. The calendar event is being removed.");
      return ok;
    },
    resolve: async (id: string, action: "requeue" | "cancel") => {
      const ok = await run(action, `/api/admin/appointments/${encodeURIComponent(id)}/resolve`, { action });
      if (ok) toast.show(action === "requeue" ? "Calendar sync retried. It shows as pending until confirmed." : "Appointment cancelled.");
      return ok;
    },
  };
}

const ACTIVE = new Set<BookingStatus>(["pending_sync", "confirmed", "needs_review"]);

/** The confirmation shown before any appointment is cancelled. */
export function cancelConfirmation(
  a: Pick<DrawerAppointment, "starts_at" | "patient_name" | "service_name">,
  timeZone: string,
): ConfirmOptions {
  const who = a.patient_name ?? "this patient";
  return {
    title: "Cancel this appointment?",
    body: `${a.service_name} for ${who} on ${formatLongDate(a.starts_at, timeZone)} at ${formatTime(a.starts_at, timeZone)}. The time is released and the calendar event removed. This can't be undone; the patient would need to book again.`,
    confirmLabel: "Cancel appointment",
    cancelLabel: "Keep it",
  };
}

export function AppointmentDrawer({
  appointment,
  onClose,
  onChanged,
}: {
  appointment: DrawerAppointment | null;
  onClose: () => void;
  onChanged: () => void;
}) {
  const { canWrite, timeZone } = useStaff();
  const actions = useAppointmentActions(onChanged);
  const [confirm, confirmDialog] = useConfirm();

  const a = appointment;
  const started = a ? Date.parse(a.starts_at) <= Date.now() : false;

  async function cancel(target: DrawerAppointment) {
    if (!(await confirm(cancelConfirmation(target, timeZone)))) return;
    const ok = target.status === "needs_review" ? await actions.resolve(target.id, "cancel") : await actions.cancel(target.id);
    if (ok) onClose();
  }

  return (
    <>
      <Drawer
        open={a !== null}
        onClose={onClose}
        title={a ? a.service_name : "Appointment"}
        description={a ? `${formatLongDate(a.starts_at, timeZone)} · ${formatTime(a.starts_at, timeZone)} to ${formatTime(a.ends_at, timeZone)} ${zoneAbbreviation(timeZone, Date.parse(a.starts_at))}` : undefined}
        footer={
          a && canWrite && ACTIVE.has(a.status) ? (
            a.status === "needs_review" ? (
              <>
                <Button variant="danger" loading={actions.busy === "cancel"} disabled={!!actions.busy} onClick={() => void cancel(a)}>
                  Cancel appointment
                </Button>
                <Button loading={actions.busy === "requeue"} disabled={!!actions.busy || started} onClick={async () => (await actions.resolve(a.id, "requeue")) && onClose()}>
                  Retry calendar sync
                </Button>
              </>
            ) : (
              <Button variant="danger" loading={actions.busy === "cancel"} onClick={() => void cancel(a)} disabled={started || !!actions.busy}>
                {started ? "Already started" : "Cancel appointment"}
              </Button>
            )
          ) : undefined
        }
      >
        {a ? (
          <div className="flex flex-col gap-6">
            <div className="flex flex-wrap gap-2">
              <LabelBadge label={appointmentStatus(a.status)} />
              {a.sync_state ? <LabelBadge label={syncState(a.sync_state)} /> : null}
              {a.channel ? <LabelBadge label={{ ...channelLabel(a.channel), tone: "neutral" }} /> : null}
            </div>
            <dl className="grid grid-cols-[8rem_1fr] gap-x-4 gap-y-3 text-[0.95rem]">
              <dt className="text-ink-3">Patient</dt>
              <dd className="font-semibold">
                {a.patient_name ?? <span className="font-normal text-ink-3">Hidden for your role</span>}
                {a.phone_last2 ? <span className="ml-2 font-normal text-ink-3 tnum">phone ending {a.phone_last2}</span> : null}
              </dd>
              <dt className="text-ink-3">Dentist</dt>
              <dd>{a.doctor_name}</dd>
              <dt className="text-ink-3">Reference</dt>
              <dd className="font-mono tracking-wider">{a.reference ?? "—"}</dd>
            </dl>
            {a.status === "needs_review" ? (
              <p className="rounded-md border border-bad/25 bg-bad-tint/50 px-3 py-2 text-sm text-ink">
                The calendar write for this booking did not complete, so it is not confirmed. Retry the sync, or cancel and call the patient.
              </p>
            ) : null}
            {a.status === "pending_sync" ? (
              <p className="rounded-md border border-warn/25 bg-warn-tint/50 px-3 py-2 text-sm text-ink">
                Waiting for the calendar to confirm. Don&apos;t tell the patient it is confirmed yet.
              </p>
            ) : null}
          </div>
        ) : null}
      </Drawer>
      {confirmDialog}
    </>
  );
}
