/** AddReceiverModal — the source picker (frontend side of ADR-004 + ADR-006).
 *
 *  Four sections:
 *    Quick connect  — paste a remote receiver URL (deep links honored)
 *    Catalog        — every registered Source manifest with per-source
 *                     config forms, hardware-badged via /api/hardware
 *    Remote         — browse public receiver directories (RemoteBrowser)
 *    VFO            — spawn a sub-receiver tapping a wideband parent
 *                     (ADR-005 source_type="vfo")
 *
 *  All spawns go through POST /api/receivers; the caller subscribes to the
 *  returned receiver id over the SharedWorker.
 */

import {
  createEffect,
  createResource,
  createSignal,
  For,
  Show,
  type JSX,
} from 'solid-js';
import {
  api,
  parseRemoteUrl,
  formatHz,
  type SourceManifest,
} from '../lib/api';
import { SourceConfigForm } from './sourceForms';
import {
  SOURCE_NOTES,
  collectKwargs,
  defaultValues,
} from './sourceFormModel';
import { RemoteBrowser } from './RemoteBrowser';
import { MODES, parseFreqHz } from './TuningBar';
import type { ReceiverMode } from '@openwebrx-plus/shared-types';

export type Section = 'quick' | 'catalog' | 'remote' | 'vfo';

export interface AddReceiverModalProps {
  onClose: () => void;
  /** Called after a successful spawn — caller subscribes + adds a tab. */
  onSpawned: (receiverId: string) => void;
  /** Section to open first (e.g. 'vfo' from a receiver tab's + VFO button). */
  initialSection?: Section;
}


const SECTIONS: { id: Section; label: string; hint: string }[] = [
  { id: 'quick', label: 'Quick connect', hint: 'paste a receiver URL' },
  { id: 'catalog', label: 'Source catalog', hint: 'local hardware · files · simulated' },
  { id: 'remote', label: 'Remote receivers', hint: 'public directory browser' },
  { id: 'vfo', label: 'VFO sub-receiver', hint: 'tap a wideband parent (ADR-005)' },
];

const inputCls =
  'w-full rounded border border-base-700 bg-base-950 px-2 py-1 font-mono text-xs text-base-100 placeholder:text-base-500 focus:border-cyan-450 focus:outline-none';

export function AddReceiverModal(props: AddReceiverModalProps): JSX.Element {
  const [section, setSection] = createSignal<Section>(props.initialSection ?? 'quick');
  const [connecting, setConnecting] = createSignal(false);
  const [error, setError] = createSignal('');

  const spawn = async (req: Parameters<typeof api.spawnReceiver>[0]) => {
    setConnecting(true);
    setError('');
    try {
      const res = await api.spawnReceiver(req);
      props.onSpawned(res.receiver_id);
      props.onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setConnecting(false);
    }
  };

  return (
    <div class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6">
      <div class="flex h-[560px] w-full max-w-4xl overflow-hidden rounded-lg border border-base-700 bg-base-900 shadow-2xl">
        {/* Left nav */}
        <nav class="flex w-52 shrink-0 flex-col gap-1 border-r border-base-800 bg-base-850 p-2">
          <div class="px-2 pb-2 pt-1 font-mono text-[10px] uppercase tracking-widest text-base-500">
            Add receiver
          </div>
          <For each={SECTIONS}>
            {(s) => (
              <button
                type="button"
                class={`rounded px-2.5 py-2 text-left ${
                  section() === s.id
                    ? 'bg-cyan-450/15 text-cyan-450'
                    : 'text-base-300 hover:bg-base-800'
                }`}
                onClick={() => setSection(s.id)}
              >
                <span class="block font-mono text-xs font-semibold">{s.label}</span>
                <span class="block font-mono text-[10px] text-base-500">{s.hint}</span>
              </button>
            )}
          </For>
          <div class="mt-auto px-2 pb-1">
            <Show when={error()}>
              <div class="rounded border border-rose-450/40 bg-rose-450/10 p-2 font-mono text-[10px] leading-relaxed text-rose-450">
                {error()}
              </div>
            </Show>
          </div>
        </nav>

        {/* Body */}
        <div class="flex min-w-0 flex-1 flex-col">
          <header class="flex h-9 shrink-0 items-center justify-between border-b border-base-800 px-4">
            <span class="font-mono text-xs font-semibold text-base-200">
              {SECTIONS.find((s) => s.id === section())?.label}
            </span>
            <button
              type="button"
              class="rounded bg-base-800 px-2 py-0.5 font-mono text-xs text-base-300 hover:bg-base-700"
              onClick={props.onClose}
            >
              esc
            </button>
          </header>
          <div class="min-h-0 flex-1 overflow-auto p-4">
            <Show when={section() === 'quick'}>
              <QuickConnect onSpawn={spawn} connecting={connecting()} />
            </Show>
            <Show when={section() === 'catalog'}>
              <Catalog onSpawn={spawn} connecting={connecting()} />
            </Show>
            <Show when={section() === 'remote'}>
              <RemoteBrowser
                onConnect={(sourceType, sourceKwargs) => void spawn({ source_type: sourceType, source_kwargs: sourceKwargs })}
                connecting={connecting()}
              />
            </Show>
            <Show when={section() === 'vfo'}>
              <VfoPanel onSpawn={spawn} connecting={connecting()} />
            </Show>
          </div>
        </div>
      </div>
    </div>
  );
}

