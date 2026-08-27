/** FrequencyCounterViz — numeric frequency display, plus mode badge.
 *  This is the canonical "attachment" viz: small, cheap, dockable alongside
 *  a waterfall to give at-a-glance tuning info.
 */

import { createSignal, onCleanup, onMount, Show } from 'solid-js';
import { receiverRegistry, type ReceiverMetadata } from '../sessions/ReceiverSession';
import { registerViz, type VizProps } from './registry';

export interface FrequencyCounterConfig {
  /** Show mode badge next to the frequency. */
  showMode?: boolean;
  /** Show source label. */
  showSource?: boolean;
}

const DEFAULTS: Required<FrequencyCounterConfig> = {
  showMode: true,
  showSource: true,
};

function formatHz(hz: number): string {
  if (hz >= 1_000_000_000) return `${(hz / 1e9).toFixed(6)} GHz`;
  if (hz >= 1_000_000) return `${(hz / 1e6).toFixed(4)} MHz`;
  if (hz >= 1_000) return `${(hz / 1e3).toFixed(3)} kHz`;
  return `${hz.toFixed(0)} Hz`;
}

function FrequencyCounterViz(props: VizProps): import('solid-js').JSX.Element {
  const [meta, setMeta] = createSignal<ReceiverMetadata | null>(null);

  onMount(() => {
    const session = receiverRegistry.getOrCreate(props.receiverId);
    setMeta(session.getCurrentMetadata());
    const unsub = session.metadataStream.subscribe((m) => setMeta(m));
    onCleanup(() => unsub());
  });

  const cfg = { ...DEFAULTS, ...((props.config ?? {}) as FrequencyCounterConfig) };

  return (
    <div class="flex h-full w-full flex-col items-center justify-center bg-base-900 p-2">
      <div class="font-mono text-xs text-base-400">FREQUENCY</div>
      <div class="font-mono text-2xl font-bold text-cyan-450">
        {meta() ? formatHz(meta()!.frequency) : '---.---- MHz'}
      </div>
      <Show when={cfg.showMode && meta()}>
        <div class="mt-1 rounded bg-base-800 px-2 py-0.5 font-mono text-xs text-base-200">
          {meta()!.mode}
        </div>
      </Show>
      <Show when={cfg.showSource && meta()}>
        <div class="mt-1 font-mono text-[10px] text-base-400">{meta()!.source.label}</div>
      </Show>
    </div>
  );
}

registerViz({
  type: 'frequency-counter',
  displayName: 'Freq Counter',
  icon: 'hash',
  defaultWidth: 200,
  defaultHeight: 100,
  live: false,
  component: FrequencyCounterViz,
});

export default FrequencyCounterViz;
