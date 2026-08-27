/** SMeterViz — signal strength gauge (analog-style S-meter, also numeric). */

import { createSignal, onCleanup, onMount } from 'solid-js';
import { receiverRegistry } from '../sessions/ReceiverSession';
import { registerViz, type VizProps } from './registry';

export interface SMeterConfig {
  /** Show analog needle style in addition to numeric. */
  analog?: boolean;
}

const DEFAULTS: Required<SMeterConfig> = {
  analog: true,
};

function SMeterViz(props: VizProps): import('solid-js').JSX.Element {
  const [signal, setSignal] = createSignal<number>(0); // S-units, 0–9 + dB above
  const [rms, setRms] = createSignal<number>(-100); // dBFS

  onMount(() => {
    const session = receiverRegistry.getOrCreate(props.receiverId);
    // The SMeter computes from FFT bins (rough proxy: median of bin powers).
    const unsub = session.fftStream.subscribe((frame) => {
      const bins = frame.bins;
      const total = bins.reduce((s: number, v: number) => s + v, 0);
      const medianDb = total / bins.length;
      setRms(medianDb);
      // Convert dBFS → S-unit (rough: -73 dBFS = S9, 6 dB per S-unit below 9).
      const s9Db = -73;
      const sUnits = (medianDb - s9Db) / 6 + 9;
      setSignal(Math.max(0, Math.min(12, sUnits)));
    });
    onCleanup(() => unsub());
  });

  const cfg = { ...DEFAULTS, ...((props.config ?? {}) as SMeterConfig) };

  return (
    <div class="flex h-full w-full flex-col items-center justify-center bg-base-900 p-2">
      <div class="font-mono text-sm text-base-300">S-METER</div>
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
