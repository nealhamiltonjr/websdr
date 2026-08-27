/** DSPControls — fine-grained per-receiver DSP control panel (slice-5.2).
 *
 *  Surfaces the fields of DSPParams (apps/server/openwebrx_plus/dsp/types.py)
 *  to the user, with the appropriate control per field:
 *
 *    Bandpass width      low_cut_hz + high_cut_hz sliders (Hz, mode-relative)
 *    AGC                 toggle (replaces the soft Limiter when on)
 *    Squelch             toggle + threshold slider (dBFS, 0 = max, -100 = silence)
 *    DC block            toggle (skip the DcBlock stage on AM/NFM)
 *    De-emphasis         toggle (NFM gets NfmDeemphasis when on; WFM always has it)
 *    Manual gain         toggle + dB slider (inserts a Gain block when AGC is off)
 *    Notch filter        UI badge "experimental" — accepted but no-op (slice-5.3)
 *    Noise blanker       UI badge "experimental" — accepted but no-op (slice-5.3)
 *
 *  All controls send `setDSPParams` WS commands via the SharedWorker. The
 *  backend merges the patch into the session's dsp_params and rebuilds
 *  the AudioChain. Metadata frames echo the merged params back so the
 *  UI stays in sync.
 *
 *  The panel is read-write: opening it for the first time pulls the
 *  current dsp_params from the metadata frames the SharedWorker has
 *  already received; subsequent edits push patches and update optimistically.
 */

import {
  createSignal,
  createEffect,
  Show,
  type JSX,
} from 'solid-js';
import type { DSPParams } from '../lib/api';

export interface DSPControlsProps {
  /** The active receiver id (controls edit THIS receiver's params). */
  receiverId: string;
  /** Current dsp_params as reported by the latest metadata frame.
   *  undefined until the first metadata frame arrives. */
  dspParams: DSPParams | undefined;
  /** Callback: send a setDSPParams patch via the SharedWorker. */
  onPatch: (patch: Partial<DSPParams>) => void;
  /** Close the panel. */
  onClose: () => void;
}

const NONE_LABEL = '(mode default)';

