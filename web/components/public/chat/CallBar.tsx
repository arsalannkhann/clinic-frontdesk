"use client";

import { Mic, MicOff, PhoneOff } from "lucide-react";

import { cx } from "@/lib/cx";

export type CallPhase = "connecting" | "listening" | "hearing" | "thinking" | "speaking";

const LABELS: Record<CallPhase, string> = {
  connecting: "Connecting…",
  listening: "Listening…",
  hearing: "Listening…",
  thinking: "Thinking…",
  speaking: "Speaking…",
};

export function callLabel(phase: CallPhase, muted: boolean): string {
  if (muted && phase !== "thinking" && phase !== "speaking") return "Muted";
  return LABELS[phase];
}

/** A pulsing orb: steady when reduced motion is preferred, muted or connecting. */
function Orb({ phase, muted, reduced }: { phase: CallPhase; muted: boolean; reduced: boolean }) {
  const live = !reduced && !muted && phase !== "connecting";
  const tone = muted ? "bg-ink-3" : phase === "thinking" ? "bg-ink-2" : "bg-accent";
  return (
    <span className="relative grid size-10 shrink-0 place-items-center" aria-hidden="true">
      {live ? (
        <span
          className={cx(
            "absolute inset-0 rounded-full",
            tone,
            phase === "thinking" ? "animate-blink" : "animate-orb",
            phase === "hearing" && "[animation-duration:900ms]",
          )}
        />
      ) : null}
      <span className={cx("relative size-6 rounded-full transition-transform", tone, live && phase === "hearing" && "scale-110")} />
    </span>
  );
}

export function CallBar({
  phase,
  muted,
  message,
  reduced,
  onMute,
  onEnd,
  onInterrupt,
}: {
  phase: CallPhase;
  muted: boolean;
  /** A brief note about the last turn ("Didn't catch that"). */
  message: string | null;
  reduced: boolean;
  onMute: () => void;
  onEnd: () => void;
  onInterrupt: () => void;
}) {
  return (
    <div role="group" aria-label="Voice call" className="flex items-center gap-3 border-b border-line bg-sunk/70 px-4 py-2.5">
      <Orb phase={phase} muted={muted} reduced={reduced} />
      <div className="min-w-0 flex-1" role="status" aria-live="polite">
        <p className="text-sm font-semibold text-ink">{callLabel(phase, muted)}</p>
        {message ? <p className="truncate text-xs text-ink-2">{message}</p> : null}
      </div>
      {phase === "speaking" ? (
        <button
          type="button"
          onClick={onInterrupt}
          className="rounded-full border border-line-strong bg-raised px-3 py-1 text-xs font-semibold text-ink hover:border-accent hover:text-accent"
        >
          Tap to interrupt
        </button>
      ) : null}
      <button
        type="button"
        onClick={onMute}
        aria-label={muted ? "Unmute" : "Mute"}
        className={cx(
          "grid size-9 place-items-center rounded-full border border-line-strong bg-raised text-ink-2 hover:text-ink",
          muted && "border-ink bg-ink text-paper hover:text-paper",
        )}
      >
        {muted ? <MicOff className="size-4" aria-hidden="true" /> : <Mic className="size-4" aria-hidden="true" />}
      </button>
      <button
        type="button"
        onClick={onEnd}
        className="inline-flex h-9 items-center gap-1.5 rounded-full bg-bad px-3 text-sm font-semibold text-white hover:bg-bad/90"
      >
        <PhoneOff className="size-4" aria-hidden="true" />
        End call
      </button>
    </div>
  );
}
