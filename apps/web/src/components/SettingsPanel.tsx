/** SettingsPanel — the in-app Settings modal (slice-5.1).
 *
 *  Six sections mirroring the backend UserSettings model:
 *    Display     — theme, waterfall colormap, spectrum peak hold / averaging,
 *                  frequency display unit, passband overlay toggle
 *    Audio       — master volume, preferred output device, default squelch,
 *                  force-mono toggle
 *    DSP         — default DSP mode (raw/classic), AGC, passband low/high cut,
 *                  notch filter defaults, noise blanker defaults
 *    Sources     — default source type, sample rate, center frequency
 *    Decoders    — auto-attach ADS-B / AIS / dump978
 *    Debug       — log capture toggle, ring capacities, async/unhandled
 *                  exception capture
 *
 *  All fields are bound to the local form state; the server is updated via
 *  PUT /api/settings on every change (debounced) so a crash mid-edit
 *  preserves progress. Reset to defaults is one click.
 */

import {
  createResource,
  createSignal,
  For,
  Show,
  type JSX,
} from 'solid-js';
import { api, type UserSettings } from '../lib/api';

export interface SettingsPanelProps {
  onClose: () => void;
}

type Section = 'display' | 'audio' | 'dsp' | 'sources' | 'decoders' | 'debug';

const SECTION_LABELS: Record<Section, string> = {
  display: 'Display',
  audio: 'Audio',
  dsp: 'DSP',
  sources: 'Sources',
  decoders: 'Decoders',
  debug: 'Debug',
};

const THEMES: UserSettings['display']['theme'][] = ['dark', 'light', 'system'];
const COLORMAPS: UserSettings['display']['waterfall_colormap'][] = [
  'viridis',
  'turbo',
  'jet',
  'grayscale',
];
const AVERAGING: UserSettings['display']['spectrum_averaging'][] = [
  'none',
  'linear',
  'exponential',
];
const FREQ_UNITS: UserSettings['display']['freq_display_unit'][] = [
  'hz',
  'khz',
  'mhz',
];
const DSP_MODES: UserSettings['dsp']['default_dsp_mode'][] = ['raw', 'classic'];

