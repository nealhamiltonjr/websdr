/** AudioPlayer — plays back PCM audio frames received from the SharedWorker
 *  via the Web Audio API.
 *
 *  Strategy: maintain a schedule of upcoming AudioBufferSourceNodes, each
 *  holding one PCM frame. Schedule them back-to-back starting at the next
 *  available time. If we fall behind (listener's clock > scheduled end time),
 *  drop frames to catch up.
 *
 *  Slice-24 (2026-08-28): added optional client-side RNNoise denoise via
 *  AudioWorklet. When `enableClientDenoise()` is called and the WASM is
 *  available (loader from slice-19), the AudioPlayer registers the worklet
 *  at `/worklets/rnnoise-processor.js` and inserts an AudioWorkletNode
 *  between the BufferSourceNodes and the GainNode. The worklet buffers
 *  128-sample render quanta into 480-sample frames for RNNoise, then
 *  drains its output ring back to 128-sample quanta. Initial latency
 *  is ~10 ms (one frame at 48 kHz). When the WASM is not deployed, the
 *  method returns false and audio continues through the direct path.
 */

import { createSignal, onCleanup } from 'solid-js';
import { loadRNNoiseModule } from '../denoise/RNNoiseLoader';

/** URL of the worklet processor. Vite serves public/ verbatim at the
 *  site root, so this path resolves in both dev and prod. */
const RNNOISE_WORKLET_URL = '/worklets/rnnoise-processor.js';

export interface AudioPlayer {
  /** Whether audio is currently muted. UI binding. */
  readonly muted: () => boolean;
  /** Current master volume (0..1). UI binding. */
  readonly volume: () => number;
  /** Whether client-side denoise (RNNoise WASM) is active. UI binding. */
  readonly clientDenoiseEnabled: () => boolean;
  /** Enable audio output. Must be called from a user gesture (browser autoplay policy). */
  enable: () => Promise<void>;
  /** Disable audio output. */
  disable: () => void;
  /** Toggle mute state. */
  toggleMute: () => void;
  /** Set master volume (0..1). */
  setVolume: (v: number) => void;
  /** Push a new PCM frame into the playback queue. No-op if disabled. */
  enqueue: (samples: Int16Array, sampleRate: number) => void;
  /** Enable client-side RNNoise denoise. Loads the WASM module (slice-19
   *  loader), registers the worklet, inserts an AudioWorkletNode between
   *  source and gain. Returns false if the WASM isn't deployed (caller
   *  can show a UI hint). Idempotent — calling twice is a no-op. */
  enableClientDenoise: () => Promise<boolean>;
  /** Disable client-side denoise. Disconnects the worklet node, returns
   *  to the direct BufferSourceNode → GainNode path. Idempotent. */
  disableClientDenoise: () => void;
}

