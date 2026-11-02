// Web voice call: copies the microphone's first channel to the page in ~20 ms chunks.
// Loaded by lib/voice/recorder.ts with audioWorklet.addModule (same origin, script-src 'self').
class VoiceCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    // About 21 ms at 48 kHz; the page computes the real duration from the sample rate.
    this.size = 1024;
    this.buffer = new Float32Array(this.size);
    this.filled = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) {
      let offset = 0;
      while (offset < channel.length) {
        const n = Math.min(channel.length - offset, this.size - this.filled);
        this.buffer.set(channel.subarray(offset, offset + n), this.filled);
        this.filled += n;
        offset += n;
        if (this.filled === this.size) {
          this.port.postMessage(this.buffer);
          this.buffer = new Float32Array(this.size);
          this.filled = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor("voice-capture", VoiceCapture);
