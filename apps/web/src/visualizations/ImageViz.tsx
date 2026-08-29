/** ImageViz — image display for the SSTV decoder (slice-46).
 *
 *  Subscribes to the receiver's decoderStream and renders decoded SSTV
 *  images on a canvas. Any IMAGE_DECODERS family member (currently just
 *  sstv) feeds this display.
 *
 *  The viz shows:
 *    - The decoder name + SSTV mode (e.g., "SCOTTIE_1")
 *    - Image dimensions + scanline progress bar during decoding
 *    - The decoded image rendered via canvas putImageData
 *    - Image count + last-update timestamp
 *
 *  Architecture mirrors TextStreamViz:
 *    - onMount: subscribe to the session's decoderStream
 *    - onCleanup: unsubscribe
 *    - Pure model in imageVizModel.ts (testable without SolidJS)
 */

import { createSignal, onCleanup, onMount, Show, createEffect } from 'solid-js';
import { IMAGE_DECODERS } from '@openwebrx-plus/shared-types';
import { receiverRegistry } from '../sessions/ReceiverSession';
import { registerViz, type VizProps } from './registry';
import {
  applyDecoderEvent,
  decodeBase64ToBytes,
  formatAge,
  formatTime,
  initialImageVizState,
  progressPercent,
  type ImageVizState,
} from './imageVizModel';

const imageDecoderNames: readonly string[] = IMAGE_DECODERS;

function ImageViz(props: VizProps): import('solid-js').JSX.Element {
  const [state, setState] = createSignal<ImageVizState>(initialImageVizState());
  const [now, setNow] = createSignal(Date.now() / 1000);
  const [error, setError] = createSignal<string | null>(null);
  let canvasRef: HTMLCanvasElement | undefined;

  // Tick the "age" display every second.
  const tick = setInterval(() => setNow(Date.now() / 1000), 1000);
  onCleanup(() => clearInterval(tick));

  // Re-render the canvas whenever currentImage changes.
  createEffect(() => {
    const img = state().currentImage;
    if (!img || !canvasRef) return;
    const canvas = canvasRef;
    canvas.width = img.width;
    canvas.height = img.height;
    const ctx = canvas.getContext('2d');
    if (!ctx) {
      setError('cannot get 2D canvas context');
      return;
    }
    // Decode the base64 RGB data into a Uint8Array.
    const bytes = decodeBase64ToBytes(img.data);
    // Create an ImageData and copy the RGB bytes into it (RGBA — set alpha=255).
    const imageData = ctx.createImageData(img.width, img.height);
    const data = imageData.data;
    for (let i = 0; i < bytes.length && i * 4 < data.length; i += 3) {
      const pixelIdx = (i / 3) * 4;
      data[pixelIdx] = bytes[i];     // R
      data[pixelIdx + 1] = bytes[i + 1]; // G
      data[pixelIdx + 2] = bytes[i + 2]; // B
      data[pixelIdx + 3] = 255;      // A
    }
    ctx.putImageData(imageData, 0, 0);
  });

  onMount(() => {
    const session = receiverRegistry.get(props.receiverId);
    if (!session) {
      setError(`receiver not found: ${props.receiverId}`);
      return;
    }
    const unsub = session.decoderStream.subscribe((envelope) => {
      if (!imageDecoderNames.includes(envelope.decoder)) return;
      setState((prev) => applyDecoderEvent(prev, envelope));
    });
    onCleanup(unsub);
  });

  return (
    <div class="flex h-full flex-col bg-zinc-900 text-zinc-100">
      {/* Header */}
      <div class="flex items-center justify-between border-b border-zinc-700 px-3 py-2">
        <div class="flex items-center gap-2">
          <span class="text-sm font-semibold uppercase tracking-wide text-cyan-400">
            SSTV Image
          </span>
          <Show when={state().mode}>
            <span class="rounded bg-zinc-700 px-1.5 py-0.5 text-xs font-mono text-zinc-300">
              {state().mode}
            </span>
          </Show>
        </div>
        <div class="flex items-center gap-3 text-xs text-zinc-400">
          <Show when={state().currentImage}>
            <span>{state().currentImage!.width}×{state().currentImage!.height}</span>
          </Show>
          <span>{state().imageCount} imgs</span>
          <span>{formatTime(state().lastUpdate)}</span>
          <span>{formatAge(state().lastUpdate, now())} ago</span>
        </div>
      </div>

      {/* Error banner */}
      <Show when={error()}>
        <div class="bg-red-900/50 px-3 py-1 text-xs text-red-300">
          {error()}
        </div>
      </Show>

      {/* Progress bar (during decoding) */}
      <Show when={state().scanlineProgress > 0 && progressPercent(state()) < 100}>
        <div class="border-b border-zinc-700 px-3 py-1">
          <div class="flex items-center justify-between text-xs text-zinc-400">
            <span>Decoding… {state().scanlineProgress} scanlines</span>
            <span>{progressPercent(state())}%</span>
          </div>
          <div class="mt-1 h-1 w-full overflow-hidden rounded bg-zinc-700">
            <div
              class="h-full bg-cyan-500 transition-all"
              style={{ width: `${progressPercent(state())}%` }}
            />
          </div>
        </div>
      </Show>

      {/* Image display */}
      <div class="flex flex-1 items-center justify-center overflow-auto p-4">
        <Show
          when={state().currentImage}
          fallback={
            <div class="text-center text-sm text-zinc-500">
              <p class="mb-1">Waiting for SSTV image…</p>
              <p class="text-xs text-zinc-600">
                Attach an SSTV decoder and tune to an SSTV frequency to see images here.
              </p>
            </div>
          }
        >
          <canvas
            ref={canvasRef}
            class="max-h-full max-w-full rounded border border-zinc-700"
            style={{ 'image-rendering': 'pixelated' }}
          />
        </Show>
      </div>
    </div>
  );
}

registerViz({
  type: 'image',
  displayName: 'SSTV Image',
  icon: 'image',
  defaultWidth: 480,
  defaultHeight: 360,
  live: true,
  component: ImageViz,
});

export default ImageViz;
