"use client";

import { Plus, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import useSWR, { useSWRConfig } from "swr";

import { api, errorMessage, withQuery } from "@/lib/api/client";
import type {
  AdminDoctor,
  AdminService,
  ClinicConfig,
  OutboundStatus,
  ExceptionCreated,
  HoursWindow,
  ScheduleException,
} from "@/lib/api/types";
import { useCatalog } from "@/lib/catalog";
import { exceptionKind } from "@/lib/labels";
import { addDays, formatShortDate, formatTime, todayKey, zonedToIso } from "@/lib/time";
import { Badge, LabelBadge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Checkbox, Field, Input, Select, Textarea } from "@/components/ui/Field";
import { EmptyState, ErrorState, Loading, Skeleton } from "@/components/ui/States";
import { Tabs } from "@/components/ui/Tabs";
import { useToast } from "@/components/ui/Toast";
import { PageHeader, Panel } from "./PageHeader";
import { useStaff } from "./StaffContext";

type Tab = "clinic" | "doctors" | "services" | "hours" | "exceptions";
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

export function SettingsPage() {
  const [tab, setTab] = useState<Tab>("clinic");
  return (
    <div>
      <PageHeader title="Settings" description="Clinic profile, dentists, services, hours and closures." />
      <Tabs
        label="Settings sections"
        value={tab}
        onChange={setTab}
        items={[
          { key: "clinic", label: "Clinic" },
          { key: "doctors", label: "Dentists" },
          { key: "services", label: "Services" },
          { key: "hours", label: "Hours" },
          { key: "exceptions", label: "Closures and leave" },
        ]}
      >
        {tab === "clinic" ? <ClinicSettings /> : null}
        {tab === "doctors" ? <DoctorSettings /> : null}
        {tab === "services" ? <ServiceSettings /> : null}
        {tab === "hours" ? <HoursSettings /> : null}
        {tab === "exceptions" ? <ExceptionSettings /> : null}
      </Tabs>
    </div>
  );
}

/* ---------- clinic ---------- */

type ClinicForm = Omit<ClinicConfig, "id" | "timezone">;
const OWNER_ONLY: (keyof ClinicForm)[] = ["outbound_enabled", "scripts_approved", "test_calls_enabled", "include_reference_in_messages"];

/** The public profile, edited through GET/PATCH admin/clinic and shown on the website. */
const PROFILE_TEXT = ["tagline", "address", "map_url", "phone_display", "email", "hours_note", "about", "voice_number"] as const;
type ProfileText = (typeof PROFILE_TEXT)[number];
const PROFILE_KEYS: (keyof ClinicForm)[] = [...PROFILE_TEXT, "chat_enabled"];

/** Empty text and a missing value are the same thing; a cleared field is sent as null. */
function normalise(key: keyof ClinicForm, value: unknown): unknown {
  if ((PROFILE_TEXT as readonly string[]).includes(key)) {
    const text = typeof value === "string" ? value.trim() : "";
    return text === "" ? null : text;
  }
  return value;
}

/** The PATCH body: changed fields only, leaving out owner-only fields for everyone else. */
export function clinicChanges(form: ClinicForm, original: ClinicConfig, isOwner: boolean): Partial<ClinicForm> {
  const changes: Record<string, unknown> = {};
  for (const key of Object.keys(form) as (keyof ClinicForm)[]) {
    if (!isOwner && OWNER_ONLY.includes(key)) continue;
    const next = normalise(key, form[key]);
    if (next !== normalise(key, original[key])) changes[key] = next;
  }
  return changes as Partial<ClinicForm>;
}

function toForm(clinic: ClinicConfig): ClinicForm {
  const { id: _id, timezone: _tz, ...rest } = clinic;
  const form: ClinicForm = { ...rest };
  for (const key of PROFILE_TEXT) form[key] = rest[key] ?? "";
  if (form.chat_enabled === undefined && "chat_enabled" in clinic) form.chat_enabled = false;
  return form;
}

function ClinicSettings() {
  const { canWrite, isOwner } = useStaff();
  const toast = useToast();
  const { mutate } = useSWRConfig();
  const clinic = useSWR<ClinicConfig>("/api/admin/clinic");
  const [form, setForm] = useState<ClinicForm | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (clinic.data) setForm(toForm(clinic.data));
  }, [clinic.data]);

  if (clinic.error) return <ErrorState message={errorMessage(clinic.error)} />;
  if (!clinic.data || !form) {
    return (
      <Loading label="Loading clinic settings" className="flex flex-col gap-3">
        <Skeleton className="h-10 w-1/2" />
        <Skeleton className="h-32" />
      </Loading>
    );
  }
  const original = clinic.data;
  // Until the API returns the profile on admin/clinic there is nothing to edit.
  const hasProfile = PROFILE_KEYS.some((key) => key in original);
  const set = <K extends keyof ClinicForm>(key: K, value: ClinicForm[K]) => setForm((f) => (f ? { ...f, [key]: value } : f));
  const text = (key: ProfileText) => (form[key] as string | null | undefined) ?? "";

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!form) return;
    setError(null);
    const changes = clinicChanges(form, original, isOwner);
    if (Object.keys(changes).length === 0) {
      toast.show("Nothing has changed.", "info");
      return;
    }
    setSaving(true);
    try {
      const updated = await api<ClinicConfig>("/api/admin/clinic", { method: "PATCH", body: changes });
      await clinic.mutate(updated, { revalidate: false });
      // Outbound readiness depends on outbound_enabled; refresh the panel and Call-backs.
      if ("outbound_enabled" in changes) void mutate("/api/staff/outbound-status");
      toast.show("Clinic settings saved.");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="grid items-start gap-6 xl:grid-cols-[1.4fr_1fr]">
      <form onSubmit={save} noValidate>
        <fieldset disabled={!canWrite || saving} className="flex flex-col gap-6">
          <Panel title="Booking rules">
            <div className="grid gap-4 p-4 sm:grid-cols-2">
              <Field label="Clinic name">{(p) => <Input {...p} value={form.name} maxLength={120} onChange={(e) => set("name", e.target.value)} />}</Field>
              <Field label="Time zone" hint="Set when the clinic was created.">
                {(p) => <Input {...p} value={original.timezone} readOnly disabled />}
              </Field>
              <Field label="Minimum notice (minutes)" hint="0 to 10080">
                {(p) => <Input {...p} type="number" min={0} max={10080} value={form.lead_time_minutes} onChange={(e) => set("lead_time_minutes", Number(e.target.value))} />}
              </Field>
              <Field label="Booking horizon (days)" hint="1 to 365">
                {(p) => <Input {...p} type="number" min={1} max={365} value={form.horizon_days} onChange={(e) => set("horizon_days", Number(e.target.value))} />}
              </Field>
            </div>
          </Panel>

          <Panel title="Clinic profile">
            {hasProfile ? (
              <div className="grid gap-4 p-4 sm:grid-cols-2">
                <p className="text-sm text-ink-2 sm:col-span-2">Shown to patients on the website and used by the assistants. Leave a field empty to hide it.</p>
                <Field label="Tagline" className="sm:col-span-2">
                  {(p) => <Input {...p} value={text("tagline")} maxLength={160} onChange={(e) => set("tagline", e.target.value)} />}
                </Field>
                <Field label="Address" className="sm:col-span-2">
                  {(p) => <Textarea {...p} rows={2} value={text("address")} maxLength={300} onChange={(e) => set("address", e.target.value)} />}
                </Field>
                <Field label="Map link" hint="A Google Maps or similar https link.">
                  {(p) => <Input {...p} type="url" inputMode="url" value={text("map_url")} maxLength={500} onChange={(e) => set("map_url", e.target.value)} />}
                </Field>
                <Field label="Phone shown on the website">
                  {(p) => <Input {...p} type="tel" value={text("phone_display")} maxLength={40} onChange={(e) => set("phone_display", e.target.value)} />}
                </Field>
                <Field label="Email">
                  {(p) => <Input {...p} type="email" autoComplete="off" value={text("email")} maxLength={254} onChange={(e) => set("email", e.target.value)} />}
                </Field>
                <Field label="Receptionist line" hint="The number patients call to reach the voice assistant, in +91 format.">
                  {(p) => <Input {...p} type="tel" value={text("voice_number")} maxLength={20} onChange={(e) => set("voice_number", e.target.value)} />}
                </Field>
                <Field label="Hours note" hint="For example “Sunday by appointment only”." className="sm:col-span-2">
                  {(p) => <Input {...p} value={text("hours_note")} maxLength={200} onChange={(e) => set("hours_note", e.target.value)} />}
                </Field>
                <Field label="About the clinic" className="sm:col-span-2">
                  {(p) => <Textarea {...p} rows={4} value={text("about")} maxLength={2000} onChange={(e) => set("about", e.target.value)} />}
                </Field>
                <Checkbox
                  className="sm:col-span-2"
                  label="Show the chat assistant on the website"
                  checked={!!form.chat_enabled}
                  onChange={(e) => set("chat_enabled", e.target.checked)}
                />
              </div>
            ) : (
              <p className="p-4 text-sm text-ink-2">The clinic&apos;s public profile can&apos;t be edited from here yet.</p>
            )}
          </Panel>

          <Panel title="Patient contact">
            <div className="flex flex-col gap-4 p-4">
              <Checkbox
                label="Allow outbound contact (reminders and staff call-backs)"
                description={
                  isOwner
                    ? "Off by default. Nothing is sent or dialled while this is off. Calls also need a calling number and an outbound agent; see Outbound calling."
                    : "Owner only. Nothing is sent or dialled while this is off."
                }
                checked={form.outbound_enabled}
                disabled={!isOwner}
                onChange={(e) => set("outbound_enabled", e.target.checked)}
              />
              <div className="grid gap-4 sm:grid-cols-2">
                <Field label="Quiet hours start">{(p) => <Input {...p} type="time" value={form.quiet_hours_start} onChange={(e) => set("quiet_hours_start", e.target.value)} />}</Field>
                <Field label="Quiet hours end">{(p) => <Input {...p} type="time" value={form.quiet_hours_end} onChange={(e) => set("quiet_hours_end", e.target.value)} />}</Field>
                <Field label="Reminder lead (minutes)" hint="60 to 10080">
                  {(p) => <Input {...p} type="number" min={60} max={10080} value={form.reminder_lead_minutes} onChange={(e) => set("reminder_lead_minutes", Number(e.target.value))} />}
                </Field>
                <Field label="Daily contact budget" hint="0 turns outbound contact off">
                  {(p) => <Input {...p} type="number" min={0} max={100000} value={form.daily_contact_budget} onChange={(e) => set("daily_contact_budget", Number(e.target.value))} />}
                </Field>
              </div>
              <Checkbox
                label="Include the booking reference in messages"
                description={isOwner ? undefined : "Owner only."}
                checked={form.include_reference_in_messages}
                disabled={!isOwner}
                onChange={(e) => set("include_reference_in_messages", e.target.checked)}
              />
            </div>
          </Panel>

          <Panel title="Scripts">
            <div className="flex flex-col gap-4 p-4">
              <Field label="Emergency script" hint="Read by the assistants when a patient describes an emergency. Up to 1000 characters; no braces or %.">
                {(p) => <Textarea {...p} maxLength={1000} value={form.emergency_text} onChange={(e) => set("emergency_text", e.target.value)} />}
              </Field>
              <Field label="Hand-off script" hint="Used when the assistant passes a patient to the front desk.">
                {(p) => <Textarea {...p} maxLength={1000} value={form.handoff_text} onChange={(e) => set("handoff_text", e.target.value)} />}
              </Field>
              <Checkbox
                label="Scripts approved by the owner"
                description={isOwner ? "Editing either script without re-approving clears the approval." : "Owner only. Editing a script clears the approval."}
                checked={form.scripts_approved}
                disabled={!isOwner}
                onChange={(e) => set("scripts_approved", e.target.checked)}
              />
              <Checkbox
                label="Allow test calls before the line is live"
                description={isOwner ? undefined : "Owner only."}
                checked={form.test_calls_enabled}
                disabled={!isOwner}
                onChange={(e) => set("test_calls_enabled", e.target.checked)}
              />
            </div>
          </Panel>

          {error ? (
            <p role="alert" className="rounded-md border border-bad/25 bg-bad-tint/60 px-3 py-2 text-sm font-medium">
              {error}
            </p>
          ) : null}
          {canWrite ? (
            <div>
              <Button type="submit" loading={saving}>
                Save clinic settings
              </Button>
            </div>
          ) : null}
        </fieldset>
      </form>

      <OutboundPanel saved={original.outbound_enabled} />
    </div>
  );
}