export function SettingsPanel(props: SettingsPanelProps): JSX.Element {
  const [settings, { refetch, mutate }] = createResource<UserSettings>(
    () => api.getUserSettings(),
  );
  const [activeSection, setActiveSection] = createSignal<Section>('display');
  const [saving, setSaving] = createSignal(false);
  const [savedAt, setSavedAt] = createSignal<number | null>(null);
  const [error, setError] = createSignal<string | null>(null);

  // Debounced update — sends a patch for just the changed field.
  let saveTimer: ReturnType<typeof setTimeout> | null = null;
  function updateField<K extends keyof UserSettings>(
    section: K,
    field: keyof UserSettings[K],
    value: unknown,
  ) {
    const current = settings();
    if (!current) return;
    // Optimistic update — mutate local cache so the UI feels instant.
    mutate({
      ...current,
      [section]: {
        ...(current[section] as object),
        [field]: value,
      },
    } as UserSettings);
    if (saveTimer) clearTimeout(saveTimer);
    setSaving(true);
    setError(null);
    saveTimer = setTimeout(async () => {
      try {
        const patch = {
          [section]: { [field]: value },
        } as Partial<UserSettings>;
        await api.updateUserSettings(patch);
        setSaving(false);
        setSavedAt(Date.now());
      } catch (err) {
        setSaving(false);
        setError(err instanceof Error ? err.message : 'Update failed');
        // Refetch to roll back the optimistic update.
        await refetch();
      }
    }, 250);
  }

  async function handleReset() {
    if (!confirm('Reset all settings to defaults?')) return;
    try {
      setError(null);
      const fresh = await api.resetUserSettings();
      mutate(fresh);
      setSavedAt(Date.now());
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Reset failed');
    }
  }

  const s = () => settings();

  return (
    <div class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm">
      <div class="w-full max-w-3xl rounded-lg border border-slate-700 bg-slate-900 shadow-2xl">
        {/* Header */}
        <div class="flex items-center justify-between border-b border-slate-700 px-6 py-4">
          <h2 class="text-lg font-semibold text-slate-100">Settings</h2>
          <div class="flex items-center gap-3">
            <Show when={saving()}>
              <span class="text-xs text-slate-400">Saving…</span>
            </Show>
            <Show when={savedAt() && !saving()}>
              <span class="text-xs text-emerald-400">Saved</span>
            </Show>
            <Show when={error()}>
              <span class="text-xs text-red-400" title={error() ?? ''}>
                Error
              </span>
            </Show>
            <button
              type="button"
              onClick={handleReset}
              class="rounded border border-slate-600 px-3 py-1 text-xs text-slate-300 hover:bg-slate-700"
            >
              Reset to defaults
            </button>
            <button
              type="button"
              onClick={props.onClose}
              class="rounded px-2 py-1 text-slate-400 hover:bg-slate-800 hover:text-slate-100"
              aria-label="Close settings"
            >
              ✕
            </button>
          </div>
        </div>

        {/* Body: section tabs + form */}
        <div class="flex min-h-[420px]">
          {/* Sidebar */}
          <nav class="w-40 shrink-0 border-r border-slate-800 bg-slate-950/40 px-2 py-4">
            <ul class="flex flex-col gap-1">
              <For each={Object.keys(SECTION_LABELS) as Section[]}>
                {(section) => (
                  <li>
                    <button
                      type="button"
                      onClick={() => setActiveSection(section)}
                      class={`w-full rounded px-3 py-2 text-left text-sm transition-colors ${
                        activeSection() === section
                          ? 'bg-slate-800 text-slate-100'
                          : 'text-slate-300 hover:bg-slate-800'
                      }`}
                    >
                      {SECTION_LABELS[section]}
                    </button>
                  </li>
                )}
              </For>
            </ul>
          </nav>

          {/* Form area */}
          <div class="flex-1 overflow-y-auto px-6 py-4">
            <Show
              when={s()}
              fallback={<div class="text-slate-400">Loading settings…</div>}
            >
              {(current) => (
                <>
                  <Show when={activeSection() === 'display'}>
                    <DisplaySection
                      display={current().display}
                      onChange={(field, value) =>
                        updateField('display', field, value)
                      }
                    />
                  </Show>
                  <Show when={activeSection() === 'audio'}>
                    <AudioSection
                      audio={current().audio}
                      onChange={(field, value) =>
                        updateField('audio', field, value)
                      }
                    />
                  </Show>
                  <Show when={activeSection() === 'dsp'}>
                    <DSPSection
                      dsp={current().dsp}
                      onChange={(field, value) =>
                        updateField('dsp', field, value)
                      }
                    />
                  </Show>
                  <Show when={activeSection() === 'sources'}>
                    <SourcesSection
                      sources={current().sources}
                      onChange={(field, value) =>
                        updateField('sources', field, value)
                      }
                    />
                  </Show>
                  <Show when={activeSection() === 'decoders'}>
                    <DecodersSection
                      decoders={current().decoders}
                      onChange={(field, value) =>
                        updateField('decoders', field, value)
                      }
                    />
                  </Show>
                  <Show when={activeSection() === 'debug'}>
                    <DebugSection
                      debug={current().debug}
                      onChange={(field, value) =>
                        updateField('debug', field, value)
                      }
                    />
                  </Show>
                </>
              )}
            </Show>
          </div>
        </div>
      </div>
    </div>
  );
}

// ---- Per-section form components --------------------------------------------

interface SectionProps<T> {
  onChange: <K extends keyof T>(field: K, value: T[K]) => void;
}