export function DSPControls(props: DSPControlsProps): JSX.Element {
  // Local form state — synced from props.dspParams when it changes.
  const [lowCutHz, setLowCutHz] = createSignal<number | null>(null);
  const [highCutHz, setHighCutHz] = createSignal<number | null>(null);
  const [agcEnabled, setAgcEnabled] = createSignal<boolean | null>(null);
  const [squelchDb, setSquelchDb] = createSignal<number | null>(null);
  const [squelchOn, setSquelchOn] = createSignal(false);
  const [dcBlock, setDcBlock] = createSignal<boolean | null>(null);
  const [deemphasis, setDeemphasis] = createSignal<boolean | null>(null);
  const [manualGainDb, setManualGainDb] = createSignal<number | null>(null);
  const [manualGainOn, setManualGainOn] = createSignal(false);
  const [notchOn, setNotchOn] = createSignal(false);
  const [notchFreq, setNotchFreq] = createSignal(1000);
  const [notchQ, setNotchQ] = createSignal(30);
  const [nbOn, setNbOn] = createSignal(false);
  // NB threshold is in dB above the noise floor (slice-7 — was a
  // fractional 0..1 placeholder; the new NoiseBlanker treats this as
  // dB, 6=gentle, 15=aggressive).
  const [nbThreshold, setNbThreshold] = createSignal(10);

  // Sync from props when the metadata frame updates the params.
  createEffect(() => {
    const p = props.dspParams;
    if (!p) return;
    setLowCutHz(p.low_cut_hz ?? null);
    setHighCutHz(p.high_cut_hz ?? null);
    setAgcEnabled(p.agc_enabled ?? null);
    setSquelchDb(p.squelch_db ?? -40);
    setSquelchOn(p.squelch_db !== null && p.squelch_db !== undefined);
    setDcBlock(p.dc_block_enabled ?? null);
    setDeemphasis(p.deemphasis_enabled ?? null);
    setManualGainDb(p.manual_gain_db ?? 6);
    setManualGainOn(p.manual_gain_db !== null && p.manual_gain_db !== undefined);
    setNotchOn(p.notch_enabled === true);
    setNotchFreq(p.notch_freq_hz ?? 1000);
    setNotchQ(p.notch_q ?? 30);
    setNbOn(p.noise_blanker_enabled === true);
    setNbThreshold(p.noise_blanker_threshold ?? 10);
  });

  // Patch senders — each one sends just the changed field(s) so the
  // backend's merge doesn't blow away other state.
  const patchLowCut = (v: number | null) => {
    setLowCutHz(v);
    props.onPatch({ low_cut_hz: v ?? undefined });
  };
  const patchHighCut = (v: number | null) => {
    setHighCutHz(v);
    props.onPatch({ high_cut_hz: v ?? undefined });
  };
  const patchAgc = (v: boolean) => {
    setAgcEnabled(v);
    props.onPatch({ agc_enabled: v });
  };
  const patchSquelch = (on: boolean) => {
    setSquelchOn(on);
    props.onPatch({ squelch_db: on ? squelchDb() ?? -40 : undefined });
  };
  const patchSquelchDb = (v: number) => {
    setSquelchDb(v);
    if (squelchOn()) props.onPatch({ squelch_db: v });
  };
  const patchDcBlock = (v: boolean) => {
    setDcBlock(v);
    props.onPatch({ dc_block_enabled: v });
  };
  const patchDeemphasis = (v: boolean) => {
    setDeemphasis(v);
    props.onPatch({ deemphasis_enabled: v });
  };
  const patchManualGain = (on: boolean) => {
    setManualGainOn(on);
    props.onPatch({ manual_gain_db: on ? manualGainDb() ?? 6 : undefined });
  };
  const patchManualGainDb = (v: number) => {
    setManualGainDb(v);
    if (manualGainOn()) props.onPatch({ manual_gain_db: v });
  };

  return (
    <div class="fixed inset-0 z-40 flex items-center justify-end bg-black/40">
      <div class="flex h-full w-80 flex-col rounded-l-lg border-l border-slate-700 bg-slate-900 shadow-2xl">
        {/* Header */}
        <div class="flex items-center justify-between border-b border-slate-700 px-4 py-3">
          <div>
            <h2 class="text-sm font-semibold text-slate-100">DSP Controls</h2>
            <p class="text-xs text-slate-400">rx: {props.receiverId}</p>
          </div>
          <button
            type="button"
            onClick={props.onClose}
            class="rounded px-2 py-1 text-slate-400 hover:bg-slate-800 hover:text-slate-100"
            aria-label="Close DSP controls"
          >
            ✕
          </button>
        </div>

        {/* Body — scrollable form */}
        <div class="flex-1 overflow-y-auto px-4 py-3">
          <Show
            when={props.dspParams}
            fallback={
              <div class="text-xs text-slate-500">
                Waiting for first metadata frame…
              </div>
            }
          >
            <div class="space-y-5">
              {/* Bandpass width */}
              <Section title="Bandpass width" hint="Narrow the channel filter — useful for noisy SSB or crowded AM. Blank = mode default.">
                <Field label={`Low cut (Hz): ${lowCutHz() ?? NONE_LABEL}`}>
                  <RangeOrClear
                    value={lowCutHz()}
                    min={-5000}
                    max={0}
                    step={10}
                    onChange={patchLowCut}
                  />
                </Field>
                <Field label={`High cut (Hz): ${highCutHz() ?? NONE_LABEL}`}>
                  <RangeOrClear
                    value={highCutHz()}
                    min={0}
                    max={10000}
                    step={10}
                    onChange={patchHighCut}
                  />
                </Field>
              </Section>

              {/* AGC */}
              <Section title="AGC" hint="Auto gain control replaces the soft limiter. Slope/attack/decay are pycsdr defaults for now.">
                <Field label="AGC enabled">
                  <Toggle
                    checked={agcEnabled() === true}
                    indeterminate={agcEnabled() === null}
                    onChange={patchAgc}
                  />
                </Field>
              </Section>

              {/* Manual gain (only meaningful when AGC is off) */}
              <Section title="Manual makeup gain" hint="Applied when AGC is off. Linear gain inserted before the resampler.">
                <Field label="Manual gain on">
                  <Toggle checked={manualGainOn()} onChange={patchManualGain} />
                </Field>
                <Show when={manualGainOn()}>
                  <Field label={`Gain (dB): ${manualGainDb()?.toFixed(1)}`}>
                    <RangeInput
                      value={manualGainDb() ?? 6}
                      min={-20}
                      max={40}
                      step={0.5}
                      onChange={patchManualGainDb}
                    />
                  </Field>
                </Show>
              </Section>

              {/* Squelch */}
              <Section title="Squelch" hint="Gates the complex IQ (between bandpass and demod) based on power. -40 dBFS is a typical starting point.">
                <Field label="Squelch on">
                  <Toggle checked={squelchOn()} onChange={patchSquelch} />
                </Field>
                <Show when={squelchOn()}>
                  <Field label={`Threshold (dBFS): ${squelchDb()}`}>
                    <RangeInput
                      value={squelchDb() ?? -40}
                      min={-100}
                      max={0}
                      step={1}
                      onChange={patchSquelchDb}
                    />
                  </Field>
                </Show>
              </Section>

              {/* DC block / de-emphasis */}
              <Section title="Conditioning" hint="Toggle the classic-mode conditioning stages. These override the mode profile when set.">
                <Field label="DC block (AM/NFM)">
                  <Toggle
                    checked={dcBlock() === true}
                    indeterminate={dcBlock() === null}
                    onChange={(v) => patchDcBlock(v)}
                  />
                </Field>
                <Field label="De-emphasis (NFM/WFM)">
                  <Toggle
                    checked={deemphasis() === true}
                    indeterminate={deemphasis() === null}
                    onChange={(v) => patchDeemphasis(v)}
                  />
                </Field>
              </Section>

              {/* Notch filter — LIVE (slice-7, complex IIR) */}
              <Section
                title="Notch filter"
                hint="Narrow single-pole complex IIR notch at +freq offset from center. Kills CW carriers / switching-PSU spurs without touching the opposite sideband. Q=30 ≈ 33 Hz bandwidth."
              >
                <Field label="Notch on">
                  <Toggle checked={notchOn()} onChange={(v) => {
                    setNotchOn(v);
                    props.onPatch({ notch_enabled: v });
                  }} />
                </Field>
                <Show when={notchOn()}>
                  <Field label={`Notch freq (Hz): ${notchFreq()}`}>
                    <RangeInput
                      value={notchFreq()}
                      min={-20000}
                      max={20000}
                      step={10}
                      onChange={(v) => {
                        setNotchFreq(v);
                        props.onPatch({ notch_freq_hz: v });
                      }}
                    />
                  </Field>
                  <Field label={`Notch Q: ${notchQ()}`}>
                    <RangeInput
                      value={notchQ()}
                      min={1}
                      max={200}
                      step={0.5}
                      onChange={(v) => {
                        setNotchQ(v);
                        props.onPatch({ notch_q: v });
                      }}
                    />
                  </Field>
                </Show>
              </Section>

              {/* Noise blanker — LIVE (slice-7, adaptive clipper) */}
              <Section
                title="Noise blanker"
                hint="Impulse-noise suppressor: tracks the running noise floor (5 ms EMA) and clips any sample whose magnitude exceeds the threshold (in dB above floor). 6 dB = gentle, 10 dB = balanced, 15 dB = aggressive."
              >
                <Field label="Noise blanker on">
                  <Toggle checked={nbOn()} onChange={(v) => {
                    setNbOn(v);
                    props.onPatch({ noise_blanker_enabled: v });
                  }} />
                </Field>
                <Show when={nbOn()}>
                  <Field label={`Threshold (dB above floor): ${nbThreshold().toFixed(1)}`}>
                    <RangeInput
                      value={nbThreshold()}
                      min={3}
                      max={30}
                      step={0.5}
                      onChange={(v) => {
                        setNbThreshold(v);
                        props.onPatch({ noise_blanker_threshold: v });
                      }}
                    />
                  </Field>
                </Show>
              </Section>
            </div>
          </Show>
        </div>
      </div>
    </div>
  );
}