// ---- Quick connect -----------------------------------------------------------

function QuickConnect(props: {
  onSpawn: (req: Parameters<typeof api.spawnReceiver>[0]) => Promise<void>;
  connecting: boolean;
}): JSX.Element {
  const [url, setUrl] = createSignal('');
  const [kind, setKind] = createSignal<'openwebrx_remote' | 'kiwi'>('openwebrx_remote');

  const parsed = () => parseRemoteUrl(url(), kind());

  const connect = () => {
    const p = parsed();
    if (!p) return;
    const req: Parameters<typeof api.spawnReceiver>[0] = {
      source_type: p.sourceType,
      source_kwargs: p.sourceKwargs,
    };
    // Pass the deep-link frequency/mode through as the requested tuning —
    // openwebrx_remote honors the URL hash itself, but this also seeds the
    // session's metadata so the UI shows the right numbers immediately.
    if (p.freqHz !== null && p.freqHz > 0) req.center_freq = p.freqHz;
    if (p.mod) req.mode = p.mod.toUpperCase();
    void props.onSpawn(req);
  };

  return (
    <div class="flex max-w-xl flex-col gap-3">
      <p class="font-mono text-[11px] leading-relaxed text-base-400">
        Paste any public receiver URL. OpenWebRX(+) deep links carry their
        tuning in the hash — <span class="text-cyan-450">#freq=…,mod=…,sql=…</span> —
        and are honored end to end.
      </p>
      <div class="flex overflow-hidden rounded border border-base-700">
        <For each={[['openwebrx_remote', 'OpenWebRX(+)'], ['kiwi', 'KiwiSDR']] as const}>
          {([value, label]) => (
            <button
              type="button"
              class={`px-3 py-1 font-mono text-[11px] ${
                kind() === value
                  ? 'bg-cyan-450/20 text-cyan-450'
                  : 'bg-base-850 text-base-300 hover:bg-base-800'
              }`}
              onClick={() => setKind(value)}
            >
              {label}
            </button>
          )}
        </For>
      </div>
      <input
        type="text"
        class={inputCls}
        placeholder={
          kind() === 'openwebrx_remote'
            ? 'http://boomerthedog.com:8073/#freq=3570000,mod=lsb,sql=-150'
            : 'http://rx.example.kiwisdr.com:8073/'
        }
        value={url()}
        onInput={(e) => setUrl((e.target as HTMLInputElement).value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') connect();
        }}
        spellcheck={false}
      />
      <Show when={parsed()} keyed>
        {(p) => (
          <div class="rounded border border-base-800 bg-base-950 p-2 font-mono text-[11px] text-base-300">
            <Show when={kind() === 'openwebrx_remote'} fallback={<div>host: <span class="text-cyan-450">{String(p.sourceKwargs.host)}</span> · port: <span class="text-cyan-450">{String(p.sourceKwargs.port)}</span></div>}>
              <div class="flex flex-wrap gap-x-4 gap-y-1">
                <span>
                  receiver: <span class="text-cyan-450">{String(p.sourceKwargs.url)}</span>
                </span>
                <Show when={p.freqHz}>
                  <span>
                    tune: <span class="text-amber-450">{formatHz(p.freqHz!)}</span>
                  </span>
                </Show>
                <Show when={p.mod}>
                  <span>
                    mode: <span class="text-amber-450">{p.mod!.toUpperCase()}</span>
                  </span>
                </Show>
              </div>
            </Show>
          </div>
        )}
      </Show>
      <div class="flex items-center gap-3">
        <button
          type="button"
          class="rounded bg-cyan-450/20 px-3 py-1.5 font-mono text-xs font-semibold text-cyan-450 hover:bg-cyan-450/30 disabled:opacity-50"
          disabled={props.connecting || !parsed()}
          onClick={connect}
        >
          {props.connecting ? 'connecting…' : 'connect'}
        </button>
        <span class="font-mono text-[10px] text-base-500">
          spawns a receiver pane with remote waterfall + audio
        </span>
      </div>
    </div>
  );
}