function DisplaySection(
  props: { display: UserSettings['display'] } & SectionProps<
    UserSettings['display']
  >,
): JSX.Element {
  return (
    <div class="space-y-4">
      <h3 class="text-sm font-semibold uppercase tracking-wide text-slate-400">
        Display
      </h3>
      <Field label="Theme">
        <Select
          value={props.display.theme}
          options={THEMES}
          onChange={(v) => props.onChange('theme', v)}
        />
      </Field>
      <Field label="Waterfall colormap">
        <Select
          value={props.display.waterfall_colormap}
          options={COLORMAPS}
          onChange={(v) => props.onChange('waterfall_colormap', v)}
        />
      </Field>
      <Field label="Spectrum peak hold">
        <Toggle
          checked={props.display.spectrum_show_peak_hold}
          onChange={(v) => props.onChange('spectrum_show_peak_hold', v)}
        />
      </Field>
      <Field label="Spectrum averaging">
        <Select
          value={props.display.spectrum_averaging}
          options={AVERAGING}
          onChange={(v) => props.onChange('spectrum_averaging', v)}
        />
      </Field>
      <Field label="Spectrum decay alpha (0-1, smoothing factor)">
        <NumberInput
          value={props.display.spectrum_decay_alpha}
          min={0}
          max={1}
          step={0.01}
          onChange={(v) => props.onChange('spectrum_decay_alpha', v)}
        />
      </Field>
      <Field label="Frequency display unit">
        <Select
          value={props.display.freq_display_unit}
          options={FREQ_UNITS}
          onChange={(v) => props.onChange('freq_display_unit', v)}
        />
      </Field>
      <Field label="Show passband overlay on the spectrum">
        <Toggle
          checked={props.display.show_passband_overlay}
          onChange={(v) => props.onChange('show_passband_overlay', v)}
        />
      </Field>
    </div>
  );
}

function AudioSection(
  props: { audio: UserSettings['audio'] } & SectionProps<UserSettings['audio']>,
): JSX.Element {
  return (
    <div class="space-y-4">
      <h3 class="text-sm font-semibold uppercase tracking-wide text-slate-400">
        Audio
      </h3>
      <Field label={`Master volume (${Math.round(props.audio.master_volume * 100)}%)`}>
        <RangeInput
          value={props.audio.master_volume}
          min={0}
          max={1}
          step={0.01}
          onChange={(v) => props.onChange('master_volume', v)}
        />
      </Field>
      <Field label="Preferred output device (empty = system default)">
        <TextInput
          value={props.audio.preferred_output_device}
          placeholder="e.g. speakers, hdmi, usb-headset"
          onChange={(v) => props.onChange('preferred_output_device', v)}
        />
      </Field>
      <Field label={`Default squelch (dBFS): ${props.audio.default_squelch_db}`}>
        <RangeInput
          value={props.audio.default_squelch_db}
          min={-150}
          max={0}
          step={1}
          onChange={(v) => props.onChange('default_squelch_db', v)}
        />
      </Field>
      <Field label="Force mono output">
        <Toggle
          checked={props.audio.force_mono}
          onChange={(v) => props.onChange('force_mono', v)}
        />
      </Field>
    </div>
  );
}

