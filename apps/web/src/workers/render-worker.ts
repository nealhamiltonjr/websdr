/** Render worker — OffscreenCanvas + Web Worker thread-off (slice-11).
 *
 *  Architecture (per ARCHITECTURE.md Pillar 2 — "render thread-off"):
 *    Main thread                    Worker thread
 *    ----------                     -------------
 *    WaterfallViz                   RenderHost
 *      ├ canvas.transferControlToOffscreen()
 *      └ worker.postMessage({init})
 *                                    ├ new WaterfallRenderer(offscreen)
 *                                    └ RenderHost.subscribe
 *      └ worker.postMessage({fft})
 *                                    ├ RenderHost.pushFrame
 *                                    └ RAF → render → present
 *      └ worker.postMessage({resize})
 *                                    └ renderer.resize()
 *      └ worker.postMessage({dispose})
 *                                    └ renderer.dispose() + close()
 *
 *  Falls back gracefully when OffscreenCanvas isn't supported (Safari iOS):
 *  WaterfallViz detects `canvas.transferControlToOffscreen` and uses the
 *  main-thread WaterfallRenderer when it's missing — the API is identical.
 *
 *  Message protocol (postMessage):
 *    - init:    { kind: 'init', canvas, config } — transfer canvas, build renderer
 *    - fft:     { kind: 'fft', bins: Float32Array, centerFreq, sampleRate } — push frame
 *    - resize:  { kind: 'resize', width, height } — update canvas size
 *    - dispose: { kind: 'dispose' } — cleanup
 *
 *  The FFT bins are transferred (zero-copy) so the worker doesn't need to
 *  copy megabytes per second of FFT data.
 */

/// <reference lib="webworker" />

import { WaterfallRenderer } from '../lib/webgl2/WaterfallRenderer';
import type { WaterfallRendererConfig } from '../lib/webgl2/WaterfallRenderer';
import type { FFTFrame } from '../sessions/ReceiverSession';

// ---------------------------------------------------------------------------
// Wire protocol — the message types both threads agree on.
// ---------------------------------------------------------------------------

export type RenderWorkerMessage =
  | { kind: 'init'; canvas: OffscreenCanvas; config: WaterfallRendererConfig }
  | { kind: 'fft'; bins: Float32Array; centerFreq: number; sampleRate: number }
  | { kind: 'resize'; width: number; height: number }
  | { kind: 'dispose' };

export type RenderWorkerCommand = RenderWorkerMessage & {
  /** postMessage transfer list convention: canvas on init, bins on fft. */
  __transfer?: Transferable[];
};

// ---------------------------------------------------------------------------
// RenderHost — worker-side state machine.
// ---------------------------------------------------------------------------

class RenderHost {
  private renderer: WaterfallRenderer | null = null;
  private pendingFrame: FFTFrame | null = null;
  private rafId: number | null = null;

  init(canvas: OffscreenCanvas, config: WaterfallRendererConfig): void {
    if (this.renderer) {
      this.renderer.dispose();
      this.renderer = null;
    }
    // The WaterfallRenderer accepts an HTMLCanvasElement | OffscreenCanvas;
    // both expose the WebGL2 rendering context the same way.
    this.renderer = new WaterfallRenderer(canvas as unknown as HTMLCanvasElement, config);
  }

  pushFrame(bins: Float32Array, centerFreq: number, sampleRate: number): void {
    if (!this.renderer) return;
    // Build a partial FFTFrame — the renderer only uses bins, centerFreq,
    // and sampleRate; the other fields (receiverId, timestamp, minDb,
    // maxDb) are stamped to sentinel values since the worker doesn't
    // need them for rendering.
    this.pendingFrame = {
      receiverId: 'worker',
      timestamp: 0,
      bins,
      centerFreq,
      sampleRate,
      minDb: 0,
      maxDb: 0,
    };
    if (this.rafId === null) {
      // Worker-global requestAnimationFrame is available when the OffscreenCanvas
      // is "visible" (it isn't in headless tests, so guard against that).
      const raf = (self as unknown as { requestAnimationFrame?: (cb: () => void) => number }).requestAnimationFrame;
      if (raf) {
        this.rafId = raf(() => this.render());
      } else {
        // Fall back to a setTimeout pump (tests / non-visible contexts).
        setTimeout(() => this.render(), 16);
      }
    }
  }

  resize(width: number, height: number): void {
    if (!this.renderer) return;
    // Force the renderer's backing store to the requested size.
    const canvas = this.renderer.canvas as unknown as OffscreenCanvas;
    if (canvas.width !== width) canvas.width = width;
    if (canvas.height !== height) canvas.height = height;
    // resize() also re-asserts the GL viewport to match the new size.
    this.renderer.resize();
  }

  dispose(): void {
    if (this.rafId !== null) {
      const caf = (self as unknown as { cancelAnimationFrame?: (id: number) => void }).cancelAnimationFrame;
      caf?.(this.rafId);
      this.rafId = null;
    }
    this.renderer?.dispose();
    this.renderer = null;
    this.pendingFrame = null;
  }

  private render(): void {
    this.rafId = null;
    const frame = this.pendingFrame;
    this.pendingFrame = null;
    if (frame && this.renderer) {
      this.renderer.pushFrame(frame);
    }
  }
}

const host = new RenderHost();

self.onmessage = (ev: MessageEvent<RenderWorkerMessage>) => {
  const msg = ev.data;
  if (!msg || typeof msg !== 'object') return;
  switch (msg.kind) {
    case 'init':
      host.init(msg.canvas, msg.config);
      break;
    case 'fft':
      host.pushFrame(msg.bins, msg.centerFreq, msg.sampleRate);
      break;
    case 'resize':
      host.resize(msg.width, msg.height);
      break;
    case 'dispose':
      host.dispose();
      self.close();
      break;
  }
};

// Type-narrow the global for the worker scope.
export type { RenderHost };
