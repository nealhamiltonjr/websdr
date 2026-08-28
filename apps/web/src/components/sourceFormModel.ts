/** Declarative per-source config forms for the AddReceiverModal.
 *
 *  Each spawnable source type declares a list of fields. The modal renders
 *  them generically and collects values into `source_kwargs` for
 *  POST /api/receivers. Field keys and defaults mirror the server-side
 *  dataclass constructors exactly (apps/server openwebrx_plus/sources/*).
 *
 *  Sources not listed here (vfo — spawned from its own panel; future plugin
 *  entry-points) fall back to a free-form JSON kwargs editor.
 */

// ---- Field model ------------------------------------------------------------

export interface FieldSpec {
  key: string;
  label: string;
  kind: 'text' | 'number' | 'select' | 'checkbox' | 'fixture';
  /** Placeholder for text/number inputs. */
  placeholder?: string | number;
  /** Options for selects: [value, label] pairs. */
  options?: [string, string][];
  /** Initial value (numbers/strings) or checked state (checkboxes). */
  default?: string | number | boolean;
  /** One-line help text. */
  hint?: string;
}

export interface SourceFormSpec {
  fields: FieldSpec[];
}

/** Text shown above the form when a source is selected. */
export const SOURCE_NOTES: Record<string, string> = {
  simulated:
    'Synthetic multi-signal IQ — zero config, always works. Best for UI work.',
  file: 'Replay a cf32 IQ capture in real time. Ships with baked fixtures.',
  rtl_sdr:
    'RTL-SDR Blog V4 supported. HF via direct sampling (Q-branch = setting 2).',
  rtl_tcp: 'A remote rtl_tcp server — raw IQ over TCP, full local DSP.',
  airspy: 'Airspy R2 / Mini. Three gain stages or composite gain modes.',
  sdrplay: 'SDRplay RSP family via the API v3 stream callback.',
  soapy: 'Any SDR with a SoapySDR module. The universal escape hatch.',
  kiwi: 'Public KiwiSDR (0–30 MHz, channelized IQ at the Kiwi sound rate).',
  spyserver:
    'Any SpyServer receiver (Airspy HF+/Discovery/R2 or RTL-SDR server-side) — raw float32 IQ over TCP.',
  openwebrx_remote:
    'Any public OpenWebRX(+) receiver — remote waterfall + audio, tuning forwarded.',
};

