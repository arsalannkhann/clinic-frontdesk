"use client";

import { Phone } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import useSWR from "swr";

import { api, ApiError, errorMessage, withQuery } from "@/lib/api/client";
import type { CallbackItem, CallbackStatus, CallStarted, OutboundStatus } from "@/lib/api/types";
import { CALL_REFUSALS, callbackSource, callbackStatus } from "@/lib/labels";
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
    const yes = await confirm({
      title: `Call ${item.name} now?`,
      body: `This places a phone call to the patient now, on ${item.phone_masked}, from the clinic's calling number. It starts as soon as you confirm and can't be called off from here.`,
      confirmLabel: "Call now",
      cancelLabel: "Not now",
      tone: "primary",
    });
    if (!yes) return;
    setBusy(`call:${item.id}`);
    try {
      const started = await api<CallStarted>(`/api/staff/callbacks/${encodeURIComponent(item.id)}/call`, { method: "POST", body: {} });
      toast.show(`Calling ${item.name}. Execution ${started.execution_id.slice(0, 8)}.`);
      void list.mutate();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409 && (error.code === "outbound_disabled" || error.code === "no_from_number")) {
        setRefusal(error.code);
        void outbound.mutate();
      } else {
        toast.show(errorMessage(error), "error");
      }
    } finally {
      setBusy(null);
    }
  }

  async function resolve(item: CallbackItem, status: "done" | "dismissed") {
    setBusy(`${status}:${item.id}`);
    try {
      await api(`/api/staff/callbacks/${encodeURIComponent(item.id)}/resolve`, { method: "POST", body: { status } });
      toast.show(status === "done" ? `Marked ${item.name} as called back.` : `Dismissed ${item.name}'s request.`);
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
            {list.data.map((item) => (
              <li key={item.id} className="flex flex-col gap-3 rounded-lg border border-line bg-raised p-4 sm:flex-row sm:items-start sm:justify-between">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <p className="font-semibold">{item.name}</p>
                    <span className="text-sm text-ink-3 tnum">{item.phone_masked}</span>
                    <Badge dot={false}>{callbackSource(item.source).text}</Badge>
                    {item.status !== "open" ? <LabelBadge label={callbackStatus(item.status)} /> : null}
                  </div>
                  <p className="mt-1 text-[0.95rem] text-ink-2">{item.reason ?? <span className="text-ink-3">No reason given</span>}</p>
                  <p className="mt-1 text-xs text-ink-3 tnum">
                    Requested {formatShortDate(item.created_at, timeZone)}, {formatTime(item.created_at, timeZone)}
                  </p>
                </div>
                {canWrite && (item.status === "open" || item.status === "calling") ? (
                  <div className="flex shrink-0 flex-wrap gap-2">
                    {item.status === "open" ? (
                      <Button
                        size="sm"
                        icon={<Phone className="size-4" aria-hidden="true" />}
                        disabled={!!reason || checking || !!busy}
                        aria-describedby={reason ? "call-reason" : undefined}
                        title={reason ?? undefined}
                        loading={busy === `call:${item.id}`}
                        onClick={() => void call(item)}
                      >
                        Call now
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
            ))}
          </ul>
        )}
      </Tabs>
      {confirmDialog}
    </div>
  );
}
