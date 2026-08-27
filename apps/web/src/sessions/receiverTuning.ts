/** Per-receiver tuning state — pure model behind the TuningBar + tab bar.
 *
 *  Slice-4.5: the TuningBar used to hardcode rx-default — with multiple
 *  receivers spawned (ADR-001's whole point), only the default could be
 *  tuned. This module introduces the ACTIVE RECEIVER concept:
 *
 *    - Every receiver has its own {frequency, mode} (learned from metadata
 *      frames, updated optimistically on control sends).
 *    - Exactly one receiver is "active" — the TuningBar shows and edits it.
 *    - Clicking a receiver tab makes it active; spawning makes the new
 *      receiver active; removing the active receiver falls back to the
 *      default (entry-point) receiver.
 *
 *  Slice-4.7: TuningState grows the gain + DSP-mode controls (the de-stubbed
 *  setGain/setDSPMode wire commands). Gain capability (gainRange, supportsAgc)
 *  is per-SOURCE, learned from the same metadata frames — the TuningBar
 *  renders the knob from whichever receiver is active.
 *
 *  Pure functions over an immutable state object so main.tsx only has to
 *  swap a signal — and so the transitions are unit-testable without a DOM.
 */

import type { DSPMode, ReceiverMode } from '@openwebrx-plus/shared-types';

/** One receiver's tuning state. */
export interface TuningState {
  /** Tuned frequency in Hz. 0 = unknown (metadata not yet received). */
  frequency: number;
  mode: ReceiverMode;
  /** Manual gain in dB, or null = auto/AGC (slice-4.7). */
  gain: number | null;
  /** ADR-002 DSP mode of the server-side audio chain. */
  dspMode: DSPMode;
  /** The source's advertised gain range (dB) — null = unknown/no range. */
  gainRange: [number, number] | null;
  /** Whether the source's "auto" gain is a real AGC (vs. unit gain). */
  supportsAgc: boolean;
}

/** Whole-model state: per-receiver tunings + which receiver is active. */
export interface ReceiverTuningModel {
  /** receiverId → tuning. Missing entries fall back to `defaults`. */
  tunings: Readonly<Record<string, TuningState>>;
  /** The receiver the TuningBar edits. Always a live receiver id. */
  activeId: string;
  /** Fallback values for receivers with no metadata yet. */
  defaults: TuningState;
}

/** Default slice-4.7 control values for receivers with no metadata yet. */
export const CONTROL_DEFAULTS = {
  gain: null,
  dspMode: 'classic' as DSPMode,
  gainRange: null,
  supportsAgc: false,
};

export function initialTuningModel(
  defaultReceiverId: string,
  defaults: Pick<TuningState, 'frequency' | 'mode'> &
    Partial<Pick<TuningState, 'gain' | 'dspMode' | 'gainRange' | 'supportsAgc'>>,
): ReceiverTuningModel {
  return {
    tunings: {},
    activeId: defaultReceiverId,
    defaults: { ...CONTROL_DEFAULTS, ...defaults },
  };
}

/** Payload shape of a metadata frame's control-relevant fields. */
export interface MetadataControls {
  frequency: number;
  mode: string;
  gain?: number | null;
  dspMode?: string;
  gainRange?: [number, number] | null;
  supportsAgc?: boolean;
}

function sameGainRange(
  a: [number, number] | null | undefined,
  b: [number, number] | null | undefined,
): boolean {
  if (a == null || b == null) return a == b;
  return a[0] === b[0] && a[1] === b[1];
}

/** Merge one receiver's metadata into the model (creates or updates).
 *
 *  Metadata frames arrive after every binary frame per receiver — this is
 *  the source of truth; optimistic control updates are only a bridge until
 *  the confirming metadata lands.
 */