export const SOURCE_FORMS: Record<string, SourceFormSpec> = {
  simulated: {
    fields: [
      {
        key: 'signal_set',
        label: 'Signal set',
        kind: 'select',
        default: 'default',
        options: [
          ['default', 'Default mix'],
          ['am_band', 'AM broadcast band'],
          ['ham_band', 'Ham band'],
          ['ads_b', 'ADS-B at 1090 MHz'],
          ['ft8_dry_run', 'FT8 dry run'],
        ],
      },
      {
        key: 'noise_floor',
        label: 'Noise floor',
        kind: 'number',
        default: 0.05,
        hint: 'RMS amplitude 0–1',
      },
    ],
  },
  file: {
    fields: [
      {
        key: '_fixture',
        label: 'Fixture',
        kind: 'fixture',
        hint: 'Baked demo captures (GET /api/fixtures) — fills the path below',
      },
      {
        key: 'file_path',
        label: 'IQ file',
        kind: 'text',
        placeholder: '/…/fixtures/iq/hf_20m_evening.cf32',
        hint: 'cf32 / cs16 / cu8 file; a sibling .meta supplies freq/rate',
      },
      { key: 'loop', label: 'Loop', kind: 'checkbox', default: true },
    ],
  },
  rtl_sdr: {
    fields: [
      { key: 'device_index', label: 'Device index', kind: 'number', default: 0 },
      {
        key: 'transport',
        label: 'Transport',
        kind: 'select',
        default: 'auto',
        options: [
          ['auto', 'Auto-probe (usb → tcp → subprocess)'],
          ['usb', 'USB (librtlsdr)'],
          ['tcp', 'rtl_tcp'],
          ['subprocess', 'rtl_sdr CLI'],
        ],
      },
      {
        key: 'host',
        label: 'Host',
        kind: 'text',
        placeholder: '127.0.0.1',
        hint: 'rtl_tcp endpoint (tcp/auto transport)',
      },
      { key: 'port', label: 'Port', kind: 'number', placeholder: 1234 },
      { key: 'ppm', label: 'PPM', kind: 'number', placeholder: 0 },
      {
        key: 'direct_sampling',
        label: 'Direct sampling',
        kind: 'select',
        default: '0',
        options: [
          ['0', 'Off (default)'],
          ['1', 'I branch'],
          ['2', 'Q branch (V4 HF 0.5–28.8 MHz)'],
        ],
      },
      { key: 'bias_tee', label: 'Bias tee', kind: 'checkbox', default: false },
    ],
  },
  rtl_tcp: {
    fields: [
      {
        key: 'host',
        label: 'Host',
        kind: 'text',
        placeholder: '192.168.1.20',
      },
      { key: 'port', label: 'Port', kind: 'number', default: 1234 },
      { key: 'ppm', label: 'PPM', kind: 'number', placeholder: 0 },
    ],
  },
  airspy: {
    fields: [
      {
        key: 'serial_number',
        label: 'Serial',
        kind: 'text',
        placeholder: 'auto (first device)',
      },
      {
        key: 'gain_mode',
        label: 'Gain mode',
        kind: 'select',
        default: 'linearity',
        options: [
          ['linearity', 'Linearity'],
          ['sensitivity', 'Sensitivity'],
          ['manual', 'Manual stages'],
        ],
      },
      { key: 'linearity_gain', label: 'Linearity gain', kind: 'number', default: 10 },
      { key: 'sensitivity_gain', label: 'Sensitivity gain', kind: 'number', default: 10 },
      { key: 'lna_gain', label: 'LNA (manual)', kind: 'number', default: 8 },
      { key: 'mixer_gain', label: 'Mixer (manual)', kind: 'number', default: 8 },
      { key: 'vga_gain', label: 'VGA (manual)', kind: 'number', default: 6 },
      { key: 'bias_tee', label: 'Bias tee', kind: 'checkbox', default: false },
    ],
  },
  sdrplay: {
    fields: [
      {
        key: 'serial',
        label: 'Serial',
        kind: 'text',
        placeholder: 'auto (first device)',
        hint: 'Substring match against device serials',
      },
      {
        key: 'antenna',
        label: 'Antenna',
        kind: 'select',
        default: 'a',
        options: [
          ['a', 'Port A'],
          ['b', 'Port B'],
          ['c', 'Port C (RSPduo master)'],
        ],
      },
      {
        key: 'grdb',
        label: 'Gain reduction',
        kind: 'number',
        placeholder: 'auto',
        hint: '20–59, higher = less gain',
      },
      { key: 'lna_state', label: 'LNA state', kind: 'number', default: 3 },
      { key: 'agc', label: 'AGC', kind: 'checkbox', default: false },
    ],
  },
  soapy: {
    fields: [
      {
        key: 'driver',
        label: 'Soapy driver',
        kind: 'text',
        placeholder: 'auto (first device)',
        hint: 'e.g. rtlsdr, airspy, hackrf, remote, plutosdr',
      },
      {
        key: 'remote',
        label: 'Remote endpoint',
        kind: 'text',
        placeholder: 'tcp://other-host:1234',
        hint: 'Only for driver=remote (SoapyRemote)',
      },
      {
        key: 'serial',
        label: 'Serial',
        kind: 'text',
        placeholder: 'auto',
        hint: 'Passed into soapy_args when set',
      },
      {
        key: 'antenna',
        label: 'Antenna',
        kind: 'text',
        placeholder: 'driver default',
      },
      { key: 'agc', label: 'AGC', kind: 'checkbox', default: false },
    ],
  },
  kiwi: {
    fields: [
      { key: 'host', label: 'Host', kind: 'text', placeholder: 'rx.example.kiwisdr.com' },
      { key: 'port', label: 'Port', kind: 'number', default: 8073 },
      { key: 'use_tls', label: 'TLS (wss://)', kind: 'checkbox', default: false },
      {
        key: 'user',
        label: 'Identity',
        kind: 'text',
        default: 'openwebrx_plus',
        hint: 'Volunteer-run receivers — identify honestly',
      },
      {
        key: 'iq_sample_rate',
        label: 'IQ rate',
        kind: 'number',
        default: 12000,
        hint: 'Kiwi sound rate (Hz)',
      },
    ],
  },
  spyserver: {
    fields: [
      {
        key: 'host',
        label: 'Host',
        kind: 'text',
        placeholder: 'sdr.example.com',
      },
      { key: 'port', label: 'Port', kind: 'number', default: 5555 },
      {
        key: 'sample_rate',
        label: 'IQ rate',
        kind: 'number',
        default: 768000,
        hint: 'Must be device max / 2^k — 768000 = HF+ full, 2400000 = RTL',
      },
      {
        key: 'gain',
        label: 'Gain (dB)',
        kind: 'number',
        placeholder: 'auto',
        hint: 'Applied at connect; also adjustable live',
      },
    ],
  },
  openwebrx_remote: {
    fields: [
      {
        key: 'url',
        label: 'Receiver URL',
        kind: 'text',
        placeholder: 'http://boomerthedog.com:8073/#freq=3570000,mod=lsb,sql=-150',
        hint: 'Deep-link hash (#freq/mod/sql) is honored',
      },
    ],
  },
};

