import { describe, expect, it } from "vitest";

import { initialVad, UtteranceSegmenter, VAD_DEFAULTS, vadStep, vadThreshold, type VadEvent, type VadState } from "@/lib/voice/vad";
import { base64ToBytes, bytesToBase64, encodeWav, floatTo16Bit, resample, rms, toWavBase64 } from "@/lib/voice/wav";

describe("WAV encoding", () => {
  it("writes a 44-byte RIFF header for 16 kHz mono 16-bit PCM", () => {
    const wav = encodeWav(new Int16Array([0, 1, -1, 32767]), 16000);
    const view = new DataView(wav.buffer);
    const text = (at: number) => String.fromCharCode(...wav.subarray(at, at + 4));
    expect(wav.length).toBe(44 + 8);
    expect(text(0)).toBe("RIFF");
    expect(view.getUint32(4, true)).toBe(36 + 8);
    expect(text(8)).toBe("WAVE");
    expect(text(12)).toBe("fmt ");
    expect(view.getUint16(20, true)).toBe(1); // PCM
    expect(view.getUint16(22, true)).toBe(1); // mono
    expect(view.getUint32(24, true)).toBe(16000);
    expect(view.getUint32(28, true)).toBe(32000);
    expect(view.getUint16(32, true)).toBe(2);
    expect(view.getUint16(34, true)).toBe(16);
    expect(text(36)).toBe("data");
    expect(view.getUint32(40, true)).toBe(8);
    expect([0, 1, 2, 3].map((i) => view.getInt16(44 + i * 2, true))).toEqual([0, 1, -1, 32767]);
  });

  it("quantises floats to 16-bit with clipping", () => {
    expect([...floatTo16Bit(new Float32Array([0, 1, -1, 2, -2, 0.5]))]).toEqual([0, 32767, -32768, 32767, -32768, 16384]);
  });

  it("round-trips base64, including large buffers", () => {
    const bytes = new Uint8Array(100_000).map((_, i) => (i * 31) % 256);
    expect(base64ToBytes(bytesToBase64(bytes))).toEqual(bytes);
    expect(bytesToBase64(new Uint8Array([77, 97, 110]))).toBe("TWFu");
  });
});

describe("resampling", () => {
  it("downsamples 48 kHz to 16 kHz by averaging each span", () => {
    const input = new Float32Array([0.3, 0.3, 0.3, -0.6, -0.6, -0.6, 0.9, 0, 0]);
    expect([...resample(input, 48000, 16000)].map((v) => +v.toFixed(5))).toEqual([0.3, -0.6, 0.3]);
  });

  it("handles a non-integer ratio (44.1 kHz) with the right length and level", () => {
    const second = new Float32Array(44100).fill(0.25);
    const out = resample(second, 44100, 16000);
    expect(out.length).toBe(16000);
    expect(Math.max(...out.map((v) => Math.abs(v - 0.25)))).toBeLessThan(1e-6);
  });

  it("keeps a low tone and copies at the same rate", () => {
    const tone = new Float32Array(4800).map((_, i) => Math.sin((2 * Math.PI * 200 * i) / 48000));
    const out = resample(tone, 48000, 16000);
    expect(out.length).toBe(1600);
    expect(rms(out)).toBeGreaterThan(0.69); // ~0.707 for a sine
    const same = resample(tone, 16000, 16000);
    expect(same).not.toBe(tone);
    expect(same).toEqual(tone);
  });

  it("upsamples by linear interpolation", () => {
    expect([...resample(new Float32Array([0, 1]), 8000, 16000)]).toEqual([0, 0.5, 1, 1]);
  });

  it("produces a 16 kHz WAV from microphone-rate samples", () => {
    const wav = base64ToBytes(toWavBase64(new Float32Array(48000), 48000));
    expect(wav.length).toBe(44 + 16000 * 2);
    expect(new DataView(wav.buffer).getUint32(24, true)).toBe(16000);
  });
});

