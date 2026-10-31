"use client";

import { ArrowLeft, Check, ChevronLeft, ChevronRight, Phone, Users } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useMemo, useReducer, useRef, useState, type FormEvent, type ReactNode } from "react";
import useSWR from "swr";

import { api, ApiError, errorMessage, fetcher, newIdempotencyKey, withQuery } from "@/lib/api/client";
import type { BookingResult, PublicDoctor, PublicService, Slot, SlotsResponse } from "@/lib/api/types";
import {
  ANY_DOCTOR,
  bookingReducer,
  distinctTimes,
  doctorsFor,
  initialBooking,
  partOfDay,
  slotsByDay,
  toRequest,
  validateDetails,
  type FieldErrors,
  type Step,
} from "@/lib/booking";
import { cx } from "@/lib/cx";
import { patientBookingStatus } from "@/lib/labels";
import { formatIndian, toIndianE164 } from "@/lib/phone";
import { addDays, formatDayKey, formatLongDate, formatTime, minutesOfDay, todayKey, weekdayOf, zoneAbbreviation } from "@/lib/time";
import { Monogram } from "@/components/public/Monogram";
import { LabelBadge } from "@/components/ui/Badge";
import { Button, ButtonLink } from "@/components/ui/Button";
import { Checkbox, Field, Input, PhoneInput, Textarea } from "@/components/ui/Field";
import { EmptyState, ErrorState, Loading, Skeleton } from "@/components/ui/States";
import { safeColour } from "@/lib/calendar";

const VISIBLE_STEPS: { key: Step; label: string }[] = [
  { key: "service", label: "Treatment" },
  { key: "doctor", label: "Dentist" },
  { key: "time", label: "Time" },
  { key: "details", label: "Your details" },
];

const WEEK = 7;

interface StepperProps {
  services: PublicService[];
  doctors: PublicDoctor[];
  timeZone: string;
  callHref: string | null;
  callDisplay: string | null;
}

function StepHeading({ children, id }: { children: ReactNode; id: string }) {
  return (
    <h2 id={id} tabIndex={-1} className="font-display text-2xl text-ink focus:outline-none sm:text-3xl">
      {children}
    </h2>
  );
}

/**
 * One option on the treatment or dentist step. Choosing moves to the next step, so it is a
 * button rather than a radio: arrow keys never choose (and never skip a step); a click,
 * Enter or Space does. Tab moves between options.
 */
function Choice({
  checked,
  onSelect,
  children,
}: {
  checked: boolean;
  onSelect: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={checked}
      onClick={onSelect}
      className={cx(
        "group relative flex w-full cursor-pointer gap-4 rounded-lg border bg-raised p-4 text-left transition-colors",
        checked ? "border-accent ring-1 ring-accent" : "border-line hover:border-line-strong",
      )}
    >
      {children}
      {checked ? (
        <span className="absolute top-3 right-3 inline-flex size-5 items-center justify-center rounded-full bg-accent text-white">
          <Check className="size-3.5" aria-hidden="true" />
        </span>
      ) : null}
    </button>
  );
}

const CONFLICT_MESSAGE = "We may already have your booking — check it with your reference, or try again.";

