"use client";

import { useEffect, useState, type FormEvent } from "react";

import { api, errorMessage, newIdempotencyKey } from "@/lib/api/client";
import type { BookingResult, PublicDoctor, PublicService } from "@/lib/api/types";
import { validateDetails } from "@/lib/booking";
import { servicesForDoctor } from "@/lib/catalog";
import { appointmentStatus } from "@/lib/labels";
import { toIndianE164 } from "@/lib/phone";
import { formatDateTime, hhmm, minutesLabel, zonedToIso } from "@/lib/time";
import { LabelBadge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Field, Input, PhoneInput, Select, Textarea } from "@/components/ui/Field";
import { Dialog } from "@/components/ui/Modal";
import { useStaff } from "./StaffContext";

export interface BookingDraft {
  doctorId: string;
  date: string; // clinic day key
  minute: number; // minutes since local midnight
}

const TIMES = Array.from({ length: (21 - 7) * 4 }, (_, i) => 7 * 60 + i * 15);

export function StaffBookingDialog({
  draft,
  doctors,
  services,
  onClose,
  onBooked,
}: {
  draft: BookingDraft | null;
  doctors: readonly PublicDoctor[];
  services: readonly PublicService[];
  onClose: () => void;
  onBooked: () => void;
}) {
  const { timeZone } = useStaff();
  const [doctorId, setDoctorId] = useState("");
  const [serviceId, setServiceId] = useState("");
  const [date, setDate] = useState("");
  const [minute, setMinute] = useState(9 * 60);
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [notes, setNotes] = useState("");
  const [errors, setErrors] = useState<Record<string, string | undefined>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<BookingResult | null>(null);
  const [key, setKey] = useState("");

  useEffect(() => {
    if (!draft) return;
    setDoctorId(draft.doctorId);
    setDate(draft.date);
    setMinute(draft.minute);
    const offered = servicesForDoctor(draft.doctorId, doctors, services);
    setServiceId(offered[0]?.id ?? "");
    setName("");
    setPhone("");
    setNotes("");
    setErrors({});
    setError(null);
    setResult(null);
    setKey(newIdempotencyKey("staff"));
  }, [draft, doctors, services]);

  const offered = servicesForDoctor(doctorId, doctors, services);
  const service = offered.find((s) => s.id === serviceId);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    const found: Record<string, string | undefined> = { ...validateDetails({ name, phone, notes }) };
    if (!serviceId) found.service = "Choose a service this dentist offers.";
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) found.date = "Choose a date.";
    setErrors(found);
    if (Object.values(found).some(Boolean)) return;
    const e164 = toIndianE164(phone);
    if (!e164.ok) return;
    let startsAt: string;
    try {
      startsAt = zonedToIso(`${date}T${hhmm(minute)}`, timeZone);
    } catch (err) {
      setError(errorMessage(err));
      return;
    }
    setBusy(true);
    try {
      const booked = await api<BookingResult>("/api/staff/bookings", {
        method: "POST",
        body: { service_id: serviceId, doctor_id: doctorId, starts_at: startsAt, name: name.trim(), phone: e164.e164, ...(notes.trim() ? { notes: notes.trim() } : {}) },
        idempotencyKey: key,
      });
      setResult(booked);
      onBooked();
    } catch (err) {
      setError(errorMessage(err));
      // A refused booking can be retried as a new action after edits.
      setKey(newIdempotencyKey("staff"));
    } finally {
      setBusy(false);
    }
  }

  const doctorName = doctors.find((d) => d.id === doctorId)?.name ?? "";

  return (
    <Dialog
      open={draft !== null}
      onClose={onClose}
      title={result ? "Booked" : "New booking"}
      description={result ? undefined : "Booked by the front desk. Times are clinic time."}
      footer={
        result ? (
          <Button onClick={onClose}>Done</Button>
        ) : (
          <>
            <Button variant="ghost" onClick={onClose} disabled={busy}>
              Cancel
            </Button>
            <Button type="submit" form="staff-booking" loading={busy}>
              Book appointment
            </Button>
          </>
        )
      }
    >
      {result ? (
        <div className="flex flex-col gap-3">
          <p className="font-display text-xl">{formatDateTime(result.starts_at, timeZone)}</p>
          <p className="text-ink-2">
            {result.service_name} with {result.doctor_name}
          </p>
          <p>
            Reference <span className="font-mono font-semibold tracking-wider">{result.reference}</span>
          </p>
          <LabelBadge label={appointmentStatus(result.status)} />
        </div>
      ) : (
        <form id="staff-booking" onSubmit={submit} noValidate className="flex flex-col gap-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Dentist">
              {(props) => (
                <Select
                  {...props}
                  value={doctorId}
                  onChange={(e) => {
                    setDoctorId(e.target.value);
                    setServiceId(servicesForDoctor(e.target.value, doctors, services)[0]?.id ?? "");
                  }}
                >
                  {doctors.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.name}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <Field label="Service" error={errors.service}>
              {(props) => (
                <Select {...props} value={serviceId} onChange={(e) => setServiceId(e.target.value)}>
                  {offered.length === 0 ? <option value="">No services for this dentist</option> : null}
                  {offered.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name} ({s.duration_minutes} min)
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <Field label="Date" error={errors.date}>
              {(props) => <Input {...props} type="date" value={date} onChange={(e) => setDate(e.target.value)} />}
            </Field>
            <Field label="Start time" hint={service ? `Ends ${minutesLabel(minute + service.duration_minutes)}` : undefined}>
              {(props) => (
                <Select {...props} value={minute} onChange={(e) => setMinute(Number(e.target.value))}>
                  {(TIMES.includes(minute) ? TIMES : [...TIMES, minute].sort((x, y) => x - y)).map((m) => (
                    <option key={m} value={m}>
                      {minutesLabel(m)}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
          </div>
          <Field label="Patient name" error={errors.name}>
            {(props) => <Input {...props} value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" />}
          </Field>
          <Field label="Patient mobile" error={errors.phone}>
            {(props) => <PhoneInput {...props} value={phone} onValueChange={setPhone} autoComplete="off" />}
          </Field>
          <Field label="Notes" optional error={errors.notes}>
            {(props) => <Textarea {...props} maxLength={500} value={notes} onChange={(e) => setNotes(e.target.value)} className="min-h-16" />}
          </Field>
          {error ? (
            <p role="alert" className="rounded-md border border-bad/25 bg-bad-tint/60 px-3 py-2 text-sm font-medium">
              {error}
            </p>
          ) : null}
          <p className="text-xs text-ink-3">
            {doctorName ? `${doctorName}, ` : ""}
            {date ? `${date} at ${minutesLabel(minute)}` : ""}. The booking stays pending until the calendar confirms it.
          </p>
        </form>
      )}
    </Dialog>
  );
}
