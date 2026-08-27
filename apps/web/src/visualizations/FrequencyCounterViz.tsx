/** FrequencyCounterViz — numeric frequency display, plus mode badge.
 *
 * This is the canonical "attachment" viz: small, cheap, dockable alongside
 * a waterfall to give at-a-glance tuning info.
 *
 * Slice-6.2 (linked readout): when the cursor hovers over a sibling FFT
 * canvas of the same receiver, the Frequency Counter shows the cursor
 * frequency instead of the tuned frequency. A small "cursor" / "tuned"
 * badge in the header tells the user which mode is active. The original
 * tuned frequency stays visible (in a smaller font under the main number)
 * so the user can compare the cursor position against the tuned spot.
 */

import { createSignal, onCleanup, onMount, Show } from 'solid-js';
import { receiverRegistry, type ReceiverMetadata } from '../sessions/ReceiverSession';
import { registerViz, type VizProps } from './registry';

export interface FrequencyCounterConfig {
  /** Show mode badge next to the frequency. */
  showMode?: boolean;
  /** Show source label. */
  showSource?: boolean;
  /** When true, the counter follows the cursor when one is active. */
  followCursor?: boolean;
}

const DEFAULTS: Required<FrequencyCounterConfig> = {
  showMode: true,
  showSource: true,
  followCursor: true,
};

function formatHz(hz: number): string {
  if (hz >= 1_000_000_000) return `${(hz / 1e9).toFixed(6)} GHz`;
  if (hz >= 1_000_000) return `${(hz / 1e6).toFixed(4)} MHz`;
  if (hz >= 1_000) return `${(hz / 1e3).toFixed(3)} kHz`;
  return `${hz.toFixed(0)} Hz`;
}

function FrequencyCounterViz(props: VizProps): import('solid-js').JSX.Element {
  const [meta, setMeta] = createSignal<ReceiverMetadata | null>(null);
  const [cursorHz, setCursorHz] = createSignal<number | null>(null);

  onMount(() => {
    const session = receiverRegistry.getOrCreate(props.receiverId);
    setMeta(session.getCurrentMetadata());
    setCursorHz(session.getCursor()?.hz ?? null);

    const unsubMeta = session.metadataStream.subscribe((m) => setMeta(m));
    const unsubCursor = session.cursorStream.subscribe((c) => {
      setCursorHz(c ? c.hz : null);
    });

    onCleanup(() => {
      unsubMeta();
      unsubCursor();
    });
  });

  const cfg = { ...DEFAULTS, ...((props.config ?? {}) as FrequencyCounterConfig) };

  // The big number is the cursor frequency when one is active (and followCursor
  // is enabled), else the tuned frequency from metadata.
  const primaryHz = (): number | null => {
    if (cfg.followCursor && cursorHz() !== null) return cursorHz();
    return meta() ? meta()!.frequency : null;
  };

  const showCursorBadge = (): boolean => cfg.followCursor && cursorHz() !== null;

  return (
    <div class="flex h-full w-full flex-col items-center justify-center bg-base-900 p-2">
      <div class="flex items-center gap-2">
        <div class="font-mono text-xs text-base-400">FREQUENCY</div>
        <Show when={showCursorBadge()}>
          <div class="rounded bg-cyan-500/20 px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wider text-cyan-300">
            cursor
          </div>
        </Show>
      </div>
      <div class="font-mono text-2xl font-bold text-cyan-450">
        {primaryHz() !== null ? formatHz(primaryHz()!) : '---.---- MHz'}
      </div>
      <Show when={showCursorBadge() && meta()}>
        {/* When showing the cursor, also reveal the tuned freq underneath so the
            user can compare cursor position vs tuned spot at a glance. */}
        <div class="font-mono text-[10px] text-base-400">
          tuned: {formatHz(meta()!.frequency)}
        </div>
      </Show>
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
