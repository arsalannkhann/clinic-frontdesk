"use client";

import { CalendarCheck2, Clock } from "lucide-react";
import { useId, useState, type FormEvent } from "react";

import type { BookingResult, ChatProposal } from "@/lib/api/types";
import type { ProposalState } from "@/lib/chat";
import { validateDetails } from "@/lib/booking";
import { patientBookingStatus } from "@/lib/labels";
import { toIndianE164 } from "@/lib/phone";
import { formatLongDate, formatTime } from "@/lib/time";
import { Button } from "@/components/ui/Button";
import { Checkbox, Field, Input, PhoneInput } from "@/components/ui/Field";
import { LabelBadge } from "@/components/ui/Badge";

export const CONSENT_REQUIRED = "Tick this to book. The clinic needs to be able to reach you about the appointment.";

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
  const consentId = useId();
  const [errors, setErrors] = useState<{ name?: string; phone?: string; consent?: string }>({});

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
    // The clinic has to be able to reach the patient about a booking, so consent is required.
    const consentError = consent ? undefined : CONSENT_REQUIRED;
    setErrors({ name: found.name, phone: found.phone, consent: consentError });
    if (found.name || found.phone || consentError) return;
    const e164 = toIndianE164(phone);
    if (!e164.ok) return;
    onConfirm({ name: name.trim(), phone: e164.e164, consent_contact: true });
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
          label="The clinic may contact me about this appointment."
          description="A confirmation, a reminder, or a call if the time has to change. Needed to book from the chat."
          checked={consent}
          onChange={(e) => {
            setConsent(e.target.checked);
            if (e.target.checked) setErrors((current) => ({ ...current, consent: undefined }));
          }}
          disabled={busy}
          id={consentId}
          aria-invalid={errors.consent ? true : undefined}
          aria-describedby={errors.consent ? `${consentId}-desc ${consentId}-error` : `${consentId}-desc`}
        />
        {errors.consent ? (
          <p id={`${consentId}-error`} className="-mt-1 text-sm font-medium text-bad">
            {errors.consent}
          </p>
        ) : null}
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
