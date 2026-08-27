/** WaterfallViz — time × freq × intensity scrolling waterfall.
 *  WebGL2-powered via WaterfallRenderer. Subscribes to fftStream.
 *
 *  Slice-4.6: crosshair sync (ADR-001 feature 11) — hover moves a shared
 *  crosshair across every linked viz of the same receiver; click tunes.
 *
 *  Slice-11: OffscreenCanvas + Web Worker thread-off. When the browser
 *  supports `canvas.transferControlToOffscreen()`, the FFT feed + render
 *  loop move to a dedicated Worker (render-worker.ts) so the main thread
 *  only pays the cost of one postMessage per frame. Falls back to the
 *  main-thread WaterfallRenderer when OffscreenCanvas is missing (Safari
 *  iOS, jsdom) — the API is identical to keep the fallback transparent.
 */

import { onCleanup, onMount, Show, createSignal } from 'solid-js';
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
  // Worker state — when present, the worker hosts the renderer and we
  // only forward FFT frames + lifecycle commands via postMessage.
  let worker: Worker | undefined;
  // Track whether the worker accepted the OffscreenCanvas transfer.
  // Browsers without OffscreenCanvas support fall back to main-thread.
  const [usingWorker, setUsingWorker] = createSignal(false);

  onMount(() => {
    if (!canvasRef) return;
    const cfg = { ...DEFAULTS, ...((props.config ?? {}) as WaterfallConfig) };

    // Slice-11: prefer the worker when the canvas can be transferred
    // (OffscreenCanvas support + Worker module construction succeed).
    const canTransfer = typeof canvasRef.transferControlToOffscreen === 'function';
    if (canTransfer) {
      try {
        const offscreen = canvasRef.transferControlToOffscreen();
        worker = new Worker(
          new URL('../workers/render-worker.ts', import.meta.url),
          { type: 'module', name: 'openwebrx-render' },
        );
        worker.postMessage(
          { kind: 'init', canvas: offscreen, config: cfg },
          [offscreen],
        );
      } catch (e) {
        // Worker construction can fail in environments without ESM worker
        // support; fall back to main-thread WaterfallRenderer.
        console.warn('[WaterfallViz] render worker init failed, falling back to main thread', e);
        worker = undefined;
      }
    }

    if (worker) {
      setUsingWorker(true);
      // Worker path: forward FFT frames as transferable Float32Arrays.
      const session = receiverRegistry.getOrCreate(props.receiverId);
      const unsub = session.fftStream.subscribe((frame) => {
        // Copy the bins into a transferable buffer — the source Float32Array
        // is owned by the session and may be reused after we return.
        const bins = frame.bins.slice(0);
        worker?.postMessage(
          {
            kind: 'fft',
            bins,
            centerFreq: frame.centerFreq,
            sampleRate: frame.sampleRate,
          },
          [bins.buffer],
        );
      });
      onCleanup(() => {
        unsub();
        worker?.postMessage({ kind: 'dispose' });
        worker?.terminate();
        worker = undefined;
      });
      // Crosshair sync doesn't go through the worker (it's a tiny DOM
      // overlay). Attach it on the canvas — the chip moves locally.
      const detachCrosshair = chipRef
        ? attachCrosshair({
            canvas: canvasRef,
            chip: chipRef,
            // The renderer lives in the worker; pass a partial stub that
            // satisfies the crosshair's `getAxis()` lookup pattern.
            // Real axis lookup happens via the session's metadata stream.
            renderer: { getAxis: () => null } as unknown as WaterfallRenderer,
            session,
            vizId: `waterfall-${nextVizId++}`,
          })
        : null;
      onCleanup(() => detachCrosshair?.());
    } else {
      // Main-thread path (original slice-1 behavior).
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
    }
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
      <Show when={usingWorker()}>
        <div class="pointer-events-none absolute right-1 top-1 rounded bg-base-950/80 px-1 py-0.5 font-mono text-[9px] text-cyan-350">
          worker
        </div>
      </Show>
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
