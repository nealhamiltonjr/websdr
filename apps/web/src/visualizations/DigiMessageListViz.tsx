/** DigiMessageListViz — the FT8 / audio-band digital-mode messages list
 *  (slice-21 — ADR-003 family #6).
 *
 *  Subscribes to the receiver's decoderStream (fed by the SharedWorker
 *  from WS decoder events) and renders a scrollable list of decoded
 *  digital-mode messages (FT8 / FT4 / WSPR / JT65 / JT9 / PSK31 / RTTY).
 *  Any DIGI_MESSAGE_DECODERS family member feeds this list.
 *
 *  Slice-21 stub: the FT8 plugin's feed_iq returns no events yet (the
 *  demodulator lands in a future slice). This component renders an
 *  empty-state banner when no messages have arrived; once the decoder
 *  produces events, the list populates automatically.
 */

import { createSignal, onCleanup, onMount, Show, For } from 'solid-js';
import { DIGI_MESSAGE_DECODERS } from '@openwebrx-plus/shared-types';
import { receiverRegistry } from '../sessions/ReceiverSession';
import { registerViz, type VizProps } from './registry';
import {
  applyDecoderEvent,
  formatAge,
  formatTime,
  initialDigiMessageState,
  type DigiMessageFeedState,
} from './digiMessageModel';

const digiDecoderNames: readonly string[] = DIGI_MESSAGE_DECODERS;

function DigiMessageListViz(props: VizProps): import('solid-js').JSX.Element {
  const [state, setState] = createSignal<DigiMessageFeedState>(initialDigiMessageState());
  const [now, setNow] = createSignal(Date.now() / 1000);
  const [error, setError] = createSignal<string | null>(null);

  // Tick the "age" column every second.
  const tick = setInterval(() => setNow(Date.now() / 1000), 1000);
  onCleanup(() => clearInterval(tick));

  onMount(() => {
    const session = receiverRegistry.get(props.receiverId);
    if (!session) {
      setError('receiver not found');
      return;
    }
    const unsub = session.decoderStream.subscribe((envelope) => {
      if (!digiDecoderNames.includes(envelope.decoder)) return;
      setState((prev) => applyDecoderEvent(prev, envelope));
    });
    onCleanup(unsub);
  });

  return (
    <div class="flex h-full flex-col bg-base-900 text-base-100">
      <header class="flex h-7 items-center justify-between border-b border-base-800 px-2">
        <span class="font-mono text-[11px] text-base-300">
          Digital Messages
          <Show when={state().mode}>
            <span class="ml-1 text-cyan-450">· {state().mode}</span>
          </Show>
        </span>
        <span class="font-mono text-[10px] text-base-400">
          {state().messageCount} decoded
        </span>
      </header>

      <Show
        when={state().messages.length > 0}
        fallback={
          <div class="flex flex-1 items-center justify-center p-4 text-center">
            <div>
              <p class="font-mono text-[11px] text-base-300">
                No messages decoded yet.
              </p>
              <p class="mt-2 font-mono text-[10px] text-base-400">
                The FT8 decoder is in stub state (slice-21) — the demodulator
                lands in a future slice. The visualization contract is
                shipped; once events arrive, this list populates.
              </p>
              <Show when={error()}>
                <p class="mt-2 font-mono text-[10px] text-rose-400">{error()}</p>
              </Show>
            </div>
          </div>
        }
      >
        <div class="flex-1 overflow-y-auto">
          <table class="w-full border-collapse font-mono text-[11px]">
            <thead class="sticky top-0 bg-base-850">
              <tr class="border-b border-base-800 text-base-400">
                <th class="px-1.5 py-0.5 text-left">Time</th>
                <th class="px-1.5 py-0.5 text-left">Mode</th>
                <th class="px-1.5 py-0.5 text-left">SNR</th>
                <th class="px-1.5 py-0.5 text-left">Offset</th>
                <th class="px-1.5 py-0.5 text-left">Message</th>
                <th class="px-1.5 py-0.5 text-right">Age</th>
              </tr>
            </thead>
            <tbody>
              <For each={state().messages}>
                {(msg) => (
                  <tr class="border-b border-base-850 hover:bg-base-850/60">
                    <td class="px-1.5 py-0.5 text-base-300">{formatTime(msg.ts)}</td>
                    <td class="px-1.5 py-0.5 text-cyan-450">{msg.mode}</td>
                    <td class="px-1.5 py-0.5 text-right text-base-300">
                      {msg.snr_db !== undefined ? `${msg.snr_db}` : '—'}
                    </td>
                    <td class="px-1.5 py-0.5 text-right text-base-300">
                      {msg.audio_offset_hz !== undefined
                        ? `${Math.round(msg.audio_offset_hz)}`
                        : '—'}
                    </td>
                    <td class="px-1.5 py-0.5 text-base-100">{msg.text}</td>
                    <td class="px-1.5 py-0.5 text-right text-base-400">
                      {formatAge(now(), msg.ts)}
                    </td>
                  </tr>
                )}
              </For>
            </tbody>
          </table>
        </div>

        <footer class="flex h-7 items-center justify-between border-t border-base-800 px-2">
          <span class="font-mono text-[10px] text-base-400">
            latest: <span class="text-cyan-450">{state().lastMessage}</span>
          </span>
          <span class="font-mono text-[10px] text-base-400">
            {state().messages.length} / 50 buffer
          </span>
        </footer>
      </Show>
    </div>
  );
}

registerViz({
  type: 'digi-message-list',
  displayName: 'Digital Messages',
  icon: 'message',
  defaultWidth: 520,
  defaultHeight: 280,
  live: true,
  component: DigiMessageListViz,
});

export default DigiMessageListViz;