// ---- Catalog ------------------------------------------------------------------

function Catalog(props: {
  onSpawn: (req: Parameters<typeof api.spawnReceiver>[0]) => Promise<void>;
  connecting: boolean;
}): JSX.Element {
  const [sources] = createResource(() => api.listSources());
  const [hardware] = createResource(() => api.listHardware());
  const [selected, setSelected] = createSignal<string>('simulated');
  const [values, setValues] = createSignal<Record<string, string | boolean>>({});
  const [freqText, setFreqText] = createSignal('14.205 MHz');
  const [mode, setMode] = createSignal<ReceiverMode>('USB');
  const [showAdvanced, setShowAdvanced] = createSignal(false);
  const [rateText, setRateText] = createSignal('');

  const manifests = (): SourceManifest[] =>
    (sources.latest ?? sources() ?? []).slice().sort((a, b) => {
      // Offline-friendly first, then hardware, then remote.
      const rank = (m: SourceManifest) =>
        m.source_type === 'simulated' || m.source_type === 'file'
          ? 0
          : m.hardware_required
            ? 1
            : 2;
      return rank(a) - rank(b) || a.label.localeCompare(b.label);
    });

  const manifest = () => manifests().find((m) => m.source_type === selected());

  const selectSource = (sourceType: string) => {
    setSelected(sourceType);
    setValues(defaultValues(sourceType));
    setRateText('');
    const m = (sources.latest ?? sources() ?? []).find(
      (s) => s.source_type === sourceType,
    );
    if (m && (sourceType === 'simulated' || sourceType === 'file')) {
      // Sensible defaults per source family.
      if (sourceType === 'simulated' && m.default_sample_rate) {
        setFreqText('14.205 MHz');
      }
    }
  };

  const badges = (sourceType: string) =>
    (hardware.latest ?? hardware() ?? []).filter((d) => d.driver === sourceType);

  const changeValue = (key: string, value: string | boolean) =>
    setValues((prev) => ({ ...prev, [key]: value }));

  const spawn = () => {
    const freq = parseFreqHz(freqText()) ?? 14_205_000;
    const req: Parameters<typeof api.spawnReceiver>[0] = {
      source_type: selected(),
      source_kwargs: collectKwargs(selected(), values()),
      center_freq: Math.round(freq),
      mode: mode(),
    };
    const rate = parseFreqHz(rateText());
    if (rate !== null && rate > 0) req.sample_rate = Math.round(rate);
    else if (manifest()?.default_sample_rate)
      req.sample_rate = manifest()!.default_sample_rate;
    void props.onSpawn(req);
  };

  return (
    <div class="flex h-full min-h-0 gap-3">
      {/* Source list */}
      <div class="w-64 shrink-0 overflow-auto rounded border border-base-800 bg-base-950">
        <For each={manifests()}>
          {(m) => (
            <button
              type="button"
              class={`flex w-full flex-col gap-0.5 border-b border-base-800 px-3 py-2 text-left ${
                selected() === m.source_type
                  ? 'bg-cyan-450/10'
                  : 'hover:bg-base-900'
              }`}
              onClick={() => selectSource(m.source_type)}
            >
              <span class="flex items-center gap-1.5">
                <Show when={badges(m.source_type).length > 0}>
                  <span
                    class="h-1.5 w-1.5 rounded-full bg-emerald-400"
                    title={`${badges(m.source_type).length} detected: ${badges(m.source_type)
                      .map((d) => d.label)
                      .join(', ')}`}
                  />
                </Show>
                <span
                  class={`font-mono text-xs ${
                    selected() === m.source_type ? 'text-cyan-450' : 'text-base-100'
                  }`}
                >
                  {m.label}
                </span>
              </span>
              <span class="font-mono text-[10px] text-base-500">
                {m.hardware_required ? 'hardware' : 'remote / offline'} ·{' '}
                {formatHz(m.default_sample_rate)}
              </span>
            </button>
          )}
        </For>
        <Show when={sources.loading}>
          <div class="px-3 py-4 font-mono text-[11px] text-base-500">loading…</div>
        </Show>
      </div>

      {/* Config form */}
      <div class="flex min-w-0 flex-1 flex-col gap-3 overflow-auto">
        <Show when={manifest()} keyed>
          {(m) => (
            <>
              <div class="font-mono text-[11px] text-base-400">
                {SOURCE_NOTES[m.source_type] ?? m.description}
              </div>

              <div class="rounded border border-base-800 bg-base-950 p-3">
                <div class="mb-2 font-mono text-[10px] uppercase tracking-widest text-base-500">
                  Source configuration
                </div>
                <SourceConfigForm
                  sourceType={m.source_type}
                  values={values()}
                  onChange={changeValue}
                />
              </div>

              <div class="rounded border border-base-800 bg-base-950 p-3">
                <div class="mb-2 font-mono text-[10px] uppercase tracking-widest text-base-500">
                  Tuning
                </div>
                <div class="grid grid-cols-2 gap-3">
                  <label class="flex items-center gap-2 font-mono text-[11px] text-base-300">
                    frequency
                    <input
                      type="text"
                      class={inputCls}
                      value={freqText()}
                      onInput={(e) => setFreqText((e.target as HTMLInputElement).value)}
                    />
                  </label>
                  <label class="flex items-center gap-2 font-mono text-[11px] text-base-300">
                    mode
                    <select
                      class={inputCls}
                      value={mode()}
                      onChange={(e) => setMode((e.target as HTMLSelectElement).value as ReceiverMode)}
                    >
                      <For each={MODES}>{(mo) => <option value={mo}>{mo}</option>}</For>
                    </select>
                  </label>
                </div>
                <button
                  type="button"
                  class="mt-2 font-mono text-[10px] text-base-500 underline hover:text-base-300"
                  onClick={() => setShowAdvanced((v) => !v)}
                >
                  {showAdvanced() ? '▾' : '▸'} advanced
                </button>
                <Show when={showAdvanced()}>
                  <label class="mt-2 flex items-center gap-2 font-mono text-[11px] text-base-300">
                    sample rate override
                    <input
                      type="text"
                      class={inputCls}
                      placeholder={`default ${formatHz(m.default_sample_rate)}`}
                      value={rateText()}
                      onInput={(e) => setRateText((e.target as HTMLInputElement).value)}
                    />
                  </label>
                </Show>
              </div>

              <div class="flex items-center gap-3">
                <button
                  type="button"
                  class="rounded bg-cyan-450/20 px-3 py-1.5 font-mono text-xs font-semibold text-cyan-450 hover:bg-cyan-450/30 disabled:opacity-50"
                  disabled={props.connecting}
                  onClick={spawn}
                >
                  {props.connecting ? 'spawning…' : `spawn ${m.label}`}
                </button>
                <span class="font-mono text-[10px] text-base-500">
                  {badges(m.source_type).length > 0
                    ? `✓ ${badges(m.source_type).map((d) => d.label).join(', ')} detected`
                    : m.hardware_required
                      ? 'no device detected — spawn will probe anyway'
                      : 'no hardware needed'}
                </span>
              </div>
            </>
          )}
        </Show>
      </div>
    </div>
  );
}

