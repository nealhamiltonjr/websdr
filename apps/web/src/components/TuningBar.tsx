/** TuningBar — frequency + mode + gain + DSP-mode controls for the top of
 *  the main route.
 *
 *  Owned by the MainRoute, but stateless beyond local SolidJS signals.
 *  Sends control messages to the backend via the SharedWorker (passed in
 *  via props so this component stays decoupled from the worker bootstrap).
 *
 *  Slice-1.5: setFrequency + setMode.
 *  Slice-4.5: edits the ACTIVE receiver — `receiverLabel` shows which one
 *  is on the knob, and the frequency input no longer clobbers mid-typing:
 *  backend metadata echoes arrive at stream fps, so the input only syncs
 *  when the field is NOT focused.
 *  Slice-4.7: the de-stubbed setGain / setDSPMode — a gain slider bounded
 *  by the source's advertised range (with an AUTO toggle when the source
 *  has a real AGC) and the ADR-002 DSP-mode dropdown (ai/cascade disabled
 *  until the DeepFilterNet module ships).
 */

import { createSignal, createEffect, Show, For } from 'solid-js';
import type { DSPMode, ReceiverMode } from '@openwebrx-plus/shared-types';
import { DSP_MODE_OPTIONS, DEFAULT_GAIN_RANGE, formatGainDb } from './tuningControls';

export interface TuningBarProps {
  /** Which receiver this bar edits (slice-4.5 — shown as a chip). */
  receiverLabel: string;
  /** Current frequency in Hz. Updated when the backend reports a new
   *  metadata frame. */
  frequency: number;
  /** Current mode. Updated when the backend reports a new metadata frame. */
  mode: ReceiverMode;
  /** Manual gain in dB, or null = auto/AGC (slice-4.7). */
  gain: number | null;
  /** The source's advertised gain range — slider bounds. null = [0, 49]. */
  gainRange: [number, number] | null;
  /** Whether AUTO is a real AGC on this source (hides the toggle if not). */
  supportsAgc: boolean;
  /** Server-side DSP mode (ADR-002). */
  dspMode: DSPMode;
  /** Called when the user wants to change frequency. Caller forwards via
   *  SharedWorker → backend. */
  onSetFrequency: (hz: number) => void;
  /** Called when the user wants to change mode. */
  onSetMode: (mode: ReceiverMode) => void;
  /** Called when the user wants to change gain (null = auto). */
  onSetGain: (db: number | null) => void;
  /** Called when the user wants to change the DSP mode. */
  onSetDSPMode: (mode: DSPMode) => void;
}

export const MODES: ReceiverMode[] = [
  'USB',
  'LSB',
  'AM',
  'SAM',
  'FM',
  'NBFM',
  'WBFM',
  'CW',
  'FreeDV',
  'RTTY',
  'PSK31',
  'PSK63',
  'Olivia',
  'FT8',
  'JT65',
  'JT9',
  'WSPR',
  'SSTV',
  'FAX',
  'Packet',
  'DAB',
  'ADS-B',
  'UAT',
  'AIS',
  'ATC',
  'ACARS',
];

/** ADR-002 DSP modes + gain formatting live in ./tuningControls.ts (pure,
 *  unit-testable). */

// Common ham band presets for the dropdown.
const BAND_PRESETS: { label: string; freqHz: number; mode: ReceiverMode }[] = [
  { label: '160m', freqHz: 1_900_000, mode: 'LSB' },
  { label: '80m', freqHz: 3_800_000, mode: 'LSB' },
  { label: '60m', freqHz: 5_405_000, mode: 'USB' },
  { label: '40m', freqHz: 7_200_000, mode: 'LSB' },
  { label: '30m', freqHz: 10_130_000, mode: 'USB' },
  { label: '20m', freqHz: 14_205_000, mode: 'USB' },
  { label: '17m', freqHz: 18_140_000, mode: 'USB' },
  { label: '15m', freqHz: 21_300_000, mode: 'USB' },
  { label: '12m', freqHz: 24_950_000, mode: 'USB' },
  { label: '10m', freqHz: 28_500_000, mode: 'USB' },
  { label: '6m', freqHz: 50_150_000, mode: 'USB' },
  { label: '2m', freqHz: 145_500_000, mode: 'FM' },
  { label: '70cm', freqHz: 435_000_000, mode: 'FM' },
  { label: 'ADS-B 1090', freqHz: 1_090_000_000, mode: 'ADS-B' },
  { label: 'ADS-B 978 (UAT)', freqHz: 978_000_000, mode: 'UAT' },
  { label: 'AIS Marine', freqHz: 161_975_000, mode: 'AIS' },
  { label: 'Air Band', freqHz: 122_750_000, mode: 'AM' },
  { label: 'ATC ACARS', freqHz: 131_550_000, mode: 'ACARS' },
  { label: 'FM Broadcast', freqHz: 98_500_000, mode: 'WBFM' },
  { label: 'DAB', freqHz: 220_000_000, mode: 'DAB' },
];

