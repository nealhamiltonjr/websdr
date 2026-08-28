/** PacketListViz — scrollable packet list for AX.25 decoder (slice-49).
 *
 *  Subscribes to the receiver's decoderStream and renders a table of
 *  decoded AX.25 packets. Any PACKET_DECODERS family member (currently
 *  just ax25) feeds this list.
 *
 *  Each row shows:
 *    - Timestamp (UTC)
 *    - Source → Destination (with digipeater path)
 *    - Frame type (I/S/U + control byte)
 *    - Info payload (ASCII text)
 *  CRC errors appear as red rows with the error reason.
 *
 *  Architecture mirrors DigiMessageListViz:
 *    - onMount: subscribe to the session's decoderStream
 *    - onCleanup: unsubscribe
 *    - Pure model in packetListModel.ts (testable without SolidJS)
 */

import { createSignal, onCleanup, onMount, Show, For } from 'solid-js';
import { PACKET_DECODERS } from '@openwebrx-plus/shared-types';
import { receiverRegistry } from '../sessions/ReceiverSession';
import { registerViz, type VizProps } from './registry';
import {
  applyDecoderEvent,
  formatAge,
  formatDigipeaters,
  formatFrameType,
  formatTime,
  initialPacketListState,
  type PacketListState,
} from './packetListModel';

const packetDecoderNames: readonly string[] = PACKET_DECODERS;

function PacketListViz(props: VizProps): import('solid-js').JSX.Element {
  const [state, setState] = createSignal<PacketListState>(initialPacketListState());
  const [now, setNow] = createSignal(Date.now() / 1000);
  const [error, setError] = createSignal<string | null>(null);

  // Tick the "age" column every second.
  const tick = setInterval(() => setNow(Date.now() / 1000), 1000);
  onCleanup(() => clearInterval(tick));

  onMount(() => {
    const session = receiverRegistry.get(props.receiverId);
    if (!session) {
      setError(`receiver not found: ${props.receiverId}`);
      return;
    }
    const unsub = session.decoderStream.subscribe((envelope) => {
      if (!packetDecoderNames.includes(envelope.decoder)) return;
      setState((prev) => applyDecoderEvent(prev, envelope));
    });
    onCleanup(unsub);
  });

  // Show packets in reverse order (newest first).
  const reversedPackets = () => [...state().packets].reverse();

  return (
    <div class="flex h-full flex-col bg-zinc-900 text-zinc-100">
      {/* Header */}
      <div class="flex items-center justify-between border-b border-zinc-700 px-3 py-2">
        <div class="flex items-center gap-2">
          <span class="text-sm font-semibold uppercase tracking-wide text-cyan-400">
            Packets
          </span>
          <Show when={state().decoder_name}>
            <span class="rounded bg-zinc-700 px-1.5 py-0.5 text-xs font-mono text-zinc-300">
              {state().decoder_name}
            </span>
          </Show>
        </div>
        <div class="flex items-center gap-3 text-xs text-zinc-400">
          <span class="text-green-400">{state().total_decoded} ok</span>
          <span class="text-red-400">{state().total_crc_errors} err</span>
          <span>{formatTime(state().last_update)}</span>
          <span>{formatAge(state().last_update, now())} ago</span>
        </div>
      </div>

      {/* Error banner */}
      <Show when={error()}>
        <div class="bg-red-900/50 px-3 py-1 text-xs text-red-300">
          {error()}
        </div>
      </Show>

      {/* Packet table */}
      <div class="flex-1 overflow-auto">
        <Show
          when={state().packets.length > 0}
          fallback={
            <div class="flex h-full items-center justify-center text-sm text-zinc-500">
              <div class="text-center">
                <p class="mb-1">Waiting for packets…</p>
                <p class="text-xs text-zinc-600">
                  Attach an AX.25 decoder and tune to a packet frequency.
                </p>
              </div>
            </div>
          }
        >
          <table class="w-full text-left text-xs">
            <thead class="sticky top-0 bg-zinc-800 text-zinc-400">
              <tr>
                <th class="px-2 py-1 font-medium">Time</th>
                <th class="px-2 py-1 font-medium">From → To</th>
                <th class="px-2 py-1 font-medium">Type</th>
                <th class="px-2 py-1 font-medium">Info</th>
              </tr>
            </thead>
            <tbody>
              <For each={reversedPackets()}>
                {(pkt) => (
                  <tr
                    class="border-b border-zinc-800 hover:bg-zinc-800/50"
                    classList={{ 'bg-red-900/30': pkt.is_crc_error }}
                  >
                    <td class="px-2 py-1 font-mono text-zinc-400 whitespace-nowrap">
                      {formatTime(pkt.ts)}
                    </td>
                    <td class="px-2 py-1 font-mono whitespace-nowrap">
                      <span class="text-green-400">{pkt.source}</span>
                      <span class="text-zinc-500"> → </span>
                      <span class="text-cyan-400">{pkt.destination}</span>
                      <Show when={pkt.digipeaters.length > 0}>
                        <span class="text-zinc-500 text-[10px]">
                          {formatDigipeaters(pkt.digipeaters)}
                        </span>
                      </Show>
                    </td>
                    <td class="px-2 py-1 font-mono text-zinc-300 whitespace-nowrap">
                      {formatFrameType(pkt.frame_type, pkt.control)}
                    </td>
                    <td class="px-2 py-1 font-mono text-zinc-200 max-w-xs truncate">
                      {pkt.info_text || (pkt.is_crc_error ? pkt.error_reason : '')}
                    </td>
                  </tr>
                )}
              </For>
            </tbody>
          </table>
        </Show>
      </div>
    </div>
  );
}

registerViz({
  type: 'packet-list',
  displayName: 'Packet List',
  icon: 'list',
  defaultWidth: 600,
  defaultHeight: 280,
  live: true,
  component: PacketListViz,
});

export default PacketListViz;