/**
 * What outbound calling needs and what is in place, from GET staff/outbound-status. Call-backs
 * links here ("See what's needed"). The number and agent are set up by the platform; the owner
 * then turns outbound contact on above.
 */
function OutboundPanel({ saved }: { saved: boolean }) {
  const { canWrite, isOwner } = useStaff();
  const status = useSWR<OutboundStatus>(canWrite ? "/api/staff/outbound-status" : null);

  useEffect(() => {
    if (status.data && window.location.hash === "#outbound") {
      document.getElementById("outbound")?.scrollIntoView({ block: "start" });
    }
  }, [status.data]);

  const data = status.data;
  const ready = data ? data.enabled && data.has_from_number && data.has_outbound_agent : false;
  const steps: { done: boolean; label: string; detail: string }[] = data
    ? [
        {
          done: data.has_from_number,
          label: "A calling number",
          detail: data.has_from_number ? "Configured. Calls show this number to the patient." : "Not configured yet. The platform team connects a number for the clinic.",
        },
        {
          done: data.has_outbound_agent,
          label: "An outbound voice agent",
          detail: data.has_outbound_agent ? "Configured." : "Not configured yet. It is set up together with the calling number.",
        },
        {
          done: data.enabled,
          label: "Outbound contact turned on",
          detail: data.enabled
            ? "On. Staff can place call-backs from the Call-backs page."
            : isOwner
              ? "Off. Turn on “Allow outbound contact” under Patient contact and save."
              : "Off. Only the owner can turn it on.",
        },
      ]
    : [];

  return (
    <div id="outbound" className="scroll-mt-20 xl:sticky xl:top-8">
      <Panel title="Outbound calling" action={data ? <Badge tone={ready ? "ok" : "warn"}>{ready ? "Ready" : "Not ready"}</Badge> : null}>
        <div className="p-4 text-sm">
          {!canWrite ? (
            <p className="text-ink-2">Outbound calling is {saved ? "on" : "off"} for this clinic. Only the owner or an admin can place calls.</p>
          ) : status.error ? (
            <p className="text-ink-2">The outbound status couldn&apos;t be loaded. Outbound contact is {saved ? "on" : "off"} in the clinic settings.</p>
          ) : !data ? (
            <Loading label="Loading the outbound status">
              <Skeleton className="h-28" />
            </Loading>
          ) : (
            <>
              <p className="text-ink-2">Staff can call patients back from the clinic&apos;s number once all three are in place.</p>
              <ol className="mt-4 flex flex-col gap-3">
                {steps.map((step) => (
                  <li key={step.label} className="flex gap-3">
                    <span
                      aria-hidden="true"
                      className={`mt-0.5 inline-flex size-5 shrink-0 items-center justify-center rounded-full text-xs font-bold ${step.done ? "bg-ok text-paper" : "border border-line-strong text-ink-3"}`}
                    >
                      {step.done ? "✓" : ""}
                    </span>
                    <span>
                      <span className="block font-semibold text-ink">
                        {step.label}
                        <span className="sr-only">{step.done ? ": done" : ": not yet"}</span>
                      </span>
                      <span className="block text-ink-2">{step.detail}</span>
                    </span>
                  </li>
                ))}
              </ol>
              <p className="mt-4 border-t border-line pt-3 text-xs text-ink-3">Nothing is dialled automatically: every call is started by a person from Call-backs, after a confirmation.</p>
            </>
          )}
        </div>
      </Panel>
    </div>
  );
}