export function BookingStepper({ services, doctors, timeZone, callHref, callDisplay }: StepperProps) {
  const params = useSearchParams();
  const [state, dispatch] = useReducer(bookingReducer, initialBooking);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [submitError, setSubmitError] = useState<string | null>(null);
  /** The API saw this idempotency key with a different request: the booking may exist. */
  const [maybeBooked, setMaybeBooked] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const idempotency = useRef<{ request: string; key: string } | null>(null);

  function setField(field: "name" | "phone" | "notes", value: string) {
    dispatch({ type: "field", field, value });
    if (errors[field]) setErrors(({ [field]: _cleared, ...rest }) => rest);
  }
  const headingRef = useRef<string | null>(null);
  const [weekStart, setWeekStart] = useState<string | null>(null);

  // A treatment chosen on the landing page (?service=...) skips the first step.
  useEffect(() => {
    const id = params.get("service");
    if (id && services.some((s) => s.id === id) && state.serviceId === null) dispatch({ type: "service", id });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Move focus to the new step's heading so keyboard and screen-reader users follow along.
  useEffect(() => {
    if (headingRef.current === null) {
      headingRef.current = state.step;
      return;
    }
    if (headingRef.current === state.step) return;
    headingRef.current = state.step;
    document.getElementById(`step-${state.step}`)?.focus();
  }, [state.step]);

  const service = services.find((s) => s.id === state.serviceId) ?? null;
  const eligible = useMemo(() => doctorsFor(doctors, state.serviceId), [doctors, state.serviceId]);
  const doctorName = useMemo(() => new Map(doctors.map((d) => [d.id, d.name])), [doctors]);

  const today = todayKey(timeZone);
  const start = weekStart ?? today;
  const slotsKey =
    state.step === "time" && state.serviceId && state.doctorId
      ? withQuery("/api/public/slots", {
          service_id: state.serviceId,
          doctor_id: state.doctorId === ANY_DOCTOR ? undefined : state.doctorId,
          date: start,
          days: WEEK,
        })
      : null;
  const slots = useSWR<SlotsResponse>(slotsKey, fetcher, { revalidateOnFocus: true, keepPreviousData: false });
  const zone = slots.data?.timezone ?? timeZone;
  const byDay = useMemo(() => slotsByDay(slots.data?.slots ?? [], zone), [slots.data, zone]);
  const days = Array.from({ length: WEEK }, (_, i) => addDays(start, i));
  const firstOpenDay = days.find((d) => (byDay.get(d)?.length ?? 0) > 0) ?? null;
  const selectedDay = state.date && days.includes(state.date) ? state.date : firstOpenDay;

  async function submit(event: FormEvent) {
    event.preventDefault();
    setSubmitError(null);
    setMaybeBooked(false);
    const found = validateDetails(state);
    setErrors(found);
    if (Object.keys(found).length) {
      const first = Object.keys(found)[0];
      document.querySelector<HTMLElement>(`[data-field="${first}"] input, [data-field="${first}"] textarea`)?.focus();
      return;
    }
    const request = toRequest(state);
    if (!request) return;
    // A retry of the same request reuses its key (so a lost response can't double-book); any
    // change to the slot or the details is a new request with a fresh key.
    const fingerprint = JSON.stringify(request);
    if (!idempotency.current || idempotency.current.request !== fingerprint) {
      idempotency.current = { request: fingerprint, key: newIdempotencyKey("web") };
    }
    setSubmitting(true);
    try {
      const result = await api<BookingResult>("/api/public/bookings", {
        method: "POST",
        body: request,
        idempotencyKey: idempotency.current.key,
      });
      dispatch({ type: "booked", result });
    } catch (error) {
      if (error instanceof ApiError && error.status === 409 && error.code === "idempotency_conflict") {
        // Not a taken slot: an earlier attempt with this key may have gone through. Stay on
        // the details step; the next submission gets a new key once anything changes.
        idempotency.current = null;
        setMaybeBooked(true);
        setSubmitError(CONFLICT_MESSAGE);
      } else if (error instanceof ApiError && error.status === 409) {
        dispatch({ type: "slotLost" });
        setSubmitError(error.message || "That time has just been taken. Please choose another.");
        void slots.mutate();
      } else {
        setSubmitError(errorMessage(error));
      }
    } finally {
      setSubmitting(false);
    }
  }

  if (state.step === "done" && state.result) {
    return <Confirmation result={state.result} timeZone={timeZone} phone={toIndianE164(state.phone)} callHref={callHref} />;
  }

  const stepIndex = VISIBLE_STEPS.findIndex((s) => s.key === state.step);

  return (
    <div className="grid gap-10 lg:grid-cols-[1fr_20rem]">
      <div className="min-w-0">
        <nav aria-label="Booking steps" className="mb-8">
          <ol className="flex flex-wrap items-center gap-x-2 gap-y-2 text-sm">
            {VISIBLE_STEPS.map((step, index) => {
              const current = index === stepIndex;
              const done = index < stepIndex;
              return (
                <li key={step.key} className="flex items-center gap-2">
                  {done ? (
                    <button
                      type="button"
                      onClick={() => dispatch({ type: "goto", step: step.key })}
                      className="inline-flex items-center gap-1.5 rounded-sm font-semibold text-ink underline-offset-4 hover:underline"
                    >
                      <span className="inline-flex size-5 items-center justify-center rounded-full bg-ink text-paper">
                        <Check className="size-3" aria-hidden="true" />
                      </span>
                      {step.label}
                      <span className="sr-only"> (done, change)</span>
                    </button>
                  ) : (
                    <span aria-current={current ? "step" : undefined} className={cx("inline-flex items-center gap-1.5", current ? "font-semibold text-ink" : "text-ink-3")}>
                      <span
                        className={cx(
                          "inline-flex size-5 items-center justify-center rounded-full text-xs tnum",
                          current ? "bg-accent text-white" : "border border-line-strong",
                        )}
                      >
                        {index + 1}
                      </span>
                      {step.label}
                    </span>
                  )}
                  {index < VISIBLE_STEPS.length - 1 ? <span aria-hidden="true" className="h-px w-5 bg-line-strong" /> : null}
                </li>
              );
            })}
          </ol>
        </nav>

        {state.step === "service" ? (
          <section aria-labelledby="step-service">
            <StepHeading id="step-service">Which treatment?</StepHeading>
            <p className="mt-2 text-ink-2">Not sure? Choose a check-up and cleaning; the dentist will advise from there.</p>
            {services.length ? (
              <fieldset className="mt-6">
                <legend className="sr-only">Treatment</legend>
                <div className="grid gap-3 sm:grid-cols-2">
                  {services.map((s) => (
                    <Choice key={s.id} checked={state.serviceId === s.id} onSelect={() => dispatch({ type: "service", id: s.id })}>
                      <span aria-hidden="true" className="w-1 shrink-0 rounded-full" style={{ background: safeColour(s.colour) }} />
                      <span className="flex flex-col gap-1 pr-6">
                        <span className="font-semibold text-ink">{s.name}</span>
                        <span className="text-sm text-ink-3 tnum">{s.duration_minutes} minutes</span>
                        {s.description ? <span className="line-clamp-2 text-sm text-ink-2">{s.description}</span> : null}
                      </span>
                    </Choice>
                  ))}
                </div>
              </fieldset>
            ) : (
              <ErrorState className="mt-6" message="We couldn't load the list of treatments. Refresh the page, or call us to book." />
            )}
          </section>
        ) : null}

        {state.step === "doctor" ? (
          <section aria-labelledby="step-doctor">
            <StepHeading id="step-doctor">Who would you like to see?</StepHeading>
            <p className="mt-2 text-ink-2">Choosing &ldquo;any dentist&rdquo; shows the most openings.</p>
            <fieldset className="mt-6">
              <legend className="sr-only">Dentist</legend>
              <div className="grid gap-3 sm:grid-cols-2">
                <Choice checked={state.doctorId === ANY_DOCTOR} onSelect={() => dispatch({ type: "doctor", id: ANY_DOCTOR })}>
                  <span aria-hidden="true" className="inline-flex size-11 shrink-0 items-center justify-center rounded-full border border-dashed border-line-strong text-ink-2">
                    <Users className="size-5" />
                  </span>
                  <span className="flex flex-col gap-0.5 pr-6">
                    <span className="font-semibold">Any available dentist</span>
                    <span className="text-sm text-ink-2">The first opening for {service?.name.toLowerCase() ?? "this treatment"}.</span>
                  </span>
                </Choice>
                {eligible.map((d) => (
                  <Choice key={d.id} checked={state.doctorId === d.id} onSelect={() => dispatch({ type: "doctor", id: d.id })}>
                    <Monogram name={d.name} colour={d.colour} className="size-11 text-base" />
                    <span className="flex flex-col gap-0.5 pr-6">
                      <span className="font-semibold">{d.name}</span>
                      {d.public_bio ? <span className="line-clamp-2 text-sm text-ink-2">{d.public_bio}</span> : null}
                    </span>
                  </Choice>
                ))}
              </div>
            </fieldset>
          </section>
        ) : null}

        {state.step === "time" ? (
          <section aria-labelledby="step-time">
            <StepHeading id="step-time">Choose a time</StepHeading>
            <p className="mt-2 text-ink-2">Times are in clinic time ({zoneAbbreviation(zone)}).</p>
            {submitError ? (
              <p role="alert" className="mt-4 rounded-md border border-bad/25 bg-bad-tint/60 px-3 py-2 text-sm font-medium text-ink">
                {submitError}
              </p>
            ) : null}

            <div className="mt-6 flex items-center gap-2">
              <button
                type="button"
                onClick={() => setWeekStart(addDays(start, -WEEK) < today ? today : addDays(start, -WEEK))}
                disabled={start <= today}
                className="inline-flex size-10 shrink-0 items-center justify-center rounded-md border border-line-strong bg-raised disabled:opacity-40"
                aria-label="Previous week"
              >
                <ChevronLeft className="size-4" aria-hidden="true" />
              </button>
              <div role="group" aria-label="Days" className="grid flex-1 grid-cols-7 gap-1.5">
                {days.map((day) => {
                  const count = byDay.get(day)?.length ?? 0;
                  const selected = day === selectedDay;
                  const label = formatDayKey(day, "long");
                  return (
                    <button
                      key={day}
                      type="button"
                      aria-pressed={selected}
                      aria-label={`${label}${slots.data ? `, ${count ? `${count} openings` : "no openings"}` : ""}`}
                      disabled={!!slots.data && count === 0}
                      onClick={() => dispatch({ type: "date", date: day })}
                      className={cx(
                        "flex flex-col items-center rounded-md border px-1 py-2 transition-colors",
                        selected ? "border-ink bg-ink text-paper" : "border-line bg-raised hover:border-line-strong",
                        "disabled:cursor-not-allowed disabled:bg-transparent disabled:text-ink-3",
                      )}
                    >
                      <span className="text-xs tracking-wide uppercase">{["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][weekdayOf(day)]}</span>
                      <span className="font-display text-lg leading-tight tnum">{Number(day.slice(8))}</span>
                      <span aria-hidden="true" className={cx("mt-0.5 size-1 rounded-full", count ? (selected ? "bg-paper" : "bg-accent") : "bg-transparent")} />
                    </button>
                  );
                })}
              </div>
              <button
                type="button"
                onClick={() => setWeekStart(addDays(start, WEEK))}
                className="inline-flex size-10 shrink-0 items-center justify-center rounded-md border border-line-strong bg-raised"
                aria-label="Next week"
              >
                <ChevronRight className="size-4" aria-hidden="true" />
              </button>
            </div>

            <div className="mt-6" aria-live="polite">
              {slots.error ? (
                <ErrorState
                  message={errorMessage(slots.error)}
                  action={
                    <Button size="sm" variant="secondary" onClick={() => void slots.mutate()}>
                      Try again
                    </Button>
                  }
                />
              ) : !slots.data ? (
                <Loading label="Loading available times" className="grid grid-cols-3 gap-2 sm:grid-cols-5">
                  {Array.from({ length: 10 }, (_, i) => (
                    <Skeleton key={i} className="h-11" />
                  ))}
                </Loading>
              ) : !selectedDay || !(byDay.get(selectedDay)?.length) ? (
                <EmptyState
                  title="No openings this week"
                  action={
                    <div className="flex flex-wrap justify-center gap-2">
                      <Button size="sm" variant="secondary" onClick={() => setWeekStart(addDays(start, WEEK))}>
                        Look at the next week
                      </Button>
                      {state.doctorId !== ANY_DOCTOR ? (
                        <Button size="sm" variant="ghost" onClick={() => dispatch({ type: "doctor", id: ANY_DOCTOR })}>
                          Try any dentist
                        </Button>
                      ) : null}
                    </div>
                  }
                >
                  {callHref ? (
                    <>
                      Or call us on{" "}
                      <a className="font-semibold text-accent" href={callHref}>
                        {callDisplay}
                      </a>{" "}
                      and we&apos;ll find something.
                    </>
                  ) : null}
                </EmptyState>
              ) : (
                <SlotGrid
                  day={selectedDay}
                  slots={state.doctorId === ANY_DOCTOR ? distinctTimes(byDay.get(selectedDay) ?? []) : (byDay.get(selectedDay) ?? [])}
                  selected={state.slot}
                  timeZone={zone}
                  doctorName={state.doctorId === ANY_DOCTOR ? doctorName : null}
                  onSelect={(slot) => {
                    setSubmitError(null);
                    dispatch({ type: "date", date: selectedDay });
                    dispatch({ type: "slot", slot });
                  }}
                />
              )}
            </div>
          </section>
        ) : null}

        {state.step === "details" && state.slot ? (
          <section aria-labelledby="step-details">
            <StepHeading id="step-details">Your details</StepHeading>
            <p className="mt-2 text-ink-2">We use these to confirm the booking and, if you agree, to remind you.</p>
            <form onSubmit={submit} noValidate className="mt-6 flex max-w-lg flex-col gap-5">
              <div data-field="name">
                <Field label="Patient's full name" error={errors.name}>
                  {(props) => (
                    <Input {...props} autoComplete="name" value={state.name} onChange={(e) => setField("name", e.target.value)} />
                  )}
                </Field>
              </div>
              <div data-field="phone">
                <Field label="Mobile number" hint="Ten digits. We'll only use it about this appointment." error={errors.phone}>
                  {(props) => <PhoneInput {...props} value={state.phone} onValueChange={(value) => setField("phone", value)} />}
                </Field>
              </div>
              <div data-field="notes">
                <Field label="Anything we should know?" optional hint="For example, if you're nervous or need step-free access. Please don't include medical history." error={errors.notes}>
                  {(props) => (
                    <Textarea {...props} maxLength={500} value={state.notes} onChange={(e) => setField("notes", e.target.value)} />
                  )}
                </Field>
              </div>
              <Checkbox
                label="Send me a confirmation and a reminder about this appointment."
                description="By call or message from the clinic. Saying no doesn't affect your booking."
                checked={state.consent}
                onChange={(e) => dispatch({ type: "consent", value: e.target.checked })}
              />
              {submitError ? (
                <div role="alert" className="rounded-md border border-bad/25 bg-bad-tint/60 px-3 py-2 text-sm font-medium text-ink">
                  <p>{submitError}</p>
                  {maybeBooked ? (
                    <Link href="/booking" className="mt-1 inline-block font-semibold text-accent underline-offset-4 hover:underline">
                      Check a booking with your reference
                    </Link>
                  ) : null}
                </div>
              ) : null}
              <div className="flex flex-wrap items-center gap-3">
                <Button type="submit" size="lg" loading={submitting}>
                  Book this appointment
                </Button>
                <Button type="button" variant="ghost" onClick={() => dispatch({ type: "goto", step: "time" })} icon={<ArrowLeft className="size-4" aria-hidden="true" />}>
                  Change time
                </Button>
              </div>
            </form>
          </section>
        ) : null}
      </div>

      <aside aria-label="Your booking so far" className="h-fit rounded-lg border border-line bg-raised p-5 lg:sticky lg:top-24">
        <h2 className="text-xs font-semibold tracking-[0.14em] text-ink-3 uppercase">Your booking</h2>
        <dl className="mt-4 flex flex-col gap-3 text-[0.95rem]">
          <div>
            <dt className="text-sm text-ink-3">Treatment</dt>
            <dd className="font-semibold">{service ? `${service.name} · ${service.duration_minutes} min` : "Not chosen yet"}</dd>
          </div>
          <div>
            <dt className="text-sm text-ink-3">Dentist</dt>
            <dd className="font-semibold">
              {state.slot
                ? (doctorName.get(state.slot.doctor_id) ?? "Any available dentist")
                : state.doctorId === ANY_DOCTOR
                  ? "Any available dentist"
                  : state.doctorId
                    ? doctorName.get(state.doctorId)
                    : "Not chosen yet"}
            </dd>
          </div>
          <div>
            <dt className="text-sm text-ink-3">Time</dt>
            <dd className="font-semibold tnum">
              {state.slot ? `${formatLongDate(state.slot.starts_at, zone)}, ${formatTime(state.slot.starts_at, zone)}` : "Not chosen yet"}
            </dd>
          </div>
        </dl>
        {callHref ? (
          <p className="mt-5 border-t border-line pt-4 text-sm text-ink-2">
            Prefer to talk?{" "}
            <a href={callHref} className="inline-flex items-center gap-1 font-semibold text-accent">
              <Phone className="size-3.5" aria-hidden="true" />
              {callDisplay}
            </a>
          </p>
        ) : null}
      </aside>
    </div>
  );
}

function SlotGrid({
  day,
  slots,
  selected,
  timeZone,
  doctorName,
  onSelect,
}: {
  day: string;
  slots: Slot[];
  selected: Slot | null;
  timeZone: string;
  doctorName: Map<string, string> | null;
  onSelect: (slot: Slot) => void;
}) {
  const groups = new Map<string, Slot[]>();
  for (const slot of slots) {
    const part = partOfDay(minutesOfDay(slot.starts_at, timeZone));
    groups.set(part, [...(groups.get(part) ?? []), slot]);
  }
  return (
    <div className="flex flex-col gap-6">
      <p className="font-semibold text-ink">{formatDayKey(day, "long")}</p>
      {[...groups.entries()].map(([part, list]) => (
        <fieldset key={part}>
          <legend className="mb-2 text-sm text-ink-3">{part}</legend>
          <div className="grid grid-cols-3 gap-2 sm:grid-cols-5">
            {list.map((slot) => {
              const isSelected = selected?.starts_at === slot.starts_at && selected.doctor_id === slot.doctor_id;
              const time = formatTime(slot.starts_at, timeZone);
              const who = doctorName?.get(slot.doctor_id);
              return (
                <button
                  key={`${slot.doctor_id}-${slot.starts_at}`}
                  type="button"
                  aria-pressed={isSelected}
                  aria-label={who ? `${time} with ${who}` : time}
                  onClick={() => onSelect(slot)}
                  className={cx(
                    "h-11 rounded-md border text-[0.95rem] font-semibold tnum transition-colors",
                    isSelected ? "border-accent bg-accent text-white" : "border-line-strong bg-raised text-ink hover:border-accent hover:text-accent",
                  )}
                >
                  {time}
                </button>
              );
            })}
          </div>
        </fieldset>
      ))}
    </div>
  );
}

function Confirmation({
  result,
  timeZone,
  phone,
  callHref,
}: {
  result: BookingResult;
  timeZone: string;
  phone: ReturnType<typeof toIndianE164>;
  callHref: string | null;
}) {
  const label = patientBookingStatus(result.status);
  const confirmed = result.status === "confirmed";
  const ref = useRef<HTMLHeadingElement>(null);
  useEffect(() => ref.current?.focus(), []);
  return (
    <section aria-labelledby="done-title" className="mx-auto max-w-2xl">
      <div className="rounded-lg border border-line bg-raised p-6 sm:p-10">
        <p className="text-xs font-semibold tracking-[0.16em] text-accent uppercase">{confirmed ? "Booked" : "Booking received"}</p>
        <h2 id="done-title" ref={ref} tabIndex={-1} className="mt-3 font-display text-3xl leading-tight focus:outline-none sm:text-4xl">
          {formatLongDate(result.starts_at, timeZone)}, {formatTime(result.starts_at, timeZone)}
        </h2>
        <p className="mt-2 text-lg text-ink-2">
          {result.service_name} with {result.doctor_name}
        </p>
        <dl className="mt-8 grid gap-6 border-t border-line pt-6 sm:grid-cols-2">
          <div>
            <dt className="text-sm text-ink-3">Booking reference</dt>
            <dd className="mt-1 font-mono text-2xl font-semibold tracking-[0.18em] text-ink">{result.reference}</dd>
          </div>
          <div>
            <dt className="text-sm text-ink-3">Status</dt>
            <dd className="mt-2">
              <LabelBadge label={label} />
            </dd>
          </div>
        </dl>
        <p className="mt-6 leading-relaxed text-ink-2">
          {confirmed
            ? "This time is held for you in the dentist's calendar. Please arrive five minutes early."
            : "We have your booking and are confirming it against the dentist's calendar. It is not final until it says Confirmed; check it below, and we'll contact you if the time can't be kept."}
          {phone.ok ? ` We'll use ${formatIndian(phone.e164)} if we need to reach you.` : ""}
        </p>
        <div className="mt-8 flex flex-wrap gap-3">
          <ButtonLink href="/booking" variant="secondary">
            Check this booking
          </ButtonLink>
          <ButtonLink href="/" variant="ghost">
            Back to the clinic
          </ButtonLink>
        </div>
      </div>
      <p className="mt-6 text-center text-sm text-ink-3">
        Need to change it?{" "}
        {callHref ? (
          <a href={callHref} className="font-semibold text-accent">
            Call us
          </a>
        ) : (
          <Link href="/callback" className="font-semibold text-accent">
            Ask us to call you
          </Link>
        )}{" "}
        with your reference.
      </p>
    </section>
  );
}
