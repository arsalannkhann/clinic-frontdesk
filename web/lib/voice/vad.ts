/**
 * Energy-based voice activity detection for the web voice call, as a pure state machine fed
 * one frame's RMS at a time, plus a segmenter that turns microphone chunks into utterances.
 *
 * - Speech starts once the level stays above the start threshold for `startMs` (150 ms).
 * - The utterance ends after `endSilenceMs` (800 ms) below the (lower) continue threshold.
 * - An utterance is cut at `maxMs` (15 s) and sent as it is.
 * - Anything with under `minMs` (400 ms) of speech is discarded as a cough or a click.
 * - The threshold adapts to the room: the larger of `minThreshold` and `noiseFactor` times a
 *   running estimate of the background level. The estimate follows the level within about a
 *   second while silent, falls quickly whenever the level drops below it, and rises only very
 *   slowly during "speech", so a steady noise (a fan) mistaken for speech is learned and stops
 *   triggering after one turn, while real speech barely moves it.
 */

import { concatSamples, rms } from "./wav";

export interface VadConfig {
  startMs: number;
  endSilenceMs: number;
  maxMs: number;
  minMs: number;
  /** Audio kept from just before speech was detected, so the first syllable is not clipped. */
  preRollMs: number;
  /** Trailing silence kept after the last voiced frame. */
  tailMs: number;
  minThreshold: number;
  noiseFactor: number;
  /** Continue threshold as a fraction of the start threshold (hysteresis). */
  hold: number;
  /** Noise floor estimate never rises above this, so a loud room can still be talked over. */
  maxNoise: number;
  /** Time constant of the noise estimate while silent. */
  noiseTauMs: number;
  /** Time constant of its rise during onset or speech. */
  noiseRiseActiveTauMs: number;
  /** Time constant of its fall when the level is below it. */
  noiseFallTauMs: number;
}

export const VAD_DEFAULTS: VadConfig = {
  startMs: 150,
  endSilenceMs: 800,
  maxMs: 15_000,
  minMs: 400,
  preRollMs: 300,
  tailMs: 250,
  minThreshold: 0.015,
  noiseFactor: 3,
  hold: 0.7,
  maxNoise: 0.03,
  noiseTauMs: 1000,
  noiseRiseActiveTauMs: 20_000,
  noiseFallTauMs: 200,
};

export interface VadState {
  phase: "silence" | "onset" | "speech";
  /** Time above threshold while in onset. */
  onsetMs: number;
  /** Time since the utterance began (onset included). */
  elapsedMs: number;
  /** Time from the utterance's start to the end of its last voiced frame. */
  voicedMs: number;
  /** Consecutive time below the continue threshold. */
  silenceMs: number;
  /** Running estimate of the background level. */
  noise: number;
}

export type VadEvent =
  | { type: "start" }
  | { type: "end"; voicedMs: number; silenceMs: number; capped: boolean }
  | { type: "discard"; voicedMs: number };

export const initialVad: VadState = { phase: "silence", onsetMs: 0, elapsedMs: 0, voicedMs: 0, silenceMs: 0, noise: 0.005 };

export function vadThreshold(state: VadState, config: VadConfig = VAD_DEFAULTS): number {
  return Math.max(config.minThreshold, state.noise * config.noiseFactor);
}

function trackNoise(noise: number, level: number, frameMs: number, active: boolean, config: VadConfig): number {
  const tau = level < noise ? config.noiseFallTauMs : active ? config.noiseRiseActiveTauMs : config.noiseTauMs;
  const alpha = 1 - Math.exp(-frameMs / tau);
  return Math.min(config.maxNoise, noise + (level - noise) * alpha);
}