/** Build source_kwargs from form values. Empty strings/None dropped,
 *  numbers parsed, checkboxes as booleans. Soapy driver/remote/serial
 *  fold into a single soapy_args dict (matching SoapySource's shape) —
 *  they fold from the RAW strings before numeric coercion so a serial
 *  like "00000001" survives as a string, not 1. */
export function collectKwargs(
  sourceType: string,
  values: Record<string, string | boolean>,
): Record<string, unknown> {
  const out: Record<string, unknown> = {};

  if (sourceType === 'soapy') {
    const soapyArgs: Record<string, unknown> = {};
    for (const key of ['driver', 'remote', 'serial'] as const) {
      const raw = values[key];
      if (typeof raw === 'string' && raw.trim() !== '') {
        soapyArgs[key] = raw.trim();
      }
    }
    if (Object.keys(soapyArgs).length > 0) out.soapy_args = soapyArgs;
  }

  for (const [key, raw] of Object.entries(values)) {
    if (key.startsWith('_')) {
      continue; // UI-only helper fields (e.g. the fixture picker)
    }
    if (sourceType === 'soapy' && ['driver', 'remote', 'serial'].includes(key)) {
      continue; // already folded into soapy_args above
    }
    if (typeof raw === 'boolean') {
      out[key] = raw;
      continue;
    }
    const v = raw.trim();
    if (v === '') continue;
    if (/^-?\d+(\.\d+)?$/.test(v)) out[key] = Number(v);
    else out[key] = v;
  }
  return out;
}

/** Default form values for a source type (pre-filled on selection).
 *  Booleans stay booleans (checkbox state); numbers become strings so the
 *  shared string-typed value model stays simple. */
export function defaultValues(sourceType: string): Record<string, string | boolean> {
  const spec = SOURCE_FORMS[sourceType];
  const values: Record<string, string | boolean> = {};
  if (!spec) return values;
  for (const f of spec.fields) {
    if (f.default !== undefined) {
      values[f.key] = typeof f.default === 'boolean' ? f.default : String(f.default);
    } else if (f.kind === 'checkbox') {
      values[f.key] = false;
    }
  }
  return values;
}
