/** TextStreamViz — scrolling text display for CW / RTTY / PSK31 / Olivia
 *  decoders (slice-44).
 *
 *  Subscribes to the receiver's decoderStream and renders a monospace
 *  scrolling text panel. Any TEXT_DECODERS family member (cw, rtty,
 *  psk31, olivia) feeds this display.
 *
 *  The viz shows:
 *    - The decoder name (e.g., "CW", "RTTY", "PSK31", "Olivia")
 *    - Character count + last-update timestamp
 *    - The accumulated decoded text in a scrollable <pre> block
 *
 *  Architecture mirrors DigiMessageListViz:
 *    - onMount: subscribe to the session's decoderStream
 *    - onCleanup: unsubscribe
 *    - Pure model in textStreamModel.ts (testable without SolidJS)
 */

import { createSignal, onCleanup, onMount, Show } from 'solid-js';
import { TEXT_DECODERS } from '@openwebrx-plus/shared-types';
import { receiverRegistry } from '../sessions/ReceiverSession';
import { registerViz, type VizProps } from './registry';
import {
  applyDecoderEvent,
  formatAge,
  formatTime,
  initialTextStreamState,
  truncateText,
  type TextStreamState,
} from './textStreamModel';

const textDecoderNames: readonly string[] = TEXT_DECODERS;

const MAX_DISPLAY_CHARS = 5000;

function TextStreamViz(props: VizProps): import('solid-js').JSX.Element {
  const [state, setState] = createSignal<TextStreamState>(initialTextStreamState());
  const [now, setNow] = createSignal(Date.now() / 1000);
  const [error, setError] = createSignal<string | null>(null);

  // Tick the "age" display every second.
  const tick = setInterval(() => setNow(Date.now() / 1000), 1000);
  onCleanup(() => clearInterval(tick));

  onMount(() => {
    const session = receiverRegistry.get(props.receiverId);
    if (!session) {
      setError(`receiver not found: ${props.receiverId}`);
      return;
    }
    const unsub = session.decoderStream.subscribe((envelope) => {
      if (!textDecoderNames.includes(envelope.decoder)) return;
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
            Text Stream
          </span>
          <Show when={state().decoderName}>
            <span class="rounded bg-zinc-700 px-1.5 py-0.5 text-xs font-mono text-zinc-300">
              {state().decoderName}
            </span>
          </Show>
        </div>
        <div class="flex items-center gap-3 text-xs text-zinc-400">
          <span>{state().charCount} chars</span>
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

      {/* Text display */}
      <div class="flex-1 overflow-auto p-3">
        <Show
          when={state().text.length > 0}
          fallback={
            <div class="flex h-full items-center justify-center text-sm text-zinc-500">
              <div class="text-center">
                <p class="mb-1">Waiting for decoded text…</p>
                <p class="text-xs text-zinc-600">
                  Attach a CW / RTTY / PSK31 / Olivia decoder to see text here.
                </p>
              </div>
            </div>
          }
        >
          <pre class="whitespace-pre-wrap break-all font-mono text-sm leading-relaxed text-green-400">
            {truncateText(state().text, MAX_DISPLAY_CHARS)}
          </pre>
        </Show>
      </div>
    </div>
  );
}

registerViz({
  type: 'textstream',
  displayName: 'Text Stream',
  icon: 'terminal',
  defaultWidth: 480,
  defaultHeight: 240,
  live: true,
  component: TextStreamViz,
});

export default TextStreamViz;
