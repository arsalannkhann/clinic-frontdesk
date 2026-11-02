/**
 * Microphone capture for the web voice call: getUserMedia with the browser's echo
 * cancellation, noise suppression and gain control, an AudioWorklet (ScriptProcessor where
 * worklets are missing) handing raw chunks to the energy VAD, and one callback per finished
 * utterance with base64 16 kHz WAV. Nothing is recorded while paused or muted, and stop()
 * releases the microphone and closes the AudioContext.
 *
 * The browser-only parts live here; the detection and encoding are pure (vad.ts, wav.ts).
 */

import { UtteranceSegmenter, VAD_DEFAULTS, type VadConfig } from "./vad";
import { toWavBase64 } from "./wav";

export type MicErrorKind = "denied" | "unsupported" | "no_device" | "failed";

export class MicError extends Error {
  constructor(readonly kind: MicErrorKind) {
    super(`Microphone ${kind}`);
    this.name = "MicError";
  }
}

export interface RecorderHandlers {
  /** Speech was detected (the utterance is still being spoken). */
  onSpeechStart?: () => void;
  /** A finished utterance as base64 16 kHz mono 16-bit WAV. */
  onUtterance: (audio: string, durationMs: number) => void;
  /** Something too short to send (a cough, a click). */
  onDiscard?: () => void;
}

export interface Recorder {
  /** Ask for the microphone and start listening; throws MicError. */
  start(): Promise<void>;
  /** Stop listening (assistant speaking, a turn in flight) without releasing the microphone. */
  pause(): void;
  resume(): void;
  setMuted(muted: boolean): void;
  /** Release the microphone and the AudioContext. Safe to call more than once. */
  stop(): void;
}

export const WORKLET_URL = "/voice-capture-worklet.js";

export function micSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    !!navigator.mediaDevices?.getUserMedia &&
    typeof (window.AudioContext ?? (window as unknown as { webkitAudioContext?: unknown }).webkitAudioContext) === "function"
  );
}

function micError(error: unknown): MicError {
  const name = (error as { name?: string } | null)?.name;
  if (name === "NotAllowedError" || name === "SecurityError" || name === "PermissionDeniedError") return new MicError("denied");
  if (name === "NotFoundError" || name === "DevicesNotFoundError" || name === "OverconstrainedError") return new MicError("no_device");
  return new MicError("failed");
}

export function createRecorder(handlers: RecorderHandlers, config: VadConfig = VAD_DEFAULTS): Recorder {
  let context: AudioContext | null = null;
  let stream: MediaStream | null = null;
  let source: MediaStreamAudioSourceNode | null = null;
  let node: AudioWorkletNode | ScriptProcessorNode | null = null;
  let sink: GainNode | null = null;
  let segmenter: UtteranceSegmenter | null = null;
  let paused = false;
  let muted = false;
  let stopped = false;

  const onChunk = (chunk: Float32Array) => {
    if (stopped || paused || muted || !segmenter) return;
    const event = segmenter.push(chunk);
    if (!event) return;
    if (event.type === "start") handlers.onSpeechStart?.();
    else if (event.type === "discard") handlers.onDiscard?.();
    else handlers.onUtterance(toWavBase64(event.samples, event.sampleRate), event.durationMs);
  };

  function release() {
    node?.disconnect();
    if (node && "port" in node) node.port.onmessage = null;
    if (node && "onaudioprocess" in node) node.onaudioprocess = null;
    source?.disconnect();
    sink?.disconnect();
    for (const track of stream?.getTracks() ?? []) track.stop();
    void context?.close().catch(() => undefined);
    node = null;
    source = null;
    sink = null;
    stream = null;
    context = null;
    segmenter = null;
  }

  return {
    async start() {
      if (!micSupported()) throw new MicError("unsupported");
      const Ctor = window.AudioContext ?? (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      // Created before the first await, inside the tap, so the browser lets it run.
      context = new Ctor();
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
        });
      } catch (error) {
        release();
        throw micError(error);
      }
      if (stopped || !context) {
        release();
        return;
      }
      try {
        if (context.state === "suspended") await context.resume().catch(() => undefined);
        segmenter = new UtteranceSegmenter(context.sampleRate, config);
        source = context.createMediaStreamSource(stream);
        // Processing nodes only run when connected to the destination; a muted gain keeps the
        // microphone out of the speakers.
        sink = context.createGain();
        sink.gain.value = 0;
        sink.connect(context.destination);
        if (context.audioWorklet && typeof AudioWorkletNode === "function") {
          try {
            await context.audioWorklet.addModule(WORKLET_URL);
            const worklet = new AudioWorkletNode(context, "voice-capture", { numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1 });
            worklet.port.onmessage = (event: MessageEvent<Float32Array>) => onChunk(event.data);
            node = worklet;
          } catch {
            node = null;
          }
        }
        if (!node) {
          const processor = context.createScriptProcessor(2048, 1, 1);
          processor.onaudioprocess = (event) => onChunk(new Float32Array(event.inputBuffer.getChannelData(0)));
          node = processor;
        }
        if (stopped) {
          release();
          return;
        }
        source.connect(node);
        node.connect(sink);
      } catch {
        release();
        throw new MicError("failed");
      }
    },
    pause() {
      paused = true;
      segmenter?.reset();
    },
    resume() {
      segmenter?.reset();
      paused = false;
    },
    setMuted(value: boolean) {
      muted = value;
      segmenter?.reset();
      for (const track of stream?.getAudioTracks() ?? []) track.enabled = !value;
    },
    stop() {
      stopped = true;
      release();
    },
  };
}
