"use client";

import { CalendarCheck2, Clock } from "lucide-react";
import { useState, type FormEvent } from "react";

import type { BookingResult, ChatProposal } from "@/lib/api/types";
import type { ProposalState } from "@/lib/chat";
import { validateDetails } from "@/lib/booking";
import { patientBookingStatus } from "@/lib/labels";
import { toIndianE164 } from "@/lib/phone";
import { formatLongDate, formatTime } from "@/lib/time";
import { Button } from "@/components/ui/Button";
import { Checkbox, Field, Input, PhoneInput } from "@/components/ui/Field";
import { LabelBadge } from "@/components/ui/Badge";

export interface ProposalCardProps {
  proposal: ChatProposal;
  state: ProposalState;
  error: string | null;
  booking: BookingResult | null;
  timeZone: string;
  onConfirm: (details: { name: string; phone: string; consent_contact: boolean }) => void;
  onChange: () => void;
}

/** The booking suggestion. Only the Confirm button books, with the details typed here. */
export function ProposalCard({ proposal, state, error, booking, timeZone, onConfirm, onChange }: ProposalCardProps) {
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [consent, setConsent] = useState(false);
  const [errors, setErrors] = useState<{ name?: string; phone?: string }>({});

  const when = (
    <div className="flex flex-col gap-1">
      <p className="font-display text-lg leading-tight text-ink">{proposal.service_name}</p>
      <p className="text-sm text-ink-2">with {proposal.doctor_name}</p>
      <p className="mt-1 flex items-center gap-1.5 text-sm font-semibold text-ink tnum">
        <Clock className="size-4 text-accent" aria-hidden="true" />
        {formatLongDate(proposal.starts_at, timeZone)}, {formatTime(proposal.starts_at, timeZone)}
      </p>
    </div>
  );

  if (state === "booked" && booking) {
    const label = patientBookingStatus(booking.status);
    return (
      <section aria-label="Booking" className="rounded-lg border border-ok/30 bg-raised p-4">
        <div className="mb-3 flex items-center gap-2 text-ok">
          <CalendarCheck2 className="size-5" aria-hidden="true" />
          <p className="font-semibold">{booking.status === "confirmed" ? "You're booked" : "Booking received"}</p>
        </div>
        {when}
        <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
          <dt className="text-ink-3">Reference</dt>
          <dd className="font-mono font-semibold tracking-wider text-ink">{booking.reference}</dd>
          <dt className="text-ink-3">Status</dt>
          <dd>
            <LabelBadge label={label} />
          </dd>
        </dl>
        <p className="mt-3 text-xs text-ink-3">Keep the reference. You can check this booking on the website with it and your phone number.</p>
      </section>
    );
  }

  if (state === "withdrawn" || state === "expired") {
    return (
      <section aria-label="Suggested time" className="rounded-lg border border-line bg-sunk/60 p-4 text-sm text-ink-2">
        <p className="line-through decoration-ink-3/60">
          {proposal.service_name}, {formatLongDate(proposal.starts_at, timeZone)} {formatTime(proposal.starts_at, timeZone)}
        </p>
        <p className="mt-1">{state === "expired" ? (error ?? "This suggestion has expired. Ask for a new time.") : "You asked for a different time."}</p>
      </section>
    );
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    const found = validateDetails({ name, phone, notes: "" });
    setErrors({ name: found.name, phone: found.phone });
    if (found.name || found.phone) return;
    const e164 = toIndianE164(phone);
    if (!e164.ok) return;
    onConfirm({ name: name.trim(), phone: e164.e164, consent_contact: consent });
  }

  const busy = state === "confirming";
  return (
    <section aria-label="Suggested time" className="rounded-lg border border-accent/30 bg-raised p-4 shadow-lift">
      <p className="mb-2 text-xs font-semibold tracking-[0.12em] text-accent uppercase">Suggested time</p>
      {when}
      <form onSubmit={submit} noValidate className="mt-4 flex flex-col gap-3">
        <Field label="Patient name" error={errors.name}>
          {(props) => (
            <Input {...props} autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} disabled={busy} />
          )}
        </Field>
        <Field label="Mobile number" error={errors.phone}>
          {(props) => <PhoneInput {...props} value={phone} onValueChange={setPhone} disabled={busy} />}
        </Field>
        <Checkbox
          label="Send me a confirmation and a reminder about this appointment."
          description="By call or message from the clinic. You can say no; it doesn't affect the booking."
          checked={consent}
          onChange={(e) => setConsent(e.target.checked)}
          disabled={busy}
        />
        {error ? (
          <p role="alert" className="text-sm font-medium text-bad">
            {error}
          </p>
        ) : null}
        <div className="mt-1 flex flex-wrap gap-2">
          <Button type="submit" loading={busy}>
            Confirm booking
          </Button>
          <Button type="button" variant="secondary" onClick={onChange} disabled={busy}>
            Change
          </Button>
        </div>
      </form>
    </section>
  );
}