/* ---------- doctors ---------- */

function DoctorSettings() {
  const { canWrite } = useStaff();
  const toast = useToast();
  const doctors = useSWR<AdminDoctor[]>("/api/admin/doctors");
  const [busy, setBusy] = useState<string | null>(null);

  async function patch(doctor: AdminDoctor, body: Partial<Pick<AdminDoctor, "name" | "active">>) {
    setBusy(doctor.id);
    try {
      await api(`/api/admin/doctors/${encodeURIComponent(doctor.id)}`, { method: "PATCH", body });
      await doctors.mutate();
      toast.show(body.active === undefined ? "Dentist renamed." : body.active ? `${doctor.name} is bookable again.` : `${doctor.name} is no longer bookable.`);
    } catch (error) {
      toast.show(errorMessage(error), "error");
    } finally {
      setBusy(null);
    }
  }

  if (doctors.error) return <ErrorState message={errorMessage(doctors.error)} />;
  if (!doctors.data) return <Skeleton className="h-48" />;
  if (!doctors.data.length) return <EmptyState title="No dentists yet">Dentists are added during clinic setup.</EmptyState>;

  return (
    <ul className="divide-y divide-line rounded-lg border border-line bg-raised">
      {doctors.data.map((d) => (
        <DoctorRow key={d.id} doctor={d} canWrite={canWrite} busy={busy === d.id} onPatch={(body) => void patch(d, body)} />
      ))}
    </ul>
  );
}

