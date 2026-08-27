/** SpectrumViz — linear plane (real-time scope).
 *  Freq on X, amplitude on Y, no time axis. Complements the waterfall.
 *
 *  Slice-4.6: crosshair sync (ADR-001 feature 11) — the crosshair is shared
 *  with the waterfall (and any other viz of the same receiver); the hover
 *  chip adds a dB readout at the cursor; click tunes.
 */

import { onCleanup, onMount } from 'solid-js';
import { receiverRegistry } from '../sessions/ReceiverSession';
import { SpectrumRenderer } from '../lib/webgl2/SpectrumRenderer';
import { attachCrosshair } from './crosshair';
import { registerViz, type VizProps } from './registry';

export interface SpectrumConfig {
  minDb?: number;
  maxDb?: number;
  colorMap?: 'viridis' | 'turbo' | 'grayscale' | 'jet';
  /** Show peak-hold trace in addition to instantaneous. */
  peakHold?: boolean;
  /** Decay rate of peak-hold trace (0-1, lower = faster decay). */
  peakDecay?: number;
}

const DEFAULTS: Required<SpectrumConfig> = {
  minDb: -100,
  maxDb: -20,
  colorMap: 'turbo',
  peakHold: true,
  peakDecay: 0.95,
};

let nextVizId = 1;

function SpectrumViz(props: VizProps): import('solid-js').JSX.Element {
  let canvasRef: HTMLCanvasElement | undefined;
  let chipRef: HTMLDivElement | undefined;
  let renderer: SpectrumRenderer | undefined;

  onMount(() => {
    if (!canvasRef) return;
    const cfg = { ...DEFAULTS, ...((props.config ?? {}) as SpectrumConfig) };
    renderer = new SpectrumRenderer(canvasRef, cfg);
    const session = receiverRegistry.getOrCreate(props.receiverId);
    const unsub = session.fftStream.subscribe((frame) => {
      renderer?.update(frame);
    });
    const detachCrosshair = chipRef
      ? attachCrosshair({
          canvas: canvasRef,
          chip: chipRef,
          renderer,
          session,
          vizId: `spectrum-${nextVizId++}`,
          formatLevel: (hz) => {
            const db = renderer?.levelAt(hz);
            return db != null ? `${db.toFixed(1)} dB` : null;
          },
        })
      : null;
    onCleanup(() => {
      unsub();
      detachCrosshair?.();
    });
  });

  onCleanup(() => {
    renderer?.dispose();
    renderer = undefined;
  });

  return (
    <div class="relative h-full w-full bg-base-950">
      <canvas ref={canvasRef} class="viz-canvas" />
      <div
        ref={chipRef}
        class="pointer-events-none absolute z-10 hidden rounded bg-base-950/90 px-1.5 py-0.5 font-mono text-[10px] leading-tight text-cyan-350 ring-1 ring-cyan-450/40"
        style={{ display: 'none' }}
      />
    </div>
  );
}

registerViz({
  type: 'spectrum',
  displayName: 'Spectrum',
  icon: 'activity',
  defaultWidth: 800,
  defaultHeight: 200,
  live: true,
  component: SpectrumViz,
});

export default SpectrumViz;