describe("voice activity detection", () => {
  const FRAME = 20;
  const LOUD = 0.2;
  const QUIET = 0.001;

  function run(levels: number[], state: VadState = initialVad) {
    const events: { at: number; event: VadEvent }[] = [];
    levels.forEach((level, i) => {
      const step = vadStep(state, level, FRAME);
      state = step.state;
      if (step.event) events.push({ at: (i + 1) * FRAME, event: step.event });
    });
    return { state, events };
  }
  const frames = (ms: number, level: number) => Array.from({ length: ms / FRAME }, () => level);

  it("starts speech only after 150 ms above the threshold", () => {
    expect(run(frames(140, LOUD)).events).toEqual([]);
    const { events, state } = run(frames(160, LOUD));
    expect(events).toEqual([{ at: 160, event: { type: "start" } }]);
    expect(state.phase).toBe("speech");
  });

  it("ignores a click that dips before the onset completes", () => {
    const { events, state } = run([...frames(100, LOUD), QUIET, ...frames(100, LOUD)]);
    expect(events).toEqual([]);
    expect(state.phase).toBe("onset");
  });

  it("ends the utterance after 600 ms of silence", () => {
    const { events, state } = run([...frames(1000, LOUD), ...frames(600, QUIET)]);
    expect(events.map((e) => e.event.type)).toEqual(["start", "end"]);
    expect(events[1]).toEqual({ at: 1600, event: { type: "end", voicedMs: 1000, silenceMs: 600, capped: false } });
    expect(state.phase).toBe("silence");
  });

  it("keeps going through pauses shorter than 600 ms", () => {
    const { events } = run([...frames(600, LOUD), ...frames(500, QUIET), ...frames(600, LOUD)]);
    expect(events.map((e) => e.event.type)).toEqual(["start"]);
  });

  it("discards an utterance with under 400 ms of speech", () => {
    const { events } = run([...frames(300, LOUD), ...frames(800, QUIET)]);
    expect(events.map((e) => e.event)).toEqual([{ type: "start" }, { type: "discard", voicedMs: 300 }]);
  });

  it("cuts an utterance at 15 seconds", () => {
    const { events } = run(frames(16_000, LOUD));
    const end = events.find((e) => e.event.type === "end")!;
    expect(end.at).toBe(15_000);
    expect(end.event).toMatchObject({ capped: true });
  });

  it("learns a steady background noise and stops triggering on it", () => {
    // A fan at 0.02 RMS is above the starting threshold, so the first stretch reads as speech;
    // the estimate rises meanwhile, and after that turn the noise no longer starts one.
    const { state, events } = run(frames(30_000, 0.02));
    expect(events.length).toBeGreaterThan(0);
    expect(events.every((e) => e.at < 20_000)).toBe(true);
    expect(state.phase).toBe("silence");
    expect(vadThreshold(state)).toBeGreaterThan(0.05);
    // Speech over the fan still starts a turn.
    expect(run(frames(200, 0.2), state).events).toEqual([{ at: 160, event: { type: "start" } }]);
  });

  it("barely moves the noise estimate during real speech, and caps it", () => {
    const { state } = run(frames(3000, LOUD).slice(0, -1));
    expect(state.phase).toBe("speech");
    expect(vadThreshold(state)).toBeLessThan(LOUD * VAD_DEFAULTS.hold);
    expect(vadThreshold({ ...initialVad, noise: VAD_DEFAULTS.maxNoise })).toBeCloseTo(VAD_DEFAULTS.maxNoise * VAD_DEFAULTS.noiseFactor);
  });
});

describe("utterance segmenter", () => {
  const RATE = 16000;
  const chunk = (level: number, ms = 20) => new Float32Array((RATE * ms) / 1000).fill(level);

  it("returns one utterance with pre-roll and a trimmed tail", () => {
    const seg = new UtteranceSegmenter(RATE);
    const events = [];
    for (let i = 0; i < 50; i++) events.push(seg.push(chunk(0.001))); // 1 s of silence
    for (let i = 0; i < 50; i++) events.push(seg.push(chunk(0.2))); // 1 s of speech
    for (let i = 0; i < 40; i++) events.push(seg.push(chunk(0.001))); // 800 ms of silence
    const found = events.filter(Boolean);
    expect(found.map((e) => e!.type)).toEqual(["start", "utterance"]);
    const utterance = found[1] as Extract<typeof found[number], { type: "utterance" }>;
    // 300 ms pre-roll + 1000 ms speech + 250 ms tail.
    expect(utterance.durationMs).toBeCloseTo(1550, -1);
    expect(utterance.sampleRate).toBe(RATE);
    expect(utterance.capped).toBe(false);
    expect(seg.phase).toBe("silence");
  });

  it("discards a short burst and forgets it", () => {
    const seg = new UtteranceSegmenter(RATE);
    const types: string[] = [];
    for (let i = 0; i < 15; i++) types.push(seg.push(chunk(0.2))?.type ?? "");
    for (let i = 0; i < 40; i++) types.push(seg.push(chunk(0.001))?.type ?? "");
    expect(types.filter(Boolean)).toEqual(["start", "discard"]);
  });

  it("drops a partial utterance on reset", () => {
    const seg = new UtteranceSegmenter(RATE);
    for (let i = 0; i < 30; i++) seg.push(chunk(0.2));
    expect(seg.phase).toBe("speech");
    seg.reset();
    expect(seg.phase).toBe("silence");
    const after = [];
    for (let i = 0; i < 40; i++) after.push(seg.push(chunk(0.001)));
    expect(after.filter(Boolean)).toEqual([]);
  });
});