function DoctorRow({ doctor, canWrite, busy, onPatch }: { doctor: AdminDoctor; canWrite: boolean; busy: boolean; onPatch: (body: Partial<Pick<AdminDoctor, "name" | "active">>) => void }) {
  const [name, setName] = useState(doctor.name);
  useEffect(() => setName(doctor.name), [doctor.name]);
  return (
    <li className="flex flex-col gap-3 px-4 py-3 sm:flex-row sm:items-center">
      <div className="flex-1">
        {canWrite ? (
          <label className="sr-only" htmlFor={`doctor-${doctor.id}`}>
            Name
          </label>
        ) : null}
        {canWrite ? (
          <Input id={`doctor-${doctor.id}`} value={name} onChange={(e) => setName(e.target.value)} className="max-w-sm" />
        ) : (
          <p className="font-semibold">{doctor.name}</p>
        )}
        <p className="mt-1 text-xs text-ink-3">{doctor.booking_calendar_id ? `Calendar: ${doctor.booking_calendar_id.startsWith("ledger:") ? "clinic ledger" : "Google Calendar"}` : "No booking calendar"}</p>
      </div>
      <Badge tone={doctor.active ? "ok" : "neutral"}>{doctor.active ? "Bookable" : "Inactive"}</Badge>
      {canWrite ? (
        <div className="flex gap-2">
          <Button size="sm" variant="secondary" disabled={busy || !name.trim() || name.trim() === doctor.name} onClick={() => onPatch({ name: name.trim() })}>
            Rename
          </Button>
          <Button size="sm" variant={doctor.active ? "ghost" : "secondary"} loading={busy} onClick={() => onPatch({ active: !doctor.active })}>
            {doctor.active ? "Deactivate" : "Activate"}
          </Button>
        </div>
      ) : null}
    </li>
  );
}

