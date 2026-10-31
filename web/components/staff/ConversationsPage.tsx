"use client";

import { MessageCircle, Phone } from "lucide-react";
import { Fragment, useState } from "react";
import useSWR from "swr";

import { withQuery } from "@/lib/api/client";
import type { ConversationDetail, ConversationSummary } from "@/lib/api/types";
import { cx } from "@/lib/cx";
import { appointmentStatus, outcome as outcomeLabel } from "@/lib/labels";
import { formatShortDate, formatTime } from "@/lib/time";
import { LabelBadge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Drawer } from "@/components/ui/Modal";
import { EmptyState, ErrorState, Loading, Skeleton } from "@/components/ui/States";
import { Tabs } from "@/components/ui/Tabs";
import { PageHeader } from "./PageHeader";
import { useStaff } from "./StaffContext";

type Filter = "all" | "chat" | "voice";

function Transcript({ id }: { id: string }) {
  const { timeZone } = useStaff();
  const detail = useSWR<ConversationDetail>(`/api/staff/conversations/${encodeURIComponent(id)}`);
  if (detail.error) return <ErrorState message="This conversation couldn't be loaded." />;
  if (!detail.data) {
    return (
      <Loading label="Loading conversation" className="flex flex-col gap-3">
        <Skeleton className="h-12 w-3/4" />
        <Skeleton className="ml-auto h-10 w-2/3" />
        <Skeleton className="h-16 w-4/5" />
      </Loading>
    );
  }
  const c = detail.data;
  const hidden = c.messages.some((m) => m.text === null);
  // Provider codes ("inbound", "user_hangup") read as words; the length is already text.
  const code = (value: string | null) => (value ? value.replace(/[-_]/g, " ").replace(/^./, (ch) => ch.toUpperCase()) : null);
  const call: [string, string | null][] =
    c.channel === "voice"
      ? [
          ["Call", code(c.call_type)],
          ["Length", c.duration_seconds === null ? null : formatSeconds(c.duration_seconds)],
          ["Ended by", code(c.hangup_by)],
          ["Provider status", code(c.provider_status)],
        ]
      : [];
  return (
    <div className="flex flex-col gap-5">
      <p className="text-sm text-ink-2 tnum">
        {formatShortDate(c.started_at, timeZone)}, {formatTime(c.started_at, timeZone)}
        {c.channel === "chat" ? ` · ${c.turns} ${c.turns === 1 ? "turn" : "turns"}` : ""}
        {c.ended_at ? ` · ended ${formatTime(c.ended_at, timeZone)}` : c.last_active_at ? ` · last active ${formatTime(c.last_active_at, timeZone)}` : ""}
      </p>

      {call.some(([, value]) => value) ? (
        <dl className="grid grid-cols-[8rem_1fr] gap-x-4 gap-y-2 rounded-md border border-line bg-paper p-4 text-sm">
          {call
            .filter(([, value]) => value)
            .map(([label, value]) => (
              <Fragment key={label}>
                <dt className="text-ink-3">{label}</dt>
                <dd>{value}</dd>
              </Fragment>
            ))}
        </dl>
      ) : null}

      {c.appointments.length ? (
        <div>
          <h3 className="text-xs font-semibold tracking-wide text-ink-3 uppercase">Appointments from this conversation</h3>
          <ul className="mt-2 flex flex-col divide-y divide-line rounded-md border border-line bg-paper">
            {c.appointments.map((a) => (
              <li key={a.id} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-sm">
                <span className="tnum">
                  {formatShortDate(a.starts_at, timeZone)}, {formatTime(a.starts_at, timeZone)} · <span className="font-mono tracking-wider">{a.reference}</span>
                </span>
                <LabelBadge label={appointmentStatus(a.status)} />
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {hidden ? <p className="rounded-md border border-line bg-sunk/70 px-3 py-2 text-sm text-ink-2">Message text is hidden for your role.</p> : null}

      {c.messages.length ? (
        <ol className="flex flex-col gap-2">
          {c.messages.map((m, i) =>
            m.role === "user" || m.role === "assistant" ? (
              <li key={i} className={cx("flex", m.role === "user" ? "justify-end" : "justify-start")}>
                <p
                  className={cx(
                    "max-w-[85%] rounded-lg px-3 py-2 text-[0.95rem] leading-relaxed",
                    m.role === "user" ? "bg-ink text-paper" : "border border-line bg-paper",
                    m.text === null && "italic opacity-70",
                  )}
                >
                  <span className="sr-only">{m.role === "user" ? "Patient: " : "Assistant: "}</span>
                  {m.text ?? (m.role === "user" ? "Patient message" : "Assistant reply")}
                </p>
              </li>
            ) : (
              // Tool calls and system notes: one quiet line, never raw payloads.
              <li key={i} className="px-1 text-xs text-ink-3">
                {m.role === "tool" ? `Assistant used ${m.tool_name ?? "a tool"}` : "Note"}
                {m.text ? `: ${m.role === "tool" && m.tool_name ? m.text.replace(`${m.tool_name}: `, "") : m.text}` : ""}
              </li>
            ),
          )}
        </ol>
      ) : (
        <p className="text-ink-2">{c.channel === "voice" ? "No transcript is stored for phone calls." : "No messages in this conversation."}</p>
      )}
    </div>
  );
}

function formatSeconds(total: number): string {
  const minutes = Math.floor(total / 60);
  const seconds = Math.round(total % 60);
  return minutes ? `${minutes} min ${seconds} s` : `${seconds} s`;
}

export function ConversationsPage() {
  const { timeZone } = useStaff();
  const [filter, setFilter] = useState<Filter>("all");
  const [selected, setSelected] = useState<ConversationSummary | null>(null);
  const list = useSWR<ConversationSummary[]>(withQuery("/api/staff/conversations", { channel: filter === "all" ? undefined : filter }));

  return (
    <div>
      <PageHeader title="Conversations" description="Chat transcripts and phone call summaries. Treat them as confidential patient information." />
      <Tabs
        label="Channel"
        value={filter}
        onChange={setFilter}
        items={[
          { key: "all", label: "All" },
          { key: "chat", label: "Chat" },
          { key: "voice", label: "Phone" },
        ]}
      >
        {list.error ? (
          <ErrorState
            message="Conversations couldn't be loaded."
            action={
              <Button size="sm" variant="secondary" onClick={() => void list.mutate()}>
                Try again
              </Button>
            }
          />
        ) : !list.data ? (
          <Loading label="Loading conversations" className="flex flex-col gap-2">
            {Array.from({ length: 5 }, (_, i) => (
              <Skeleton key={i} className="h-14" />
            ))}
          </Loading>
        ) : list.data.length === 0 ? (
          <EmptyState title="No conversations yet">Chats from the website and calls to the receptionist line appear here.</EmptyState>
        ) : (
          <ul className="divide-y divide-line rounded-lg border border-line bg-raised">
            {list.data.map((c) => {
              const Icon = c.channel === "voice" ? Phone : MessageCircle;
              return (
                <li key={c.id}>
                  <button type="button" onClick={() => setSelected(c)} className="flex w-full items-center gap-4 px-4 py-3 text-left hover:bg-sunk/50">
                    <Icon className="size-4 shrink-0 text-ink-3" aria-hidden="true" />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate font-semibold">{c.patient_name ?? "Unidentified"}</span>
                      <span className="block text-sm text-ink-2 tnum">
                        {c.channel === "voice" ? "Phone" : "Chat"} · {formatShortDate(c.started_at, timeZone)}, {formatTime(c.started_at, timeZone)}
                        {c.channel === "chat" ? ` · ${c.turns} ${c.turns === 1 ? "turn" : "turns"}` : ""}
                      </span>
                    </span>
                    {c.outcome ? <LabelBadge label={outcomeLabel(c.outcome)} /> : null}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </Tabs>
      <Drawer
        open={selected !== null}
        onClose={() => setSelected(null)}
        title={selected ? (selected.patient_name ?? "Unidentified") : "Conversation"}
        description={selected ? (selected.channel === "voice" ? "Phone call" : "Website chat") : undefined}
      >
        {selected ? <Transcript id={selected.id} /> : null}
      </Drawer>
    </div>
  );
}