// Slider bounds: 0 to 2 GHz (covers RTL-SDR + most ham bands).
const SLIDER_MIN_HZ = 0;
const SLIDER_MAX_HZ = 2_000_000_000;

function formatHz(hz: number): string {
  if (hz >= 1_000_000_000) return `${(hz / 1e9).toFixed(6)} GHz`;
  if (hz >= 1_000_000) return `${(hz / 1e6).toFixed(4)} MHz`;
  if (hz >= 1_000) return `${(hz / 1e3).toFixed(3)} kHz`;
  return `${hz.toFixed(0)} Hz`;
}

export function TuningBar(props: TuningBarProps) {
  // Local input state — tracks what the user typed, only commits on Enter /
  // blur to avoid sending a control message per keystroke.
  const [inputValue, setInputValue] = createSignal(formatHz(props.frequency));
  // Whether the numeric input currently has focus — while it does, metadata
  // echoes must NOT clobber what the user is typing (slice-4.5).
  const [inputFocused, setInputFocused] = createSignal(false);

  // Keep input in sync with backend-reported frequency when it changes
  // (e.g., from a band preset click, another window, or switching the active
  // receiver) — but never while the user is mid-edit in the field.
  createEffect(() => {
    const f = props.frequency;
    const label = props.receiverLabel;
    if (!inputFocused()) {
      setInputValue(formatHz(f));
    }
    void label; // receiver switches should also refresh the display
  });

  const handleSliderChange = (e: Event) => {
    const target = e.target as HTMLInputElement;
    const hz = Number(target.value);
    props.onSetFrequency(hz);
  };

  const handleInputChange = (e: Event) => {
    const target = e.target as HTMLInputElement;
    setInputValue(target.value);
  };

  const commitInput = () => {
    const parsed = parseFreqHz(inputValue());
    if (parsed !== null && parsed !== props.frequency) {
      props.onSetFrequency(parsed);
    } else {
      // Reset to last known good value.
      setInputValue(formatHz(props.frequency));
    }
  };

  const handlePreset = (e: Event) => {
    const target = e.target as HTMLSelectElement;
    const preset = BAND_PRESETS.find((p) => p.label === target.value);
    if (preset) {
      props.onSetFrequency(preset.freqHz);
      props.onSetMode(preset.mode);
    }
  };

  // ---- Gain knob (slice-4.7) --------------------------------------------

  const gainBounds = (): [number, number] => props.gainRange ?? DEFAULT_GAIN_RANGE;
  const handleGainSlider = (e: Event) => {
    const target = e.target as HTMLInputElement;
    props.onSetGain(Number(target.value));
  };

  // ---- Render -------------------------------------------------------------

  return (
    <div class="flex flex-col border-b border-base-800 bg-base-850">
      {/* Row 1 — frequency + mode (the classic slice-1.5/4.5 bar) */}
      <div class="flex h-12 items-center gap-3 px-4">
        {/* Active-receiver chip (slice-4.5) — what this bar edits */}
        <span
          class="rounded bg-amber-450/15 px-2 py-0.5 font-mono text-xs text-amber-450"
          title="Active receiver — click a receiver tab to switch. The tuning bar and audio follow it."
        >
          ⦿ {props.receiverLabel.slice(0, 14)}
        </span>

        {/* Band preset dropdown */}
        <select
          class="rounded border border-base-700 bg-base-800 px-2 py-1 font-mono text-xs text-base-200 hover:bg-base-700"
          onChange={handlePreset}
          title="Quick band presets"
        >
          <option value="">Band presets…</option>
          <For each={BAND_PRESETS}>{(p) => <option value={p.label}>{p.label}</option>}</For>
        </select>

        {/* Frequency slider */}
        <input
          type="range"
          min={SLIDER_MIN_HZ}
          max={SLIDER_MAX_HZ}
          step={100}
          value={props.frequency}
          onInput={handleSliderChange}
          class="h-1 flex-1 cursor-pointer appearance-none rounded-full bg-base-700 accent-amber-450"
          title="Drag to tune"
        />

        {/* Numeric input */}
        <input
          type="text"
          value={inputValue()}
          onInput={handleInputChange}
          onFocus={() => setInputFocused(true)}
          onBlur={() => {
            setInputFocused(false);
            commitInput();
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') commitInput();
          }}
          class="w-44 rounded border border-base-700 bg-base-900 px-2 py-1 font-mono text-sm text-cyan-450 focus:border-cyan-450 focus:outline-none"
          title="Type a frequency (e.g. 14.205 MHz)"
        />

        {/* Mode dropdown */}
        <select
          class="rounded border border-base-700 bg-base-800 px-2 py-1 font-mono text-xs text-base-200 hover:bg-base-700"
          value={props.mode}
          onChange={(e) => {
            const target = e.target as HTMLSelectElement;
            props.onSetMode(target.value as ReceiverMode);
          }}
          title="Demodulation mode"
        >
          <For each={MODES}>{(m) => <option value={m}>{m}</option>}</For>
        </select>

        {/* Live readout */}
        <Show when={props.frequency > 0}>
          <span class="font-mono text-xs text-base-400">
            {formatHz(props.frequency)} · {props.mode}
          </span>
        </Show>
      </div>

      {/* Row 2 — gain + DSP mode (slice-4.7) */}
      <div class="flex h-7 items-center gap-3 border-t border-base-800/60 px-4 font-mono text-[11px]">
        <span class="text-base-400" title="Manual gain applied to the active receiver's source">
          GAIN
        </span>
        <input
          type="range"
          min={gainBounds()[0]}
          max={gainBounds()[1]}
          step={0.5}
          value={props.gain ?? 0}
          onInput={handleGainSlider}
          class="h-1 w-44 cursor-pointer appearance-none rounded-full bg-base-700 accent-amber-450"
          title={`Gain slider (${gainBounds()[0]} … ${gainBounds()[1]} dB)`}
        />
        <span
          class="w-16 text-cyan-450"
          title={
            props.supportsAgc
              ? 'Manual gain in dB — or AUTO (source AGC)'
              : 'Manual digital/hardware gain in dB'
          }
        >
          {formatGainDb(props.gain)}
        </span>
        <Show when={props.supportsAgc}>
          <button
            type="button"
            class={`rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${
              props.gain === null
                ? 'bg-amber-450/20 text-amber-450 ring-1 ring-amber-450/40'
                : 'bg-base-800 text-base-300 hover:bg-base-700'
            }`}
            onClick={() => props.onSetGain(null)}
            title="Hand gain back to the source's AGC"
          >
            auto
          </button>
        </Show>

        <span class="ml-4 text-base-400" title="Server-side DSP mode of the audio chain (ADR-002)">
          DSP
        </span>
        <select
          class="rounded border border-base-700 bg-base-800 px-2 py-0.5 font-mono text-[11px] text-base-200 hover:bg-base-700"
          value={props.dspMode}
          onChange={(e) => {
            const target = e.target as HTMLSelectElement;
            props.onSetDSPMode(target.value as DSPMode);
          }}
          title="DSP mode: raw = unconditioned demod output · classic = conditioned audio"
        >
          <For each={DSP_MODE_OPTIONS}>
            {(opt) => (
              <option value={opt.value} disabled={!opt.available} title={opt.hint}>
                {opt.label}
              </option>
            )}
          </For>
        </select>
      </div>
    </div>
  );
}

/** Parse a human-typed frequency string into Hz.
 *  Accepts: "14.205 MHz", "14.205m", "14.2", "14205000", "1.5 GHz", "146.52"
 */
export function parseFreqHz(s: string): number | null {
  const trimmed = s.trim().toLowerCase();
  if (!trimmed) return null;

  // Try parsing with unit suffix first.
  const unitMatch = trimmed.match(/^([\d.]+)\s*([kmg]?h?z?)$/);
  if (unitMatch) {
    const val = parseFloat(unitMatch[1]);
    if (isNaN(val)) return null;
    const unit = unitMatch[2];
    if (unit.startsWith('g')) return val * 1e9;
    if (unit.startsWith('m')) return val * 1e6;
    if (unit.startsWith('k')) return val * 1e3;
    return val;
  }

  // Bare number — assume Hz.
  const n = Number(trimmed);
  return isNaN(n) ? null : n;
}