/* ---------- services ---------- */

function ServiceSettings() {
  const { canWrite } = useStaff();
  const toast = useToast();
  const catalog = useCatalog();
  const services = useSWR<AdminService[]>("/api/admin/services");
  const [busy, setBusy] = useState<string | null>(null);

  async function patch(service: AdminService, body: Partial<Pick<AdminService, "duration_minutes" | "buffer_after_minutes" | "active">>) {
    setBusy(service.id);
    try {
      await api(`/api/admin/services/${encodeURIComponent(service.id)}`, { method: "PATCH", body });
      await services.mutate();
      toast.show(`${service.name} updated.`);
    } catch (error) {
      toast.show(errorMessage(error), "error");
    } finally {
      setBusy(null);
    }
  }

  const grouped = useMemo(() => {
    const map = new Map<string, AdminService[]>();
    for (const s of services.data ?? []) map.set(s.doctor_id, [...(map.get(s.doctor_id) ?? []), s]);
    return [...map.entries()];
  }, [services.data]);

  if (services.error) return <ErrorState message={errorMessage(services.error)} />;
  if (!services.data) return <Skeleton className="h-48" />;
  if (!services.data.length) return <EmptyState title="No services yet" />;

  return (
    <div className="flex flex-col gap-6">
      {grouped.map(([doctorId, list]) => (
        <Panel key={doctorId} title={catalog.doctorNames.get(doctorId) ?? "Dentist"}>
          <ul className="divide-y divide-line">
            {list.map((s) => (
              <ServiceRow key={s.id} service={s} canWrite={canWrite} busy={busy === s.id} onPatch={(body) => void patch(s, body)} />
            ))}
          </ul>
        </Panel>
      ))}
    </div>
  );
}