export function createAudioPlayer(): AudioPlayer {
  const [muted, setMuted] = createSignal(true);
  const [volume, setVolume] = createSignal(0.5);
  const [clientDenoiseEnabled, setClientDenoiseEnabled] = createSignal(false);

  let ctx: AudioContext | null = null;
  let gainNode: GainNode | null = null;
  let denoiseNode: AudioWorkletNode | null = null;
  /** Where sources connect to. Either `gainNode` (direct path) or
   *  `denoiseNode` (worklet path, which itself connects to `gainNode`). */
  let sinkNode: AudioNode | null = null;
  let nextStartTime = 0;
  let workletRegistered = false;
  let workletRegistrationFailed = false;

  // Cleanup on scope disposal.
  onCleanup(() => {
    disable();
  });

  async function enable() {
    if (ctx) {
      // Already enabled — just unmute.
      setMuted(false);
      gainNode!.gain.value = volume();
      return;
    }
    // Browser autoplay policy: AudioContext must be created in response to
    // a user gesture. The TuningBar's audio toggle button calls this from a
    // click handler, so we're safe. `globalThis` resolves to `window` in
    // browsers (and to the node global in tests where we stubGlobal the
    // AudioContext constructor — avoids a `window is not defined` failure
    // in the node-environment vitest runs).
    const Ctor =
      globalThis.AudioContext ??
      (globalThis as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    ctx = new Ctor();
    gainNode = ctx.createGain();
    gainNode.gain.value = volume();
    gainNode.connect(ctx.destination);
    sinkNode = gainNode;
    nextStartTime = ctx.currentTime;
    setMuted(false);
    // If the context was created in a suspended state (browser policy),
    // resume it.
    if (ctx.state === 'suspended') {
      try {
        await ctx.resume();
      } catch (e) {
        console.warn('[AudioPlayer] could not resume AudioContext', e);
      }
    }
  }

  function disable() {
    if (denoiseNode) {
      try {
        denoiseNode.port.postMessage({ type: 'dispose' });
      } catch {
        // Best-effort — node may already be disconnected.
      }
      try {
        denoiseNode.disconnect();
      } catch {
        // Ignore.
      }
      denoiseNode = null;
      sinkNode = gainNode;
    }
    if (ctx) {
      ctx.close().catch(() => {});
      ctx = null;
      gainNode = null;
      sinkNode = null;
      nextStartTime = 0;
      workletRegistered = false;
      workletRegistrationFailed = false;
    }
    setMuted(true);
    setClientDenoiseEnabled(false);
  }

  function toggleMute() {
    if (muted()) {
      void enable();
    } else {
      setMuted(true);
      if (gainNode) gainNode.gain.value = 0;
    }
  }

  function setVol(v: number) {
    setVolume(v);
    if (gainNode && !muted()) gainNode.gain.value = v;
  }

  async function ensureWorkletRegistered(): Promise<boolean> {
    if (!ctx) return false;
    if (workletRegistered) return true;
    if (workletRegistrationFailed) return false;
    // Feature-detect AudioWorklet (Safari 16.x doesn't have it).
    if (typeof ctx.audioWorklet === 'undefined') {
      workletRegistrationFailed = true;
      return false;
    }
    try {
      await ctx.audioWorklet.addModule(RNNOISE_WORKLET_URL);
      workletRegistered = true;
      return true;
    } catch (e) {
      console.warn('[AudioPlayer] worklet registration failed', e);
      workletRegistrationFailed = true;
      return false;
    }
  }

  async function enableClientDenoise(): Promise<boolean> {
    if (clientDenoiseEnabled()) return true;
    if (!ctx || !gainNode) {
      // AudioContext not yet enabled — caller should call enable() first.
      return false;
    }
    // Slice-19 loader: probe for /pkg/rnnoise_wasm.js. If not deployed,
    // bail out gracefully (operator hasn't built the WASM yet).
    const mod = await loadRNNoiseModule();
    if (mod == null) {
      return false;
    }
    // Register the worklet (idempotent).
    const ok = await ensureWorkletRegistered();
    if (!ok) return false;
    // Create + insert the worklet node between source and gain.
    try {
      denoiseNode = new AudioWorkletNode(ctx, 'rnnoise-processor', {
        processorOptions: { frameSize: 480 },
      });
      denoiseNode.connect(gainNode);
      sinkNode = denoiseNode;
      setClientDenoiseEnabled(true);
      return true;
    } catch (e) {
      console.warn('[AudioPlayer] AudioWorkletNode creation failed', e);
      try {
        denoiseNode?.disconnect();
      } catch {
        // Ignore.
      }
      denoiseNode = null;
      return false;
    }
  }

  function disableClientDenoise() {
    if (!denoiseNode) return;
    try {
      denoiseNode.port.postMessage({ type: 'dispose' });
    } catch {
      // Best-effort.
    }
    try {
      denoiseNode.disconnect();
    } catch {
      // Ignore.
    }
    denoiseNode = null;
    sinkNode = gainNode;
    setClientDenoiseEnabled(false);
  }

  function enqueue(samples: Int16Array, sampleRate: number) {
    if (!ctx || !sinkNode || muted()) return;
    // Convert Int16 PCM to Float32 in [-1, 1].
    const f32 = new Float32Array(samples.length);
    for (let i = 0; i < samples.length; i++) {
      f32[i] = samples[i] / 32768;
    }
    const buf = ctx.createBuffer(1 /* mono */, f32.length, sampleRate);
    buf.copyToChannel(f32, 0);

    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(sinkNode);

    // Schedule back-to-back. If we've fallen behind the listener's clock,
    // reset to current time (drop pending frames).
    const now = ctx.currentTime;
    if (nextStartTime < now) {
      nextStartTime = now + 0.02; // 20 ms lead to avoid underruns
    }
    src.start(nextStartTime);
    nextStartTime += buf.duration;
  }

  return {
    muted,
    volume,
    clientDenoiseEnabled,
    enable,
    disable,
    toggleMute,
    setVolume: setVol,
    enqueue,
    enableClientDenoise,
    disableClientDenoise,
  };
}