// ---- Generic primitives ----------------------------------------------------

function Section(
  props: { title: string; hint?: string; badge?: string; children: JSX.Element },
): JSX.Element {
  return (
    <section class="space-y-2 border-b border-slate-800 pb-4">
      <div class="flex items-center gap-2">
        <h3 class="text-xs font-semibold uppercase tracking-wide text-slate-300">
          {props.title}
        </h3>
        <Show when={props.badge}>
          <span class="rounded bg-amber-500/20 px-1.5 py-0.5 text-[10px] font-bold uppercase text-amber-400">
            {props.badge}
          </span>
        </Show>
      </div>
      <Show when={props.hint}>
        <p class="text-[11px] leading-snug text-slate-500">{props.hint}</p>
      </Show>
      <div class="space-y-2">{props.children}</div>
    </section>
  );
}

function Field(props: { label: string; children: JSX.Element }): JSX.Element {
  return (
    <label class="block">
      <span class="block text-[11px] text-slate-400">{props.label}</span>
      <div class="mt-0.5">{props.children}</div>
    </label>
  );
}

function Toggle(
  props: { checked: boolean; indeterminate?: boolean; onChange: (v: boolean) => void },
): JSX.Element {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={props.checked}
      onClick={() => props.onChange(!props.checked)}
      class={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${
        props.checked ? 'bg-emerald-500' : 'bg-slate-700'
      } ${props.indeterminate ? 'ring-1 ring-amber-400' : ''}`}
      title={props.indeterminate ? 'Mode default — click to override' : ''}
    >
      <span
        class={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
          props.checked ? 'translate-x-4' : 'translate-x-1'
        }`}
      />
    </button>
  );
}

function RangeInput(
  props: {
    value: number;
    min?: number;
    max?: number;
    step?: number;
    onChange: (v: number) => void;
  },
): JSX.Element {
  return (
    <input
      type="range"
      class="w-full accent-emerald-500"
      value={props.value}
      min={props.min}
      max={props.max}
      step={props.step}
      onInput={(e) => props.onChange(parseFloat(e.currentTarget.value))}
    />
  );
}

function RangeOrClear(
  props: {
    value: number | null;
    min?: number;
    max?: number;
    step?: number;
    onChange: (v: number | null) => void;
  },
): JSX.Element {
  return (
    <div class="flex items-center gap-2">
      <input
        type="range"
        class="flex-1 accent-emerald-500"
        value={props.value ?? 0}
        min={props.min}
        max={props.max}
        step={props.step}
        onInput={(e) => props.onChange(parseFloat(e.currentTarget.value))}
      />
      <button
        type="button"
        class="rounded border border-slate-600 px-1.5 py-0.5 text-[10px] text-slate-300 hover:bg-slate-700"
        onClick={() => props.onChange(null)}
        title="Reset to mode default"
      >
        ✕
      </button>
    </div>
  );
}