/** Advance the detector by one frame of `frameMs` with level `level` (RMS of [-1, 1] samples). */
export function vadStep(
  state: VadState,
  level: number,
  frameMs: number,
  config: VadConfig = VAD_DEFAULTS,
): { state: VadState; event: VadEvent | null } {
  const threshold = vadThreshold(state, config);
  const noise = trackNoise(state.noise, level, frameMs, state.phase !== "silence" || level > threshold, config);
  const reset = { ...initialVad, noise };
  switch (state.phase) {
    case "silence": {
      if (level > threshold) return { state: { ...reset, phase: "onset", onsetMs: frameMs }, event: null };
      return { state: reset, event: null };
    }
    case "onset": {
      // A dip back below the threshold before startMs was a click, not speech.
      if (level <= threshold) return { state: reset, event: null };
      const onsetMs = state.onsetMs + frameMs;
      if (onsetMs < config.startMs) return { state: { ...state, onsetMs, noise }, event: null };
      return {
        state: { ...reset, phase: "speech", onsetMs, elapsedMs: onsetMs, voicedMs: onsetMs },
        event: { type: "start" },
      };
    }
    case "speech": {
      const elapsedMs = state.elapsedMs + frameMs;
      const voiced = level > threshold * config.hold;
      const voicedMs = voiced ? elapsedMs : state.voicedMs;
      const silenceMs = voiced ? 0 : state.silenceMs + frameMs;
      if (elapsedMs >= config.maxMs) {
        return { state: reset, event: { type: "end", voicedMs, silenceMs, capped: true } };
      }
      if (silenceMs >= config.endSilenceMs) {
        return voicedMs < config.minMs
          ? { state: reset, event: { type: "discard", voicedMs } }
          : { state: reset, event: { type: "end", voicedMs, silenceMs, capped: false } };
      }
      return { state: { ...state, elapsedMs, voicedMs, silenceMs, noise }, event: null };
    }
  }
}

export type SegmentEvent =
  | { type: "start" }
  | { type: "utterance"; samples: Float32Array; sampleRate: number; durationMs: number; capped: boolean }
  | { type: "discard" };

/**
 * Feeds microphone chunks (any size, one sample rate) through the detector and buffers audio:
 * a pre-roll ring while silent, the whole utterance while speaking. Returns at most one event
 * per chunk.
 */
export class UtteranceSegmenter {
  private vad: VadState = initialVad;
  private chunks: Float32Array[] = [];
  private bufferedMs = 0;

  constructor(
    readonly sampleRate: number,
    private readonly config: VadConfig = VAD_DEFAULTS,
  ) {}

  get phase(): VadState["phase"] {
    return this.vad.phase;
  }

  /** Forget any partial utterance (pause, mute), keeping the noise estimate. */
  reset(): void {
    this.vad = { ...initialVad, noise: this.vad.noise };
    this.chunks = [];
    this.bufferedMs = 0;
  }

  push(chunk: Float32Array): SegmentEvent | null {
    if (chunk.length === 0) return null;
    const frameMs = (chunk.length / this.sampleRate) * 1000;
    const { state, event } = vadStep(this.vad, rms(chunk), frameMs, this.config);
    this.vad = state;
    this.chunks.push(chunk);
    this.bufferedMs += frameMs;

    if (event?.type === "start") return { type: "start" };
    if (event?.type === "discard") {
      this.chunks = [];
      this.bufferedMs = 0;
      return { type: "discard" };
    }
    if (event?.type === "end") {
      const samples = this.trimmed(event.silenceMs);
      this.chunks = [];
      this.bufferedMs = 0;
      return {
        type: "utterance",
        samples,
        sampleRate: this.sampleRate,
        durationMs: (samples.length / this.sampleRate) * 1000,
        capped: event.capped,
      };
    }
    // Silent (or a failed onset): keep only the pre-roll.
    if (this.vad.phase === "silence") this.trimToPreRoll();
    return null;
  }

  private trimToPreRoll(): void {
    while (this.chunks.length > 1 && this.bufferedMs - this.msOf(this.chunks[0]!) >= this.config.preRollMs) {
      this.bufferedMs -= this.msOf(this.chunks.shift()!);
    }
  }

  /** The buffered utterance, with trailing silence cut to `tailMs`. */
  private trimmed(silenceMs: number): Float32Array {
    const all = concatSamples(this.chunks);
    const dropMs = Math.max(0, silenceMs - this.config.tailMs);
    const drop = Math.min(all.length, Math.floor((dropMs / 1000) * this.sampleRate));
    return drop > 0 ? all.slice(0, all.length - drop) : all;
  }

  private msOf(chunk: Float32Array): number {
    return (chunk.length / this.sampleRate) * 1000;
  }
}
