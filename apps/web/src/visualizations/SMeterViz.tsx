/** SMeterViz — signal strength gauge (analog-style S-meter, also numeric).
 *
 * Slice-6.2 (linked readout): when the cursor hovers over a sibling FFT
 * canvas of the same receiver (cursor published on the session's
 * cursorStream), the S-Meter reads the bin value AT that frequency
 * instead of the median dBFS across the whole span. A small "cursor"
 * / "tuned" badge in the header tells the user which mode is active.
 *
 * The cursor is per-receiver (cursorStream is a session-level Subject),
 * so two receivers each with their own S-Meter stay independent — the
 * hover only affects the receiver whose canvas the cursor is over.
 */

import { createSignal, onCleanup, onMount, Show } from 'solid-js';
import { receiverRegistry } from '../sessions/ReceiverSession';
import { registerViz, type VizProps } from './registry';
import { binPowerAtHz, type FreqAxis } from './freqAxis';

export interface SMeterConfig {
  /** Show analog needle style in addition to numeric. */
  analog?: boolean;
  /** When true, the S-Meter follows the cursor when one is active. */
  followCursor?: boolean;
}

const DEFAULTS: Required<SMeterConfig> = {
  analog: true,
  followCursor: true,
};

/** S9 reference (dBFS) and dB-per-S-unit below S9 — IARU Region 1 convention. */
const S9_DBFS = -73;
const DB_PER_S_UNIT = 6;

/** dBFS → S-unit (S0..S9 + dB above S9, capped at S9+40 for display). */
function dbfsToSUnits(dbfs: number): number {
  const sUnits = (dbfs - S9_DBFS) / DB_PER_S_UNIT + 9;
  return Math.max(0, Math.min(15, sUnits));
}

function SMeterViz(props: VizProps): import('solid-js').JSX.Element {
  const [signal, setSignal] = createSignal<number>(0); // S-units, 0–9 + dB above
  const [rms, setRms] = createSignal<number>(-100); // dBFS
  const [source, setSource] = createSignal<'tuned' | 'cursor'>('tuned');

  onMount(() => {
    const session = receiverRegistry.getOrCreate(props.receiverId);
    let lastCursor: number | null = null;
    let lastFrame: { axis: FreqAxis; bins: Float32Array } | null = null;

    const recompute = () => {
      // Linked readout: if a cursor is active AND it falls inside the
      // last FFT frame's axis span, read the bin value at that frequency.
      if (lastCursor !== null && lastFrame !== null) {
        const v = binPowerAtHz(lastFrame.axis, lastFrame.bins, lastCursor);
        if (v !== null) {
          setRms(v);
          setSignal(dbfsToSUnits(v));
          setSource('cursor');
          return;
        }
      }
      // Fallback: median dBFS across the whole span (the original behavior).
      if (lastFrame !== null) {
        const bins = lastFrame.bins;
        let total = 0;
        for (let i = 0; i < bins.length; i++) total += bins[i];
        const medianDb = bins.length > 0 ? total / bins.length : -100;
        setRms(medianDb);
        setSignal(dbfsToSUnits(medianDb));
      }
      setSource('tuned');
    };

    const unsubFft = session.fftStream.subscribe((frame) => {
      const axis: FreqAxis = {
        centerHz: frame.centerFreq,
        sampleRateHz: frame.sampleRate,
      };
      lastFrame = { axis, bins: frame.bins };
      recompute();
    });

    const unsubCursor = session.cursorStream.subscribe((c) => {
      lastCursor = c ? c.hz : null;
      recompute();
    });

    onCleanup(() => {
      unsubFft();
      unsubCursor();
    });
  });

  const cfg = { ...DEFAULTS, ...((props.config ?? {}) as SMeterConfig) };

  return (
    <div class="flex h-full w-full flex-col items-center justify-center bg-base-900 p-2">
      <div class="flex items-center gap-2">
        <div class="font-mono text-sm text-base-300">S-METER</div>
        <Show when={cfg.followCursor}>
          <div
            class="rounded px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wider"
            classList={{
              'bg-cyan-500/20 text-cyan-300': source() === 'cursor',
              'bg-base-800 text-base-400': source() === 'tuned',
            }}
          >
            {source()}
          </div>
        </Show>
      </div>
      <div class="font-mono text-3xl font-bold text-amber-450">
        S{signal().toFixed(1)}
      </div>
      <div class="font-mono text-xs text-base-400">{rms().toFixed(1)} dBFS</div>
      {cfg.analog && (
        <div class="mt-2 h-2 w-full overflow-hidden rounded bg-base-800">
          <div
            class="h-full bg-gradient-to-r from-cyan-450 to-amber-450"
            style={{ width: `${(signal() / 12) * 100}%` }}
          />
        </div>
      )}
    </div>
  );
}

registerViz({
  type: 'smeter',
  displayName: 'S-Meter',
  icon: 'gauge',
  defaultWidth: 200,
  defaultHeight: 120,
  live: true,
  component: SMeterViz,
});

export default SMeterViz;
