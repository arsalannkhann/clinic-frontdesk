"use client";

import { Phone, RotateCcw } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import useSWR from "swr";

import { api, ApiError, errorMessage, withQuery } from "@/lib/api/client";
import type { CallbackItem, CallbackReopened, CallbackResolved, CallbackStatus, CallStarted, OutboundStatus } from "@/lib/api/types";
import {
  CALL_FAILURES,
  CALL_OUTCOMES,
  CALL_REFUSALS,
  callbackSource,
  callbackStatus,
  DEMO_RECORD,
  LAST_CALL_FAILED,
  REOPEN_FAILURES,
} from "@/lib/labels";
import { cx } from "@/lib/cx";
import { formatShortDate, formatTime } from "@/lib/time";
import { Badge, LabelBadge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { useConfirm } from "@/components/ui/ConfirmDialog";
import { EmptyState, ErrorState, Loading, Skeleton } from "@/components/ui/States";
import { Tabs } from "@/components/ui/Tabs";
import { useToast } from "@/components/ui/Toast";
import { PageHeader } from "./PageHeader";
import { useStaff } from "./StaffContext";

const TABS: { key: CallbackStatus; label: string }[] = [
  { key: "open", label: "Open" },
  { key: "calling", label: "Calling" },
  { key: "done", label: "Done" },
  { key: "dismissed", label: "Dismissed" },
];

/** The first thing outbound calling still needs, as a refusal code, or null when ready. */
export function outboundGap(outbound: boolean | OutboundStatus): string | null {
  if (typeof outbound === "boolean") return outbound ? null : "outbound_disabled";
  if (!outbound.has_from_number) return "no_from_number";
  if (!outbound.has_outbound_agent) return "no_outbound_agent";
  if (!outbound.enabled) return "outbound_disabled";
  return null;
}

/**
 * Why "Call now" is unavailable, before or after trying. A 409 from the API wins; otherwise
 * the outbound status (or, until it loads, the clinic's outbound setting) is checked up front
 * so staff don't click a button that can't work.
 */
export function callBlockReason(outbound: boolean | OutboundStatus, refusal: string | null, canWrite: boolean): string | null {
  if (!canWrite) return "Only the owner or an admin can place calls.";
  if (refusal) return CALL_REFUSALS[refusal] ?? refusal;
  const gap = outboundGap(outbound);
  return gap ? CALL_REFUSALS[gap]! : null;
}

/**
 * What a failed Call now means. Clinic-wide refusals disable every Call now with the reason;
 * the rest are about this call and show as a toast, by code (5xx messages are generic).
 */
export function callFailure(error: unknown): { code: string; text: string; clinicWide: boolean; refresh: boolean } {
  const code = error instanceof ApiError ? (error.code) : "";
  if (code in CALL_REFUSALS) return { code, text: CALL_REFUSALS[code]!, clinicWide: true, refresh: false };
  if (code in CALL_FAILURES) {
    return { code, text: CALL_FAILURES[code]!, clinicWide: false, refresh: code === "callback_resolved" || code === "synthetic_contact" || code === "call_failed" };
  }
  return { code, text: errorMessage(error), clinicWide: false, refresh: false };
}

/**
 * What the current call's outcome means for staff, or null when there is nothing to say: in
 * Calling, the outcome of the call; on an open request, a note that its last call failed.
 */
export function callOutcomeNote(item: Pick<CallbackItem, "status" | "call_outcome">): string | null {
  const outcome = item.call_outcome ?? null;
  if (!outcome) return null;
  if (item.status === "calling") return CALL_OUTCOMES[outcome] ?? null;
  if (item.status === "open" && outcome === "failed") return LAST_CALL_FAILED;
  return null;
}

/** Reopen is offered on a Calling request once its call has an outcome (owner/admin only). */
export function canReopen(item: Pick<CallbackItem, "status" | "call_outcome">, canWrite: boolean): boolean {
  return canWrite && item.status === "calling" && !!item.call_outcome && item.call_outcome in CALL_OUTCOMES;
}

/** What a refused reopen means, and whether the list is stale (the request moved on). */
export function reopenFailure(error: unknown): { text: string; refresh: boolean } {
  const code = error instanceof ApiError ? (error.status === 404 ? "not_found" : error.code) : "";
  if (code in REOPEN_FAILURES) return { text: REOPEN_FAILURES[code]!, refresh: code !== "call_in_progress" };
  return { text: errorMessage(error), refresh: false };
}

export function CallbacksPage() {
  const { clinic, canWrite, timeZone } = useStaff();
  const toast = useToast();
  const [confirm, confirmDialog] = useConfirm();
  const [tab, setTab] = useState<CallbackStatus>("open");
  const [refusal, setRefusal] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const list = useSWR<CallbackItem[]>(withQuery("/api/staff/callbacks", { status: tab }), { refreshInterval: 30_000 });
  const outbound = useSWR<OutboundStatus>(canWrite ? "/api/staff/outbound-status" : null, { refreshInterval: 60_000 });
  // The outbound status decides; if it can't be read, the clinic's own setting does (the API
  // still refuses a call it can't place). While it loads, Call now waits, without a message.
  const reason = callBlockReason(outbound.data ?? clinic.outbound_enabled, refusal, canWrite);
  const checking = canWrite && !outbound.data && !outbound.error;

  async function call(item: CallbackItem) {
    const who = item.name ?? "this patient";
    const yes = await confirm({
      title: `Call ${who} now?`,
      body: `This places a phone call to the patient now${item.phone_masked ? `, on ${item.phone_masked}` : ""}, from the clinic's calling number. It starts as soon as you confirm and can't be called off from here.`,
      confirmLabel: "Call now",
      cancelLabel: "Not now",
      tone: "primary",
    });
    if (!yes) return;
    setBusy(`call:${item.id}`);
    try {
      const started = await api<CallStarted>(`/api/staff/callbacks/${encodeURIComponent(item.id)}/call`, { method: "POST", body: {} });
      toast.show(
        started.execution_id
          ? `Calling ${who}.`
          : `The call to ${who} was requested, but the provider hasn't confirmed it. It won't be redialled; check before trying again.`,
      );
      void list.mutate();
    } catch (error) {
      const message = callFailure(error);
      if (message.clinicWide) {
        setRefusal(message.code);
        void outbound.mutate();
      } else {
        toast.show(message.text, "error");
        if (message.refresh) void list.mutate();
      }
    } finally {
      setBusy(null);
    }
  }

  async function reopen(item: CallbackItem) {
    const who = item.name ?? "this patient";
    const yes = await confirm({
      title: `Reopen the request from ${who}?`,
      body: "This moves the request back to Open so you can try again with Call now. Nothing is dialled until someone presses Call now, and the earlier call still counts towards that day's contact limit.",
      confirmLabel: "Reopen",
      cancelLabel: "Keep as is",
      tone: "primary",
    });
    if (!yes) return;
    setBusy(`reopen:${item.id}`);
    try {
      await api<CallbackReopened>(`/api/staff/callbacks/${encodeURIComponent(item.id)}/reopen`, { method: "POST", body: {} });
      toast.show(`Reopened the request from ${who}. It's back under Open.`);
      void list.mutate();
    } catch (error) {
      const failure = reopenFailure(error);
      toast.show(failure.text, "error");
      if (failure.refresh) void list.mutate();
    } finally {
      setBusy(null);
    }
  }

  async function resolve(item: CallbackItem, status: "done" | "dismissed") {
    const who = item.name ?? "the patient";
    setBusy(`${status}:${item.id}`);
    try {
      await api<CallbackResolved>(`/api/staff/callbacks/${encodeURIComponent(item.id)}/resolve`, { method: "POST", body: { status } });
      toast.show(status === "done" ? `Marked ${who} as called back.` : `Dismissed ${who}'s request.`);
      void list.mutate();
    } catch (error) {
      toast.show(errorMessage(error), "error");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div>
      <PageHeader title="Call-backs" description="Patients who asked the clinic to ring them. Nothing is dialled automatically." />
      <Tabs label="Call-back status" items={TABS} value={tab} onChange={setTab}>
        {reason && !checking && tab === "open" && canWrite ? (
          <p className="mb-4 rounded-md border border-warn/25 bg-warn-tint/60 px-3 py-2 text-sm text-ink" id="call-reason">
            <span className="font-semibold">Call now is unavailable.</span> {reason}{" "}
            <Link href="/staff/settings#outbound" className="font-semibold text-accent underline-offset-4 hover:underline">
              See what&apos;s needed
            </Link>
          </p>
        ) : null}
        {list.error ? (
          <ErrorState
            message="The call-back queue couldn't be loaded."
            action={
              <Button size="sm" variant="secondary" onClick={() => void list.mutate()}>
                Try again
              </Button>
            }
          />
        ) : !list.data ? (
          <Loading label="Loading call-backs" className="flex flex-col gap-2">
            {Array.from({ length: 4 }, (_, i) => (
              <Skeleton key={i} className="h-20" />
            ))}
          </Loading>
        ) : list.data.length === 0 ? (
          <EmptyState title={tab === "open" ? "No one is waiting" : `No ${tab} requests`}>
            {tab === "open" ? "Requests from the website, chat and phone line appear here." : null}
          </EmptyState>
        ) : (
          <ul className="flex flex-col gap-3">
            {list.data.map((item) => {
              const note = callOutcomeNote(item);
              return (
              <li key={item.id} className="flex flex-col gap-3 rounded-lg border border-line bg-raised p-4 sm:flex-row sm:items-start sm:justify-between">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <p className="font-semibold">{item.name ?? <span className="font-normal text-ink-3">Name hidden for your role</span>}</p>
                    {item.phone_masked ? <span className="text-sm text-ink-3 tnum">{item.phone_masked}</span> : null}
                    <Badge dot={false}>{callbackSource(item.source).text}</Badge>
                    {item.status !== "open" ? <LabelBadge label={callbackStatus(item.status)} /> : null}
                    {item.synthetic ? <Badge dot={false}>Demo</Badge> : null}
                  </div>
                  <p className="mt-1 text-[0.95rem] text-ink-2">{item.reason ?? <span className="text-ink-3">No reason given</span>}</p>
                  <p className="mt-1 text-xs text-ink-3 tnum">
                    Requested {formatShortDate(item.created_at, timeZone)}, {formatTime(item.created_at, timeZone)}
                    {item.call_requested_at ? `. Call placed ${formatTime(item.call_requested_at, timeZone)}` : ""}
                  </p>
                  {note ? (
                    <p className={cx("mt-1 text-sm", item.call_outcome === "placed" ? "text-ink-2" : "font-semibold text-ink")}>{note}</p>
                  ) : null}
                  {item.synthetic && item.status === "open" && canWrite ? (
                    <p id={`demo-${item.id}`} className="mt-1 text-xs font-semibold text-ink-2">
                      {DEMO_RECORD}
                    </p>
                  ) : null}
                </div>
                {canWrite && (item.status === "open" || item.status === "calling") ? (
                  <div className="flex shrink-0 flex-wrap gap-2">
                    {item.status === "open" ? (
                      <Button
                        size="sm"
                        icon={<Phone className="size-4" aria-hidden="true" />}
                        disabled={!!reason || item.synthetic || checking || !!busy}
                        aria-describedby={item.synthetic ? `demo-${item.id}` : reason ? "call-reason" : undefined}
                        title={item.synthetic ? DEMO_RECORD : (reason ?? undefined)}
                        loading={busy === `call:${item.id}`}
                        onClick={() => void call(item)}
                      >
                        Call now
                      </Button>
                    ) : null}
                    {canReopen(item, canWrite) ? (
                      <Button
                        size="sm"
                        variant="secondary"
                        icon={<RotateCcw className="size-4" aria-hidden="true" />}
                        disabled={!!busy}
                        loading={busy === `reopen:${item.id}`}
                        onClick={() => void reopen(item)}
                      >
                        Reopen
                      </Button>
                    ) : null}
                    <Button size="sm" variant="secondary" disabled={!!busy} loading={busy === `done:${item.id}`} onClick={() => void resolve(item, "done")}>
                      Mark done
                    </Button>
                    <Button size="sm" variant="ghost" disabled={!!busy} loading={busy === `dismissed:${item.id}`} onClick={() => void resolve(item, "dismissed")}>
                      Dismiss
                    </Button>
                  </div>
                ) : null}
              </li>
              );
            })}
          </ul>
        )}
      </Tabs>
      {confirmDialog}
    </div>
  );
}