function ServiceRow({
  service,
  canWrite,
  busy,
  onPatch,
}: {
  service: AdminService;
  canWrite: boolean;
  busy: boolean;
  onPatch: (body: Partial<Pick<AdminService, "duration_minutes" | "buffer_after_minutes" | "active">>) => void;
}) {
  const [duration, setDuration] = useState(service.duration_minutes);
  const [buffer, setBuffer] = useState(service.buffer_after_minutes);
  useEffect(() => {
    setDuration(service.duration_minutes);
    setBuffer(service.buffer_after_minutes);
  }, [service.duration_minutes, service.buffer_after_minutes]);
  const changed = duration !== service.duration_minutes || buffer !== service.buffer_after_minutes;
  const valid = duration >= 5 && duration <= 240 && buffer >= 0 && buffer <= 120;
  return (
    <li className="flex flex-col gap-3 px-4 py-3 lg:flex-row lg:items-end">
      <div className="flex-1">
        <p className="font-semibold">{service.name}</p>
        <Badge tone={service.active ? "ok" : "neutral"} className="mt-1">
          {service.active ? "Offered" : "Not offered"}
        </Badge>
      </div>
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Minutes">
          {(p) => <Input {...p} type="number" min={5} max={240} value={duration} disabled={!canWrite} onChange={(e) => setDuration(Number(e.target.value))} className="w-24" />}
        </Field>
        <Field label="Buffer after">
          {(p) => <Input {...p} type="number" min={0} max={120} value={buffer} disabled={!canWrite} onChange={(e) => setBuffer(Number(e.target.value))} className="w-24" />}
        </Field>
        {canWrite ? (
          <>
            <Button size="sm" variant="secondary" disabled={!changed || !valid || busy} onClick={() => onPatch({ duration_minutes: duration, buffer_after_minutes: buffer })}>
              Save
            </Button>
            <Button size="sm" variant="ghost" loading={busy} onClick={() => onPatch({ active: !service.active })}>
              {service.active ? "Stop offering" : "Offer again"}
            </Button>
          </>
        ) : null}
      </div>
    </li>
  );
}

/* ---------- hours ---------- */

export function validateHours(windows: readonly HoursWindow[]): string | null {
  for (const w of windows) {
    if (!/^\d{2}:\d{2}$/.test(w.opens) || !/^\d{2}:\d{2}$/.test(w.closes)) return "Every window needs an opening and closing time.";
    if (w.opens >= w.closes) return `${WEEKDAYS[w.weekday]}: opening must be before closing.`;
  }
  for (let day = 0; day < 7; day++) {
    const list = windows.filter((w) => w.weekday === day).sort((a, b) => a.opens.localeCompare(b.opens));
    for (let i = 1; i < list.length; i++) {
      if (list[i]!.opens < list[i - 1]!.closes) return `${WEEKDAYS[day]}: windows overlap.`;
    }
  }
  if (windows.length > 50) return "At most 50 windows.";
  return null;
}

