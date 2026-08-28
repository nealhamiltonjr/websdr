/**
 * RNNoise AudioWorkletProcessor — Stage 2c of the AI cascade (ADR-002).
 * Closes the slice-19 loader by actually wiring it into the audio path.
 *
 * PLAIN JS (not TS) because this file is served verbatim by Vite from
 * public/ and loaded directly into the AudioWorkletGlobalScope, which
 * doesn't support TS annotations. The TS-typed reference impl lived at
 * apps/web/src/lib/audio/rnnoise-processor.ts during development
 * (kept in sync manually); see also `apps/web/src/lib/audio/AudioPlayer.ts`
 * for the main-thread integration.
 *
 * PROCESSING MODEL:
 *   RNNoise operates on 480-sample frames at 48 kHz (the canonical
 *   RNNoise/DeepFilterNet frame size). The Web Audio graph calls
 *   `process()` every render quantum (128 samples). So the processor:
 *     1. Accumulates incoming 128-sample quanta into a 480-sample buffer.
 *     2. When the buffer is full, calls the WASM Denoiser's
 *        `process_frame()` to denoise the frame.
 *     3. Pushes the denoised 480 samples into an output ring buffer.
 *     4. Each `process()` call drains 128 samples from the output ring.
 *
 *   Initial latency: ~10 ms (one 480-sample frame at 48 kHz) before
 *   the first denoised sample appears. Acceptable for live audio.
 *
 * WASM LOADING:
 *   The processor dynamically imports `/pkg/rnnoise_wasm.js` (the
 *   emcc-style glue loader that ships separately — see
 *   `packages/rnnoise-wasm/README.md`). The import is async; until it
 *   resolves, the processor passes audio through unchanged. If the
 *   import fails (WASM not deployed), the processor permanently
 *   falls back to pass-through.
 *
 *   This mirrors the slice-19 loader's "graceful failure" contract:
 *   the audio path is never broken by a missing optional native
 *   acceleration.
 *
 *   NOTE: AudioWorkletGlobalScope supports dynamic `import()` in
 *   Chromium 105+ and Firefox 113+. Safari 16.x doesn't — operators
 *   on Safari fall back to the main-thread AudioBufferSourceNode
 *   path (the AudioPlayer's `enableClientDenoise()` returns false
 *   before even attempting worklet registration, see the user-agent
 *   feature detection in `AudioPlayer.ts`).
 */

const RNNOISE_FRAME_SIZE = 480;
const RNNOISE_PATH = '/pkg/rnnoise_wasm.js';

class RNNoiseProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super(options);
    const frameSize =
      (options &&
        options.processorOptions &&
        typeof options.processorOptions.frameSize === 'number' &&
        options.processorOptions.frameSize) ||
      RNNOISE_FRAME_SIZE;
    this._frameSize = frameSize;
    this._inputBuffer = new Float32Array(frameSize);
    // Output ring is 4x the frame size so we can absorb bursty process()
    // quanta without dropping. 4 × 480 = 1920 samples ≈ 40 ms headroom.
    this._outputBuffer = new Float32Array(frameSize * 4);
    this._inputPos = 0;
    this._outputWritePos = 0;
    this._outputReadPos = 0;
    this._outputCount = 0;
    this._denoiser = null;
    this._loadAttempted = false;
    this._disposed = false;
    // Listen for control messages from the main thread.
    this.port.onmessage = (e) => {
      const msg = e.data || {};
      if (msg.type === 'reset') {
        this._inputPos = 0;
        this._outputWritePos = 0;
        this._outputReadPos = 0;
        this._outputCount = 0;
      } else if (msg.type === 'dispose') {
        this._disposed = true;
        this._denoiser = null;
      }
    };
    // Kick off the async WASM load. Until it resolves, the processor
    // passes audio through unchanged.
    this._loadDenoiser();
  }

  async _loadDenoiser() {
    if (this._loadAttempted) return;
    this._loadAttempted = true;
    try {
      const mod = await import(RNNOISE_PATH);
      if (typeof mod.Denoiser !== 'function') {
        return;
      }
      const instance = new mod.Denoiser(sampleRate);
      this._denoiser = {
        process_frame: (input) => instance.process_frame(input),
      };
      this.port.postMessage({ type: 'ready', frameSize: this._frameSize });
    } catch (err) {
      // Module not deployed or import failed in this context. Fall
      // back to permanent pass-through; main thread will see no
      // 'ready' message and can decide whether to revert to the
      // direct AudioBufferSourceNode path.
      this.port.postMessage({ type: 'load-failed', error: String(err) });
    }
  }

  process(inputs, outputs) {
    if (this._disposed) return false;

    const input = inputs[0] && inputs[0][0];
    const output = outputs[0] && outputs[0][0];
    if (!output) return true;

    // If we have input, push it into the input buffer. (When the source
    // is silent — input undefined or all zeros — we still emit zeros to
    // keep the output graph alive.)
    if (input && input.length > 0) {
      for (let i = 0; i < input.length; i++) {
        this._inputBuffer[this._inputPos++] = input[i];
        if (this._inputPos === this._inputBuffer.length) {
          // Frame complete — denoise and push to output ring.
          const frame =
            this._denoiser !== null
              ? this._denoiser.process_frame(this._inputBuffer)
              : this._inputBuffer;
          for (let j = 0; j < frame.length; j++) {
            this._outputBuffer[this._outputWritePos] = frame[j];
            this._outputWritePos = (this._outputWritePos + 1) % this._outputBuffer.length;
            if (this._outputCount < this._outputBuffer.length) {
              this._outputCount++;
            } else {
              // Overwrite oldest — effectively drop the oldest sample.
              this._outputReadPos = (this._outputReadPos + 1) % this._outputBuffer.length;
            }
          }
          this._inputPos = 0;
        }
      }
    }

    // Drain output ring by 128 samples (or input.length if smaller).
    const n = Math.min(output.length, this._outputCount);
    for (let i = 0; i < n; i++) {
      output[i] = this._outputBuffer[this._outputReadPos];
      this._outputReadPos = (this._outputReadPos + 1) % this._outputBuffer.length;
      this._outputCount--;
    }
    // Zero-fill any remainder (when the ring is empty / warming up).
    for (let i = n; i < output.length; i++) {
      output[i] = 0;
    }

    return true;
  }
}

registerProcessor('rnnoise-processor', RNNoiseProcessor);