export function applyMetadata(
  model: ReceiverTuningModel,
  receiverId: string,
  meta: MetadataControls,
): ReceiverTuningModel {
  const prev = model.tunings[receiverId];
  if (prev) {
    // Skip the no-op case: metadata repeats at stream fps and Solid signals
    // only re-run dependents on value CHANGES — keep the record referentially
    // stable when nothing changed.
    if (
      prev.frequency === meta.frequency &&
      prev.mode === meta.mode &&
      prev.gain === (meta.gain ?? null) &&
      prev.dspMode === (meta.dspMode ?? prev.dspMode) &&
      sameGainRange(prev.gainRange, meta.gainRange) &&
      prev.supportsAgc === !!meta.supportsAgc
    ) {
      return model;
    }
  }
  const base: TuningState = prev ?? model.defaults;
  return {
    ...model,
    tunings: {
      ...model.tunings,
      [receiverId]: {
        frequency: meta.frequency,
        mode: meta.mode as ReceiverMode,
        gain: meta.gain ?? null,
        dspMode: (meta.dspMode ?? base.dspMode) as DSPMode,
        gainRange: meta.gainRange ?? null,
        supportsAgc: !!meta.supportsAgc,
      },
    },
  };
}

/** Optimistically apply a control send (setFrequency / setMode / setGain /
 *  setDSPMode). `gain` may be null (= auto). */
export function applyControl(
  model: ReceiverTuningModel,
  receiverId: string,
  update: {
    frequency?: number;
    mode?: ReceiverMode;
    gain?: number | null;
    dspMode?: DSPMode;
  },
): ReceiverTuningModel {
  const prev = model.tunings[receiverId] ?? model.defaults;
  const next: TuningState = {
    ...prev,
    frequency: update.frequency ?? prev.frequency,
    mode: update.mode ?? prev.mode,
    gain: update.gain !== undefined ? update.gain : prev.gain,
    dspMode: update.dspMode ?? prev.dspMode,
  };
  if (
    prev.frequency === next.frequency &&
    prev.mode === next.mode &&
    prev.gain === next.gain &&
    prev.dspMode === next.dspMode
  ) {
    return model;
  }
  return {
    ...model,
    tunings: { ...model.tunings, [receiverId]: next },
  };
}

/** Select the active receiver (tab click / spawn adoption). */
export function setActive(
  model: ReceiverTuningModel,
  receiverId: string,
): ReceiverTuningModel {
  if (model.activeId === receiverId) return model;
  return { ...model, activeId: receiverId };
}

/** Drop a removed receiver: forget its tuning, repoint the active id.
 *
 *  If the removed receiver was active, fall back to `fallbackId` (the
 *  default receiver — the entry point that can never be removed).
 */
export function dropReceiver(
  model: ReceiverTuningModel,
  receiverId: string,
  fallbackId: string,
): ReceiverTuningModel {
  if (!(receiverId in model.tunings) && model.activeId !== receiverId) {
    return model;
  }
  const tunings = { ...model.tunings };
  delete tunings[receiverId];
  return {
    ...model,
    tunings,
    activeId: model.activeId === receiverId ? fallbackId : model.activeId,
  };
}

/** The active receiver's tuning (falls back to defaults before metadata). */
export function activeTuning(model: ReceiverTuningModel): TuningState {
  return model.tunings[model.activeId] ?? model.defaults;
}

/** Compact label for a receiver tab chip: id + tuned frequency. */
export function tabLabel(
  receiverId: string,
  tuning: TuningState | undefined,
  defaults: TuningState,
): string {
  const t = tuning ?? defaults;
  const freq = t.frequency > 0 ? ` · ${formatHzShort(t.frequency)}` : '';
  return `${receiverId.slice(0, 12)}${freq}`;
}

/** Short Hz formatting for tab chips (compact — the tab bar is 11 px mono). */
export function formatHzShort(hz: number): string {
  if (hz >= 1_000_000_000) return `${(hz / 1e9).toFixed(3)}G`;
  if (hz >= 1_000_000) return `${(hz / 1e6).toFixed(4)}M`;
  if (hz >= 1_000) return `${(hz / 1e3).toFixed(2)}k`;
  return `${hz}`;
}
