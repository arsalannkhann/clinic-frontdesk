"use client";

import { PhoneCall } from "lucide-react";
import { useState, type FormEvent } from "react";

import { api, errorMessage } from "@/lib/api/client";
import type { CallbackCreated } from "@/lib/api/types";
import { validateDetails } from "@/lib/booking";
import { toIndianE164 } from "@/lib/phone";
import { Button } from "@/components/ui/Button";
import { Field, Input, PhoneInput, Textarea } from "@/components/ui/Field";

export function CallbackForm({ hoursNote }: { hoursNote: string | null }) {
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [reason, setReason] = useState("");
  const [errors, setErrors] = useState<{ name?: string; phone?: string; notes?: string }>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    const found = validateDetails({ name, phone, notes: reason });
    setErrors({ name: found.name, phone: found.phone, notes: found.notes });
    const e164 = toIndianE164(phone);
    if (found.name || found.phone || found.notes || !e164.ok) return;
    setBusy(true);
    try {
      await api<CallbackCreated>("/api/public/callbacks", {
        method: "POST",
        body: { name: name.trim(), phone: e164.e164, ...(reason.trim() ? { reason: reason.trim() } : {}) },
      });
      setDone(true);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  if (done) {
    return (
      <section role="status" className="rounded-lg border border-ok/30 bg-raised p-6">
        <p className="flex items-center gap-2 font-semibold text-ok">
          <PhoneCall className="size-5" aria-hidden="true" /> Request received
        </p>
        <p className="mt-2 text-ink-2">
          The front desk will call you back{hoursNote ? ` during clinic hours (${hoursNote.replace(/\.$/, "")})` : " during clinic hours"}. If it&apos;s urgent,
          please call us directly.
        </p>
      </section>
    );
  }

  return (
    <form onSubmit={submit} noValidate className="flex max-w-lg flex-col gap-5">
      <Field label="Your name" error={errors.name}>
        {(props) => <Input {...props} autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} />}
      </Field>
      <Field label="Mobile number" error={errors.phone}>
        {(props) => <PhoneInput {...props} value={phone} onValueChange={setPhone} />}
      </Field>
      <Field label="What is it about?" optional hint="A line is enough, for example 'insurance question' or 'reschedule Tuesday'." error={errors.notes}>
        {(props) => <Textarea {...props} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />}
      </Field>
      {error ? (
        <p role="alert" className="rounded-md border border-bad/25 bg-bad-tint/60 px-3 py-2 text-sm font-medium">
          {error}
        </p>
      ) : null}
      <div>
        <Button type="submit" size="lg" loading={busy}>
          Ask for a call back
        </Button>
      </div>
      <p className="text-sm text-ink-3">We use your number only to return this call.</p>
    </form>
  );
}
