"use client";

import { Search } from "lucide-react";
import { useState, type FormEvent } from "react";

import { api, ApiError, errorMessage } from "@/lib/api/client";
import type { BookingResult } from "@/lib/api/types";
import { patientBookingStatus } from "@/lib/labels";
import { toIndianE164 } from "@/lib/phone";
import { formatLongDate, formatTime } from "@/lib/time";
import { LabelBadge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Field, Input, PhoneInput } from "@/components/ui/Field";

/** Booking references are 8 Crockford base32 characters; O reads as 0 and I/L as 1. */
export function normaliseReference(input: string): string {
  return input.toUpperCase().replace(/[\s-]/g, "").replace(/O/g, "0").replace(/[IL]/g, "1");
}

export function LookupForm({ timeZone }: { timeZone: string }) {
  const [reference, setReference] = useState("");
  const [phone, setPhone] = useState("");
  const [errors, setErrors] = useState<{ reference?: string; phone?: string }>({});
  const [result, setResult] = useState<BookingResult | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setMessage(null);
    setResult(null);
    const ref = normaliseReference(reference);
    const e164 = toIndianE164(phone);
    const found = {
      reference: /^[0-9A-HJKMNP-TV-Z]{8}$/.test(ref) ? undefined : "Enter the 8-character reference from your confirmation.",
      phone: e164.ok ? undefined : e164.message,
    };
    setErrors(found);
    if (found.reference || !e164.ok) return;
    setBusy(true);
    try {
      setResult(await api<BookingResult>("/api/public/bookings/lookup", { method: "POST", body: { reference: ref, phone: e164.e164 } }));
    } catch (error) {
      setMessage(
        error instanceof ApiError && error.status === 404
          ? "We couldn't find a booking with that reference and phone number. Check both, or call the clinic."
          : errorMessage(error),
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid gap-10 md:grid-cols-[1fr_1fr]">
      <form onSubmit={submit} noValidate className="flex flex-col gap-5">
        <Field label="Booking reference" hint="8 letters and numbers, for example 7K2M9QX4." error={errors.reference}>
          {(props) => (
            <Input
              {...props}
              value={reference}
              onChange={(e) => setReference(e.target.value.slice(0, 12))}
              autoCapitalize="characters"
              autoComplete="off"
              spellCheck={false}
              className="font-mono tracking-[0.12em] uppercase"
            />
          )}
        </Field>
        <Field label="Mobile number you booked with" error={errors.phone}>
          {(props) => <PhoneInput {...props} value={phone} onValueChange={setPhone} />}
        </Field>
        <div>
          <Button type="submit" size="lg" loading={busy} icon={<Search className="size-4" aria-hidden="true" />}>
            Find my booking
          </Button>
        </div>
      </form>

      <div aria-live="polite">
        {message ? (
          <p role="alert" className="rounded-lg border border-line bg-raised p-5 text-ink-2">
            {message}
          </p>
        ) : null}
        {result ? (
          <section aria-label="Booking found" className="rounded-lg border border-line bg-raised p-6">
            <p className="font-mono text-sm tracking-[0.16em] text-ink-3">{result.reference}</p>
            <p className="mt-2 font-display text-2xl leading-tight">
              {formatLongDate(result.starts_at, timeZone)}, {formatTime(result.starts_at, timeZone)}
            </p>
            <p className="mt-1 text-ink-2">
              {result.service_name} with {result.doctor_name}
            </p>
            <div className="mt-4">
              <LabelBadge label={patientBookingStatus(result.status)} />
            </div>
            {result.status !== "confirmed" && result.status !== "cancelled" ? (
              <p className="mt-3 text-sm text-ink-2">This booking isn&apos;t final yet. We&apos;ll contact you if the time can&apos;t be kept.</p>
            ) : null}
          </section>
        ) : null}
      </div>
    </div>
  );
}