function HoursSettings() {
  const { canWrite } = useStaff();
  const toast = useToast();
  const doctors = useSWR<AdminDoctor[]>("/api/admin/doctors");
  const [doctorId, setDoctorId] = useState<string>("");
  useEffect(() => {
    if (!doctorId && doctors.data?.[0]) setDoctorId(doctors.data[0].id);
  }, [doctors.data, doctorId]);
  const hours = useSWR<HoursWindow[]>(doctorId ? `/api/admin/doctors/${encodeURIComponent(doctorId)}/hours` : null);
  const [draft, setDraft] = useState<HoursWindow[] | null>(null);
  const [saving, setSaving] = useState(false);
  useEffect(() => setDraft(hours.data ? [...hours.data] : null), [hours.data]);

  const problem = draft ? validateHours(draft) : null;

  async function save() {
    if (!draft || problem) return;
    setSaving(true);
    try {
      const saved = await api<HoursWindow[]>(`/api/admin/doctors/${encodeURIComponent(doctorId)}/hours`, { method: "PUT", body: draft });
      await hours.mutate(saved, { revalidate: false });
      toast.show("Working hours saved.");
    } catch (error) {
      toast.show(errorMessage(error), "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <Field label="Dentist" className="max-w-sm">
        {(p) => (
          <Select {...p} value={doctorId} onChange={(e) => setDoctorId(e.target.value)}>
            {(doctors.data ?? []).map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
              </option>
            ))}
          </Select>
        )}
      </Field>
      {hours.error ? (
        <ErrorState message={errorMessage(hours.error)} />
      ) : !draft ? (
        <Skeleton className="h-64" />
      ) : (
        <Panel>
          <ul className="divide-y divide-line">
            {WEEKDAYS.map((label, weekday) => {
              const windows = draft.map((w, index) => ({ w, index })).filter(({ w }) => w.weekday === weekday);
              return (
                <li key={label} className="flex flex-col gap-2 px-4 py-3 sm:flex-row sm:items-start">
                  <p className="w-32 shrink-0 pt-2 font-semibold">{label}</p>
                  <div className="flex flex-1 flex-col gap-2">
                    {windows.length === 0 ? <p className="pt-2 text-sm text-ink-3">Not working</p> : null}
                    {windows.map(({ w, index }) => (
                      <div key={index} className="flex flex-wrap items-center gap-2">
                        <label className="sr-only" htmlFor={`opens-${index}`}>
                          {label} opens
                        </label>
                        <Input
                          id={`opens-${index}`}
                          type="time"
                          step={900}
                          value={w.opens}
                          disabled={!canWrite}
                          onChange={(e) => setDraft((d) => d!.map((x, i) => (i === index ? { ...x, opens: e.target.value } : x)))}
                          className="w-32"
                        />
                        <span className="text-ink-3">to</span>
                        <label className="sr-only" htmlFor={`closes-${index}`}>
                          {label} closes
                        </label>
                        <Input
                          id={`closes-${index}`}
                          type="time"
                          step={900}
                          value={w.closes}
                          disabled={!canWrite}
                          onChange={(e) => setDraft((d) => d!.map((x, i) => (i === index ? { ...x, closes: e.target.value } : x)))}
                          className="w-32"
                        />
                        {canWrite ? (
                          <Button size="sm" variant="ghost" aria-label={`Remove ${label} ${w.opens} to ${w.closes}`} onClick={() => setDraft((d) => d!.filter((_, i) => i !== index))}>
                            <Trash2 className="size-4" aria-hidden="true" />
                          </Button>
                        ) : null}
                      </div>
                    ))}
                  </div>
                  {canWrite ? (
                    <Button
                      size="sm"
                      variant="ghost"
                      icon={<Plus className="size-4" aria-hidden="true" />}
                      onClick={() => setDraft((d) => [...d!, { weekday, opens: "09:30", closes: "13:30" }])}
                    >
                      Add hours
                    </Button>
                  ) : null}
                </li>
              );
            })}
          </ul>
          {canWrite ? (
            <div className="flex flex-wrap items-center gap-3 border-t border-line px-4 py-3">
              <Button onClick={() => void save()} loading={saving} disabled={!!problem}>
                Save hours
              </Button>
              {problem ? (
                <p role="alert" className="text-sm font-medium text-bad">
                  {problem}
                </p>
              ) : (
                <p className="text-sm text-ink-3">Saving replaces this dentist&apos;s whole week. Existing appointments are kept.</p>
              )}
            </div>
          ) : null}
        </Panel>
      )}
    </div>
  );
}

/* ---------- exceptions ---------- */

