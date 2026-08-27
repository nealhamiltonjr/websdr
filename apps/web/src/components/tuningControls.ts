/** Pure tuning-control constants + helpers (slice-4.7).
 *
 *  Split out of TuningBar.tsx so they're unit-testable in the node vitest
 *  environment without dragging solid-js/web in (same pattern as
 *  sourceFormModel.ts).
 */

import type { DSPMode } from '@openwebrx-plus/shared-types';

/** ADR-002 DSP modes — ai/cascade need the server-side DeepFilterNet module
 *  (not built yet), so they render disabled with the reason in a tooltip. */
export const DSP_MODE_OPTIONS: {
  value: DSPMode;
  label: string;
  available: boolean;
  hint: string;
}[] = [
  {
    value: 'raw',
    label: 'raw',
    available: true,
    hint: 'Demodulator output unconditioned — no DC block, no WFM de-emphasis, no limiter',
  },
  {
    value: 'classic',
    label: 'classic',
    available: true,
    hint: 'Conditioned audio (default): DC block, WFM de-emphasis, ±1.0 limiter',
  },
  {
    value: 'ai',
    label: 'ai',
    available: false,
    hint: 'Requires the DeepFilterNet AI module (ADR-002) — not built yet',
  },
  {
    value: 'cascade',
    label: 'cascade',
    available: false,
    hint: 'Requires the DeepFilterNet AI module (ADR-002) — not built yet',
  },
];

/** Fallback slider bounds when the source advertises no gain range. */
export const DEFAULT_GAIN_RANGE: [number, number] = [0, 49];

/** Format a gain value for the readout: "+6.0 dB" / "-3.5 dB" / "auto". */
export function formatGainDb(db: number | null): string {
  if (db == null) return 'auto';
  return `${db >= 0 ? '+' : ''}${db.toFixed(1)} dB`;
}