// ---- VFO panel -----------------------------------------------------------------

/** Decimation factors that yield sensible channel widths (5 kHz – 500 kHz)
 *  from typical SDR rates. Only integer divisors of the parent's rate are
 *  offered — the VFO DDC requires integer decimation (ADR-005). */
const VFO_DECIMATIONS = [2, 4, 5, 8, 10, 16, 20, 25, 32, 40, 50, 80, 100, 125, 200, 250];

function spanOptions(parentRate: number): { label: string; hz: number }[] {
  const opts: { label: string; hz: number }[] = [];
  for (const n of VFO_DECIMATIONS) {
    const hz = parentRate / n;
    if (hz >= 5_000 && hz <= 500_000 && Number.isInteger(hz)) {
      opts.push({ label: `${(hz / 1_000).toFixed(hz % 1_000 ? 1 : 0)} kHz`, hz });
    }
  }
  return opts.sort((a, b) => b.hz - a.hz);
}

function VfoPanel(props: {
  onSpawn: (req: Parameters<typeof api.spawnReceiver>[0]) => Promise<void>;
  connecting: boolean;
}): JSX.Element {
  const [receivers] = createResource(() => api.listReceivers());
  const [parentId, setParentId] = createSignal('');
  const [freqText, setFreqText] = createSignal('');
  const [span, setSpan] = createSignal(0); // 0 → pick the widest sane default

  const rows = () => receivers.latest ?? receivers() ?? [];
  const parent = () => rows().find((r) => r.receiver_id === parentId());

  // Default the picker to the first non-remote receiver when loaded.
  const parentOptions = () =>
    rows().filter((r) => !['openwebrx_remote', 'kiwi'].includes(r.source.type));

  // Select a parent once receivers load (and whenever the current pick
  // disappears — e.g. the parent receiver was destroyed meanwhile).
  createEffect(() => {
    const options = parentOptions();
    if (options.length === 0) return;
    if (!options.some((r) => r.receiver_id === parentId())) {
      setParentId(options[0].receiver_id);
      if (!freqText()) setFreqText(formatHz(options[0].center_freq));
      // Reset the span to a valid divisor of the new parent.
      setSpan(0);
    }
  });

  // Span choices derive from the parent's rate (integer divisors only).
  const spans = () => (parent() ? spanOptions(parent()!.sample_rate) : []);
  // Resolve the effective span: the explicit pick, or the widest option
  // that stays ≤ ~250 kHz (roomy SSB/CW+data default).
  const effectiveSpan = () => {
    if (span() > 0 && spans().some((o) => o.hz === span())) return span();
    const candidates = spans().filter((o) => o.hz <= 250_000);
    return candidates[0]?.hz ?? spans()[0]?.hz ?? 0;
  };

  const offset = () => {
    const p = parent();
    if (!p) return null;
    const f = parseFreqHz(freqText());
    return f === null ? null : Math.round(f - p.center_freq);
  };

  const valid = () => {
    const p = parent();
    const o = offset();
    if (!p || o === null || effectiveSpan() === 0) return false;
    return Math.abs(o) + effectiveSpan() / 2 <= p.sample_rate / 2 + 1;
  };

  const spawn = () => {
    const p = parent();
    const o = offset();
    if (!p || o === null) return;
    void props.onSpawn({
      source_type: 'vfo',
      source_kwargs: { parent_receiver_id: p.receiver_id },
      center_freq: p.center_freq + o,
      sample_rate: effectiveSpan(),
      mode: 'USB',
    });
  };

  return (
    <div class="flex max-w-xl flex-col gap-3">
      <p class="font-mono text-[11px] leading-relaxed text-base-400">
        A VFO sub-receiver demodulates a slice of a wideband parent's IQ —
        multiple simultaneous VFOs share one physical SDR (ADR-005). The
        parent keeps streaming; children tap its hub.
      </p>
      <Show
        when={!receivers.loading && parentOptions().length > 0}
        fallback={
          <div class="rounded border border-amber-450/40 bg-amber-450/10 p-3 font-mono text-[11px] text-amber-450">
            No local wideband receivers running. Spawn an RTL-SDR / Airspy /
            file / simulated receiver first, then add VFOs on top of it.
          </div>
        }
      >
        <label class="flex items-center gap-2 font-mono text-[11px] text-base-300">
          parent
          <select
            class={inputCls}
            value={parentId()}
            onChange={(e) => {
              setParentId((e.target as HTMLSelectElement).value);
              const p = rows().find((r) => r.receiver_id === (e.target as HTMLSelectElement).value);
              if (p) setFreqText(formatHz(p.center_freq));
              setSpan(0); // re-derive a valid span for the new parent
            }}
          >
            <For each={parentOptions()}>
              {(r) => (
                <option value={r.receiver_id}>
                  {r.receiver_id.slice(0, 14)} · {r.source.label} ·{' '}
                  {formatHz(r.center_freq)} @ {formatHz(r.sample_rate)}
                </option>
              )}
            </For>
          </select>
        </label>
        <div class="grid grid-cols-2 gap-3">
          <label class="flex items-center gap-2 font-mono text-[11px] text-base-300">
            frequency
            <input
              type="text"
              class={inputCls}
              value={freqText()}
              onInput={(e) => setFreqText((e.target as HTMLInputElement).value)}
            />
          </label>
          <label class="flex items-center gap-2 font-mono text-[11px] text-base-300">
            span
            <select
              class={inputCls}
              value={String(effectiveSpan())}
              onChange={(e) => setSpan(Number((e.target as HTMLSelectElement).value))}
            >
              <For each={spans()}>
                {(o) => <option value={String(o.hz)}>{o.label}</option>}
              </For>
            </select>
          </label>
        </div>
        <Show when={parent()} keyed>
          {(p) => (
            <div class="rounded border border-base-800 bg-base-950 p-2 font-mono text-[11px] text-base-300">
              offset vs parent:{' '}
              <Show when={offset() !== null} fallback={<span class="text-rose-450">parse error</span>}>
                <span class={valid() ? 'text-cyan-450' : 'text-rose-450'}>
                  {offset()! >= 0 ? '+' : ''}
                  {formatHz(Math.abs(offset()!))} {offset()! < 0 ? 'below' : offset()! > 0 ? 'above' : 'at'}{' '}
                  {formatHz(p.center_freq)}
                </span>
              </Show>
              <Show when={!valid()}>
                <span class="mt-1 block text-rose-450">
                  slice exceeds ±{formatHz(p.sample_rate / 2)} — pick a frequency
                  inside the parent's band
                </span>
              </Show>
            </div>
          )}
        </Show>
        <div class="flex items-center gap-3">
          <button
            type="button"
            class="rounded bg-cyan-450/20 px-3 py-1.5 font-mono text-xs font-semibold text-cyan-450 hover:bg-cyan-450/30 disabled:opacity-50"
            disabled={props.connecting || !valid()}
            onClick={spawn}
          >
            {props.connecting ? 'spawning…' : 'spawn VFO'}
          </button>
          <span class="font-mono text-[10px] text-base-500">
            demodulates locally via pycsdr (Shift → decimate → demod)
          </span>
        </div>
      </Show>
    </div>
  );
}
