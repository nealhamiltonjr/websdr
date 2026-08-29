/** DabServiceListViz — DAB service list display (slice-56).
 *
 *  Renders a list of discovered DAB services with their labels and
 *  program types. Subscribes to the DAB decoder's 'service' events.
 */

import { createSignal, onCleanup, onMount, Show, For } from 'solid-js';
import { receiverRegistry } from '../sessions/ReceiverSession';
import { registerViz, type VizProps } from './registry';
import {
  applyDecoderEvent,
  formatAge,
  formatTime,
  initialDabServiceListState,
  type DabServiceListState,
} from './dabServiceListModel';

function DabServiceListViz(props: VizProps): import('solid-js').JSX.Element {
  const [state, setState] = createSignal<DabServiceListState>(initialDabServiceListState());
  const [now, setNow] = createSignal(Date.now() / 1000);
  const [error, setError] = createSignal<string | null>(null);

  const tick = setInterval(() => setNow(Date.now() / 1000), 1000);
  onCleanup(() => clearInterval(tick));

  onMount(() => {
    const session = receiverRegistry.get(props.receiverId);
    if (!session) {
      setError(`receiver not found: ${props.receiverId}`);
      return;
    }
    const unsub = session.decoderStream.subscribe((envelope) => {
      if (envelope.decoder !== 'dab') return;
      setState((prev) => applyDecoderEvent(prev, envelope));
    });
    onCleanup(unsub);
  });

  return (
    <div class="flex h-full flex-col bg-zinc-900 text-zinc-100">
      <div class="flex items-center justify-between border-b border-zinc-700 px-3 py-2">
        <span class="text-sm font-semibold uppercase tracking-wide text-cyan-400">
          DAB Services
        </span>
        <div class="flex items-center gap-3 text-xs text-zinc-400">
          <span>{state().services.length} stations</span>
          <span>{formatTime(state().last_update)}</span>
          <span>{formatAge(state().last_update, now())} ago</span>
        </div>
      </div>
      <Show when={error()}>
        <div class="bg-red-900/50 px-3 py-1 text-xs text-red-300">{error()}</div>
      </Show>
      <div class="flex-1 overflow-auto">
        <Show
          when={state().services.length > 0}
          fallback={
            <div class="flex h-full items-center justify-center text-sm text-zinc-500">
              <div class="text-center">
                <p class="mb-1">Waiting for DAB services…</p>
                <p class="text-xs text-zinc-600">Attach a DAB decoder to see station labels here.</p>
              </div>
            </div>
          }
        >
          <table class="w-full text-left text-xs">
            <thead class="sticky top-0 bg-zinc-800 text-zinc-400">
              <tr>
                <th class="px-2 py-1 font-medium">Label</th>
                <th class="px-2 py-1 font-medium">SId</th>
                <th class="px-2 py-1 font-medium">PTy</th>
              </tr>
            </thead>
            <tbody>
              <For each={state().services}>
                {(svc) => (
                  <tr class="border-b border-zinc-800 hover:bg-zinc-800/50">
                    <td class="px-2 py-1 font-mono text-green-400">{svc.label || '???'}</td>
                    <td class="px-2 py-1 font-mono text-zinc-400">0x{svc.service_id.toString(16).toUpperCase().padStart(8, '0')}</td>
                    <td class="px-2 py-1 font-mono text-zinc-300">{svc.program_type}</td>
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
  type: 'dab-service-list',
  displayName: 'DAB Services',
  icon: 'radio',
  defaultWidth: 400,
  defaultHeight: 280,
  live: true,
  component: DabServiceListViz,
});

export default DabServiceListViz;