function DSPSection(
  props: { dsp: UserSettings['dsp'] } & SectionProps<UserSettings['dsp']>,
): JSX.Element {
  return (
    <div class="space-y-4">
      <h3 class="text-sm font-semibold uppercase tracking-wide text-slate-400">
        DSP defaults
      </h3>
      <p class="text-xs text-slate-500">
        Defaults applied to new receivers. Per-receiver overrides land in
        slice-5.2.
      </p>
      <Field label="Default DSP mode">
        <Select
          value={props.dsp.default_dsp_mode}
          options={DSP_MODES}
          onChange={(v) => props.onChange('default_dsp_mode', v)}
        />
      </Field>
      <Field label="Default AGC enabled">
        <Toggle
          checked={props.dsp.default_agc_enabled}
          onChange={(v) => props.onChange('default_agc_enabled', v)}
        />
      </Field>
      <Field label="Default low cut (Hz, blank = mode default)">
        <NumberInput
          value={props.dsp.default_low_cut_hz ?? 0}
          min={0}
          max={20000}
          step={10}
          onChange={(v) => props.onChange('default_low_cut_hz', v || null)}
        />
      </Field>
      <Field label="Default high cut (Hz, blank = mode default)">
        <NumberInput
          value={props.dsp.default_high_cut_hz ?? 0}
          min={0}
          max={20000}
          step={10}
          onChange={(v) => props.onChange('default_high_cut_hz', v || null)}
        />
      </Field>
      <Field label="Notch filter enabled by default">
        <Toggle
          checked={props.dsp.default_notch_enabled}
          onChange={(v) => props.onChange('default_notch_enabled', v)}
        />
      </Field>
      <Field label={`Default notch freq (Hz): ${props.dsp.default_notch_freq_hz}`}>
        <RangeInput
          value={props.dsp.default_notch_freq_hz}
          min={0}
          max={20000}
          step={10}
          onChange={(v) => props.onChange('default_notch_freq_hz', v)}
        />
      </Field>
      <Field label={`Default notch Q: ${props.dsp.default_notch_q}`}>
        <RangeInput
          value={props.dsp.default_notch_q}
          min={1}
          max={200}
          step={0.5}
          onChange={(v) => props.onChange('default_notch_q', v)}
        />
      </Field>
      <Field label="Noise blanker enabled by default">
        <Toggle
          checked={props.dsp.default_noise_blanker_enabled}
          onChange={(v) => props.onChange('default_noise_blanker_enabled', v)}
        />
      </Field>
      <Field
        label={`Noise blanker threshold: ${props.dsp.default_noise_blanker_threshold}`}
      >
        <RangeInput
          value={props.dsp.default_noise_blanker_threshold}
          min={0}
          max={1}
          step={0.01}
          onChange={(v) => props.onChange('default_noise_blanker_threshold', v)}
        />
      </Field>
    </div>
  );
}

function SourcesSection(
  props: { sources: UserSettings['sources'] } & SectionProps<
    UserSettings['sources']
  >,
): JSX.Element {
  return (
    <div class="space-y-4">
      <h3 class="text-sm font-semibold uppercase tracking-wide text-slate-400">
        Sources
      </h3>
      <p class="text-xs text-slate-500">
        Defaults applied at server startup. Changing these only affects new
        ReceiverSessions spawned after the change.
      </p>
      <Field label="Default source type (key into SourceRegistry)">
        <TextInput
          value={props.sources.default_source_type}
          placeholder="file | simulated | rtl_sdr | rtl_tcp | kiwi | …"
          onChange={(v) => props.onChange('default_source_type', v)}
        />
      </Field>
      <Field label="Default sample rate (Hz)">
        <NumberInput
          value={props.sources.default_sample_rate}
          min={250000}
          max={20000000}
          step={100000}
          onChange={(v) => props.onChange('default_sample_rate', v)}
        />
      </Field>
      <Field label="Default center frequency (Hz)">
        <NumberInput
          value={props.sources.default_center_freq}
          min={0}
          max={6000000000}
          step={1000}
          onChange={(v) => props.onChange('default_center_freq', v)}
        />
      </Field>
    </div>
  );
}

function DecodersSection(
  props: { decoders: UserSettings['decoders'] } & SectionProps<
    UserSettings['decoders']
  >,
): JSX.Element {
  return (
    <div class="space-y-4">
      <h3 class="text-sm font-semibold uppercase tracking-wide text-slate-400">
        Decoders
      </h3>
      <p class="text-xs text-slate-500">
        Auto-attach a decoder plugin when a new receiver spawns at the named
        band. Saves a click on the Aircraft panel.
      </p>
      <Field label="Auto-attach ADS-B on 1090 MHz receivers">
        <Toggle
          checked={props.decoders.auto_attach_adsb}
          onChange={(v) => props.onChange('auto_attach_adsb', v)}
        />
      </Field>
      <Field label="Auto-attach AIS on 162 MHz receivers (slice-5.5)">
        <Toggle
          checked={props.decoders.auto_attach_ais}
          onChange={(v) => props.onChange('auto_attach_ais', v)}
        />
      </Field>
      <Field label="Auto-attach dump978 UAT on 978 MHz receivers (slice-5.5)">
        <Toggle
          checked={props.decoders.auto_attach_dump978}
          onChange={(v) => props.onChange('auto_attach_dump978', v)}
        />
      </Field>
    </div>
  );
}

