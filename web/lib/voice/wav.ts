/**
 * PCM helpers for the web voice call: resample the microphone's float samples to 16 kHz,
 * quantise to 16-bit, wrap them in a WAV header and base64 encode the file for the JSON body.
 * Pure functions, so they are unit-tested without an AudioContext.
 */

export const TARGET_SAMPLE_RATE = 16_000;

/**
 * Resample mono float samples. Downsampling averages each output sample's span of input
 * samples (a box filter, which also tames aliasing for speech); upsampling interpolates
 * linearly. Works for non-integer ratios such as 44.1 kHz -> 16 kHz.
 */
export function resample(input: Float32Array, fromRate: number, toRate: number = TARGET_SAMPLE_RATE): Float32Array {
  if (!(fromRate > 0) || !(toRate > 0)) throw new RangeError("Sample rates must be positive");
  if (fromRate === toRate) return input.slice();
  const ratio = fromRate / toRate;
  const length = Math.floor(input.length / ratio);
  const output = new Float32Array(length);
  if (ratio > 1) {
    for (let i = 0; i < length; i++) {
      const start = Math.floor(i * ratio);
      const end = Math.min(input.length, Math.floor((i + 1) * ratio));
      let sum = 0;
      for (let j = start; j < end; j++) sum += input[j]!;
      output[i] = end > start ? sum / (end - start) : input[start] ?? 0;
    }
    return output;
  }
  for (let i = 0; i < length; i++) {
    const position = i * ratio;
    const left = Math.floor(position);
    const right = Math.min(input.length - 1, left + 1);
    const t = position - left;
    output[i] = input[left]! * (1 - t) + input[right]! * t;
  }
  return output;
}

/** Float samples in [-1, 1] (clipped) to signed 16-bit integers. */
export function floatTo16Bit(input: Float32Array): Int16Array {
  const output = new Int16Array(input.length);
  for (let i = 0; i < input.length; i++) {
    const s = Math.max(-1, Math.min(1, input[i]!));
    output[i] = s < 0 ? Math.round(s * 0x8000) : Math.round(s * 0x7fff);
  }
  return output;
}

/** A complete mono 16-bit PCM WAV file (RIFF, 44-byte header, little endian). */
export function encodeWav(samples: Int16Array, sampleRate: number = TARGET_SAMPLE_RATE): Uint8Array {
  const dataBytes = samples.length * 2;
  const buffer = new ArrayBuffer(44 + dataBytes);
  const view = new DataView(buffer);
  const text = (offset: number, value: string) => {
    for (let i = 0; i < value.length; i++) view.setUint8(offset + i, value.charCodeAt(i));
  };
  text(0, "RIFF");
  view.setUint32(4, 36 + dataBytes, true);
  text(8, "WAVE");
  text(12, "fmt ");
  view.setUint32(16, 16, true); // fmt chunk size
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); // byte rate
  view.setUint16(32, 2, true); // block align
  view.setUint16(34, 16, true); // bits per sample
  text(36, "data");
  view.setUint32(40, dataBytes, true);
  for (let i = 0; i < samples.length; i++) view.setInt16(44 + i * 2, samples[i]!, true);
  return new Uint8Array(buffer);
}

export function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

export function base64ToBytes(value: string): Uint8Array {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

/** Microphone samples at any rate -> base64 16 kHz mono 16-bit WAV, ready for the voice route. */
export function toWavBase64(samples: Float32Array, sampleRate: number): string {
  return bytesToBase64(encodeWav(floatTo16Bit(resample(samples, sampleRate, TARGET_SAMPLE_RATE)), TARGET_SAMPLE_RATE));
}

/** Concatenate captured chunks. */
export function concatSamples(chunks: readonly Float32Array[]): Float32Array {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const out = new Float32Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.length;
  }
  return out;
}

export function rms(samples: Float32Array): number {
  if (samples.length === 0) return 0;
  let sum = 0;
  for (let i = 0; i < samples.length; i++) sum += samples[i]! * samples[i]!;
  return Math.sqrt(sum / samples.length);
}
