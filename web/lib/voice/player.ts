/**
 * Plays the assistant's spoken replies in the web voice call through one reused <audio>
 * element. unlock() is called from the tap that starts the call, so mobile browsers allow the
 * later, programmatic play() calls. Replies are played from a blob: URL (CSP media-src).
 */

import { base64ToBytes, bytesToBase64, encodeWav } from "./wav";

export type PlayOutcome = "ended" | "stopped" | "failed" | "skipped";

export interface Player {
  /** Call inside a user gesture: plays a moment of silence to allow later playback. */
  unlock(): void;
  /** Resolves when the clip ends, is stopped, fails, or immediately when there is no audio. */
  play(audio: string | null | undefined, mime?: string): Promise<PlayOutcome>;
  /** Stop the current clip (tap to interrupt). */
  stop(): void;
  dispose(): void;
}

let silence: string | null = null;
function silentWav(): string {
  silence ??= `data:audio/wav;base64,${bytesToBase64(encodeWav(new Int16Array(160)))}`;
  return silence;
}

export function createPlayer(): Player {
  const audio = typeof Audio === "function" ? new Audio() : null;
  let url: string | null = null;
  let finish: ((outcome: PlayOutcome) => void) | null = null;

  const settle = (outcome: PlayOutcome) => {
    const done = finish;
    finish = null;
    if (audio) {
      audio.onended = null;
      audio.onerror = null;
    }
    if (url) {
      URL.revokeObjectURL(url);
      url = null;
    }
    done?.(outcome);
  };

  return {
    unlock() {
      if (!audio) return;
      try {
        audio.src = silentWav();
        void audio.play()?.catch(() => undefined);
      } catch {
        // Playback may still work later; nothing to do.
      }
    },
    play(data, mime = "audio/mpeg") {
      if (!audio || !data) return Promise.resolve("skipped");
      settle("stopped");
      let bytes: Uint8Array;
      try {
        bytes = base64ToBytes(data);
      } catch {
        return Promise.resolve("failed");
      }
      return new Promise<PlayOutcome>((resolve) => {
        finish = resolve;
        url = URL.createObjectURL(new Blob([bytes as BlobPart], { type: mime }));
        audio.onended = () => settle("ended");
        audio.onerror = () => settle("failed");
        audio.src = url;
        try {
          void audio.play()?.catch(() => settle("failed"));
        } catch {
          settle("failed");
        }
      });
    },
    stop() {
      if (!audio) return;
      audio.pause();
      settle("stopped");
    },
    dispose() {
      this.stop();
      if (audio) audio.removeAttribute("src");
    },
  };
}
