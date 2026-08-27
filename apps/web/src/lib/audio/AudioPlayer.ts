/** AudioPlayer — plays back PCM audio frames received from the SharedWorker
 *  via the Web Audio API.
 *
 *  Strategy: maintain a schedule of upcoming AudioBufferSourceNodes, each
 *  holding one PCM frame. Schedule them back-to-back starting at the next
 *  available time. If we fall behind (listener's clock > scheduled end time),
 *  drop frames to catch up.
 *
 *  This is the slice-1.5 simple version. Slice-2 will swap to a proper
 *  AudioWorkletProcessor with a ring buffer for cleaner latency management.
 */

import { createSignal, onCleanup } from 'solid-js';

export interface AudioPlayer {
  /** Whether audio is currently muted. UI binding. */
  readonly muted: () => boolean;
  /** Current master volume (0..1). UI binding. */
  readonly volume: () => number;
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
}

export function createAudioPlayer(): AudioPlayer {
  const [muted, setMuted] = createSignal(true);
  const [volume, setVolume] = createSignal(0.5);

  let ctx: AudioContext | null = null;
  let gainNode: GainNode | null = null;
  let nextStartTime = 0;

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
    // click handler, so we're safe.
    const Ctor =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    ctx = new Ctor();
    gainNode = ctx.createGain();
    gainNode.gain.value = volume();
    gainNode.connect(ctx.destination);
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
    if (ctx) {
      ctx.close().catch(() => {});
      ctx = null;
      gainNode = null;
      nextStartTime = 0;
    }
    setMuted(true);
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

  function enqueue(samples: Int16Array, sampleRate: number) {
    if (!ctx || !gainNode || muted()) return;
    // Convert Int16 PCM to Float32 in [-1, 1].
    const f32 = new Float32Array(samples.length);
    for (let i = 0; i < samples.length; i++) {
      f32[i] = samples[i] / 32768;
    }
    const buf = ctx.createBuffer(1 /* mono */, f32.length, sampleRate);
    buf.copyToChannel(f32, 0);

    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(gainNode);

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
    enable,
    disable,
    toggleMute,
    setVolume: setVol,
    enqueue,
  };
}