function DebugSection(
  props: { debug: UserSettings['debug'] } & SectionProps<
    UserSettings['debug']
  >,
): JSX.Element {
  return (
    <div class="space-y-4">
      <h3 class="text-sm font-semibold uppercase tracking-wide text-slate-400">
        Debug
      </h3>
      <p class="text-xs text-slate-500">
        The in-app Debugger panel captures structured log events into a
        ring buffer for live inspection. Disable capture here if it
        becomes noisy.
      </p>
      <Field label="Log capture enabled">
        <Toggle
          checked={props.debug.log_capture_enabled}
          onChange={(v) => props.onChange('log_capture_enabled', v)}
        />
      </Field>
      <Field label="All-events ring buffer capacity">
        <NumberInput
          value={props.debug.log_ring_capacity}
          min={100}
          max={10000}
          step={100}
          onChange={(v) => props.onChange('log_ring_capacity', v)}
        />
      </Field>
      <Field label="Errors-only ring buffer capacity">
        <NumberInput
          value={props.debug.error_ring_capacity}
          min={50}
          max={2000}
          step={50}
          onChange={(v) => props.onChange('error_ring_capacity', v)}
        />
      </Field>
      <Field label="Capture asyncio loop exceptions">
        <Toggle
          checked={props.debug.capture_async_exceptions}
          onChange={(v) => props.onChange('capture_async_exceptions', v)}
        />
      </Field>
      <Field label="Capture unhandled threading crashes">
        <Toggle
          checked={props.debug.capture_unhandled_exceptions}
          onChange={(v) => props.onChange('capture_unhandled_exceptions', v)}
        />
      </Field>
    </div>
  );
}

// ---- Generic form primitives -----------------------------------------------

function Field(props: { label: string; children: JSX.Element }): JSX.Element {
  return (
    <label class="block">
      <span class="block text-xs text-slate-400">{props.label}</span>
      <div class="mt-1">{props.children}</div>
    </label>
  );
}

function Select<T extends string>(
  props: { value: T; options: readonly T[]; onChange: (v: T) => void },
): JSX.Element {
  return (
    <select
      class="rounded border border-slate-700 bg-slate-800 px-2 py-1 text-sm text-slate-100"
      value={props.value}
      onChange={(e) => props.onChange(e.currentTarget.value as T)}
    >
      <For each={props.options}>
        {(opt) => <option value={opt}>{opt}</option>}
      </For>
    </select>
  );
}

function Toggle(
  props: { checked: boolean; onChange: (v: boolean) => void },
): JSX.Element {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={props.checked}
      onClick={() => props.onChange(!props.checked)}
      class={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${
        props.checked ? 'bg-emerald-500' : 'bg-slate-700'
      }`}
    >
      <span
        class={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
          props.checked ? 'translate-x-4' : 'translate-x-1'
        }`}
      />
    </button>
  );
}

function NumberInput(
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
      type="number"
      class="rounded border border-slate-700 bg-slate-800 px-2 py-1 text-sm text-slate-100"
      value={props.value}
      min={props.min}
      max={props.max}
      step={props.step}
      onInput={(e) => {
        const v = parseFloat(e.currentTarget.value);
        if (!Number.isNaN(v)) props.onChange(v);
      }}
    />
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

function TextInput(
  props: {
    value: string;
    placeholder?: string;
    onChange: (v: string) => void;
  },
): JSX.Element {
  return (
    <input
      type="text"
      class="rounded border border-slate-700 bg-slate-800 px-2 py-1 text-sm text-slate-100"
      value={props.value}
      placeholder={props.placeholder}
      onInput={(e) => props.onChange(e.currentTarget.value)}
    />
  );
}