function ExceptionSettings() {
  const { canWrite, timeZone } = useStaff();
  const toast = useToast();
  const doctors = useSWR<AdminDoctor[]>("/api/admin/doctors");
  const [today, setToday] = useState<string | null>(null);
  useEffect(() => setToday(todayKey(timeZone)), [timeZone]);
  const key = today ? withQuery("/api/admin/exceptions", { from: zonedToIso(today, timeZone), to: zonedToIso(addDays(today, 180), timeZone) }) : null;
  const list = useSWR<ScheduleException[]>(key);
  const [scope, setScope] = useState("clinic");
  const [kind, setKind] = useState<ScheduleException["kind"]>("leave");
  const [starts, setStarts] = useState("");
  const [ends, setEnds] = useState("");
  const [label, setLabel] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const doctorName = new Map((doctors.data ?? []).map((d) => [d.id, d.name]));

  async function add(event: FormEvent) {
    event.preventDefault();
    setError(null);
    let from: string;
    let to: string;
    try {
      from = zonedToIso(starts, timeZone);
      to = zonedToIso(ends, timeZone);
    } catch (err) {
      setError(errorMessage(err));
      return;
    }
    if (Date.parse(to) <= Date.parse(from)) {
      setError("The end must be after the start.");
      return;
    }
    setBusy(true);
    try {
      const created = await api<ExceptionCreated>("/api/admin/exceptions", {
        method: "POST",
        body: { kind, starts_at: from, ends_at: to, ...(scope !== "clinic" ? { doctor_id: scope } : {}), ...(label.trim() ? { label: label.trim() } : {}) },
      });
      await list.mutate();
      setLabel("");
      toast.show(
        created.conflict_count
          ? `Saved. ${created.conflict_count} existing appointment${created.conflict_count === 1 ? "" : "s"} overlap it and still need to be moved or cancelled.`
          : "Saved. No existing appointments are affected.",
        created.conflict_count ? "info" : "success",
      );
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function remove(item: ScheduleException) {
    try {
      await api(`/api/admin/exceptions/${encodeURIComponent(item.id)}`, { method: "DELETE" });
      await list.mutate();
      toast.show("Removed.");
    } catch (err) {
      toast.show(errorMessage(err), "error");
    }
  }

  return (
    <div className="grid gap-6 xl:grid-cols-[1.3fr_1fr]">
      <Panel title="Upcoming closures, leave and breaks">
        {list.error ? (
          <ErrorState className="m-4" message={errorMessage(list.error)} />
        ) : !list.data ? (
          <Skeleton className="m-4 h-32" />
        ) : list.data.length === 0 ? (
          <p className="px-4 py-6 text-sm text-ink-2">Nothing scheduled in the next six months.</p>
        ) : (
          <ul className="divide-y divide-line">
            {list.data.map((e) => (
              <li key={e.id} className="flex items-start justify-between gap-3 px-4 py-3">
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <LabelBadge label={exceptionKind(e.kind)} />
                    <span className="font-semibold">{e.doctor_id ? (doctorName.get(e.doctor_id) ?? "Dentist") : "Whole clinic"}</span>
                    {e.label ? <span className="text-ink-2">{e.label}</span> : null}
                  </div>
                  <p className="mt-1 text-sm text-ink-2 tnum">
                    {formatShortDate(e.starts_at, timeZone)} {formatTime(e.starts_at, timeZone)} to {formatShortDate(e.ends_at, timeZone)} {formatTime(e.ends_at, timeZone)}
                  </p>
                </div>
                {canWrite ? (
                  <Button size="sm" variant="ghost" aria-label={`Remove ${exceptionKind(e.kind).text} ${e.label}`} onClick={() => void remove(e)}>
                    <Trash2 className="size-4" aria-hidden="true" />
                  </Button>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Panel>
      {canWrite ? (
        <Panel title="Add a closure or leave">
          <form onSubmit={add} noValidate className="flex flex-col gap-4 p-4">
            <Field label="Applies to">
              {(p) => (
                <Select {...p} value={scope} onChange={(e) => setScope(e.target.value)}>
                  <option value="clinic">Whole clinic</option>
                  {(doctors.data ?? []).map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.name}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <Field label="Type">
              {(p) => (
                <Select {...p} value={kind} onChange={(e) => setKind(e.target.value as ScheduleException["kind"])}>
                  <option value="holiday">Holiday</option>
                  <option value="leave">Leave</option>
                  <option value="break">Break</option>
                </Select>
              )}
            </Field>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Starts">{(p) => <Input {...p} type="datetime-local" value={starts} onChange={(e) => setStarts(e.target.value)} />}</Field>
              <Field label="Ends">{(p) => <Input {...p} type="datetime-local" value={ends} onChange={(e) => setEnds(e.target.value)} />}</Field>
            </div>
            <Field label="Label" optional hint="An operational note such as 'Diwali'. Never patient details.">
              {(p) => <Input {...p} maxLength={80} value={label} onChange={(e) => setLabel(e.target.value)} />}
            </Field>
            {error ? (
              <p role="alert" className="text-sm font-medium text-bad">
                {error}
              </p>
            ) : null}
            <div>
              <Button type="submit" loading={busy} disabled={!starts || !ends}>
                Add
              </Button>
            </div>
            <p className="text-xs text-ink-3">Existing appointments inside the period are not cancelled; you&apos;ll be told how many overlap.</p>
          </form>
        </Panel>
      ) : null}
    </div>
  );
}
