/** WaterfallViz — time × freq × intensity scrolling waterfall.
 *  WebGL2-powered via WaterfallRenderer. Subscribes to fftStream.
 *
 *  Slice-4.6: crosshair sync (ADR-001 feature 11) — hover moves a shared
 *  crosshair across every linked viz of the same receiver; click tunes.
 */

import { onCleanup, onMount } from 'solid-js';
import { receiverRegistry } from '../sessions/ReceiverSession';
import { WaterfallRenderer } from '../lib/webgl2/WaterfallRenderer';
import { attachCrosshair } from './crosshair';
import { registerViz, type VizProps } from './registry';

export interface WaterfallConfig {
  minDb?: number;
  maxDb?: number;
  colorMap?: 'viridis' | 'turbo' | 'grayscale' | 'jet';
  historyRows?: number; // height of the scrolling history texture
}

const DEFAULTS: Required<WaterfallConfig> = {
  minDb: -100,
  maxDb: -20,
  colorMap: 'turbo',
  historyRows: 1024,
};

let nextVizId = 1;

function WaterfallViz(props: VizProps): import('solid-js').JSX.Element {
  let canvasRef: HTMLCanvasElement | undefined;
  let chipRef: HTMLDivElement | undefined;
  let renderer: WaterfallRenderer | undefined;

  onMount(() => {
    if (!canvasRef) return;
    const cfg = { ...DEFAULTS, ...((props.config ?? {}) as WaterfallConfig) };
    renderer = new WaterfallRenderer(canvasRef, cfg);
    const session = receiverRegistry.getOrCreate(props.receiverId);
    const unsub = session.fftStream.subscribe((frame) => {
      renderer?.pushFrame(frame);
    });
    const detachCrosshair = chipRef
      ? attachCrosshair({
          canvas: canvasRef,
          chip: chipRef,
          renderer,
          session,
          vizId: `waterfall-${nextVizId++}`,
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
  type: 'waterfall',
  displayName: 'Waterfall',
  icon: 'waves',
  defaultWidth: 800,
  defaultHeight: 400,
  live: true,
  component: WaterfallViz,
});

export default WaterfallViz;
