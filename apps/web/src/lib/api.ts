/** Typed REST client for the OpenWebRX+ backend (apps/server api/rest.py).
 *
 *  Everything here is a thin, promise-based wrapper around the REST API.
 *  No streams — those live on the WebSocket (see workers/sdr.shared-worker.ts).
 *  Components import from this module instead of hand-rolling `fetch` calls
 *  so the wire shapes stay in one place.
 *
 *  Wire shapes mirror:
 *    - GET  /api/sources                  → SourceManifest[]
 *    - GET  /api/hardware                 → HardwareDevice[]
 *    - GET  /api/fixtures                 → IqFixture[]
 *    - GET  /api/decoders                 → DecoderManifest[]
 *    - GET  /api/directory/{kiwi|receiverbook} → DirectoryResponse
 *    - GET  /api/receivers                → ReceiverInfo[]
 *    - POST /api/receivers                → CreateReceiverResponse
 *    - DELETE /api/receivers/{id}         → 204
 *    - GET  /api/receivers/{id}/decoders  → DecoderStatus[]
 *    - POST /api/receivers/{id}/decoders  → { name, attached }
 *    - DELETE /api/receivers/{id}/decoders/{name} → 204
 */

// ---- Types -----------------------------------------------------------------

/** One entry of GET /api/sources — the server's SourceManifest.to_dict(). */
export interface SourceManifest {
  source_type: string;
  label: string;
  sdk: string;
  hardware_required: boolean;
  default_sample_rate: number;
  sample_rate_range: [number, number];
  gain_range: [number, number] | null;
  supports_bias_tee: boolean;
  supports_agc: boolean;
  description: string;
}

/** One entry of GET /api/hardware — a locally detected SDR. */
export interface HardwareDevice {
  driver: string;
  label: string;
  serial: string | null;
  transport: string;
  endpoint: string | null;
  index: number;
  details: Record<string, unknown>;
}

/** One entry of GET /api/directory/{provider} — a public remote receiver. */
export interface RemoteReceiver {
  directory: string;
  source_type: string;
  id: string;
  name: string;
  url: string;
  lat: number | null;
  lon: number | null;
  users: string | null;
  online: boolean;
  extra?: Record<string, unknown>;
}

export interface DirectoryResponse {
  directory: string;
  count: number;
  receivers: RemoteReceiver[];
}

/** One baked IQ fixture from GET /api/fixtures — replayable via the file source. */
export interface IqFixture {
  name: string;
  path: string;
  sample_rate: number | null;
  center_freq: number | null;
  description: string | null;
  label: string | null;
}

/** One decoder plugin from GET /api/decoders (ADR-003). */
export interface DecoderManifest {
  name: string;
  version: string;
  label: string;
  tap_point: 'rf_band' | 'audio_band';
  description: string;
  required_sample_rate: number | null;
  events: string[];
}

/** Live state of one attached decoder (GET /api/receivers/{id}/decoders). */
export interface DecoderStatus {
  name: string;
  [key: string]: unknown;
}

export interface ReceiverInfo {
  receiver_id: string;
  center_freq: number;
  sample_rate: number;
  mode: string;
  source: { type: string; label: string; sampleRate: number };
}

/** Body for POST /api/receivers. Mirrors the server's CreateReceiverRequest. */
export interface SpawnReceiverRequest {
  center_freq?: number;
  sample_rate?: number;
  mode?: string;
  source_type?: string;
  source_kwargs?: Record<string, unknown>;
}

export interface SpawnReceiverResponse {
  receiver_id: string;
  center_freq: number;
  sample_rate: number;
  mode: string;
}

// ---- Client ----------------------------------------------------------------

/** Normalized REST failure — carries the server's detail message when present. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(`API ${status}: ${detail}`);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body && typeof body.detail === 'string') detail = body.detail;
    } catch {
      /* non-JSON error body — keep statusText */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  listSources: () => request<SourceManifest[]>('/api/sources'),

  listHardware: () => request<HardwareDevice[]>('/api/hardware'),

  /** Baked IQ fixtures — one-click replay targets for the file source. */
  listFixtures: () => request<IqFixture[]>('/api/fixtures'),

  /** Available decoder plugins (ADR-003). */
  listDecoders: () => request<DecoderManifest[]>('/api/decoders'),

  /** Decoders currently attached to a receiver. */
  listReceiverDecoders: (receiverId: string) =>
    request<DecoderStatus[]>(`/api/receivers/${encodeURIComponent(receiverId)}/decoders`),

  /** Attach a decoder to a running receiver (409 if already attached). */
  attachDecoder: (receiverId: string, name: string) =>
    request<{ name: string; attached: boolean }>(
      `/api/receivers/${encodeURIComponent(receiverId)}/decoders`,
      { method: 'POST', body: JSON.stringify({ name }) },
    ),

  /** Detach a decoder from a receiver. */
  detachDecoder: (receiverId: string, name: string) =>
    request<void>(
      `/api/receivers/${encodeURIComponent(receiverId)}/decoders/${encodeURIComponent(name)}`,
      { method: 'DELETE' },
    ),

  /** Fetch a remote receiver directory. 503 (offline + no cache) throws. */
  fetchDirectory: (provider: 'kiwi' | 'receiverbook', refresh = false) =>
    request<DirectoryResponse>(
      `/api/directory/${provider}${refresh ? '?refresh=1' : ''}`,
    ),

  listReceivers: () => request<ReceiverInfo[]>('/api/receivers'),

  spawnReceiver: (req: SpawnReceiverRequest) =>
    request<SpawnReceiverResponse>('/api/receivers', {
      method: 'POST',
      body: JSON.stringify(req),
    }),

  destroyReceiver: (receiverId: string) =>
    request<void>(`/api/receivers/${encodeURIComponent(receiverId)}`, {
      method: 'DELETE',
    }),
};

// ---- Deep-link parsing (client side of ADR-006) ----------------------------

export interface ParsedRemoteUrl {
  /** 'openwebrx_remote' | 'kiwi' — which source plugin to spawn. */
  sourceType: 'openwebrx_remote' | 'kiwi';
  /** source_kwargs for POST /api/receivers. */
  sourceKwargs: Record<string, unknown>;
  /** Tuned frequency from the #freq= hash, if present. */
  freqHz: number | null;
  /** Modulation from #mod=, if present (server normalizes). */
  mod: string | null;
}

/**
 * Parse a pasted remote-receiver URL into spawn parameters.
 *
 * OpenWebRX(+) deep links look like:
 *   http://boomerthedog.com:8073/#freq=3570000,mod=lsb,sql=-150
 * KiwiSDR URLs look like:
 *   http://rx.example.kiwisdr.com:8073/  (no deep-link hash convention)
 *
 * We can't distinguish the two by URL shape alone (both default to :8073),
 * so the caller passes an explicit type guess; the quick-connect box exposes
 * both buttons and defaults to openwebrx_remote (the more common case and
 * the one with deep-link support).
 */
export function parseRemoteUrl(
  raw: string,
  sourceType: 'openwebrx_remote' | 'kiwi' = 'openwebrx_remote',
): ParsedRemoteUrl | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;

  // Accept bare host[:port] too — normalize to a parseable URL.
  const withScheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(trimmed)
    ? trimmed
    : `http://${trimmed}`;

  let url: URL;
  try {
    url = new URL(withScheme);
  } catch {
    return null;
  }
  if (!url.hostname) return null;

  // Slice the "#a=1,b=2" hash into a key/value map (OpenWebRX format).
  const hashParams = new Map<string, string>();
  if (url.hash.startsWith('#') && url.hash.length > 1) {
    for (const part of url.hash.slice(1).split(',')) {
      const eq = part.indexOf('=');
      if (eq > 0) hashParams.set(part.slice(0, eq), part.slice(eq + 1));
    }
  }

  const port = url.port ? Number(url.port) : url.protocol === 'https:' ? 443 : 80;

  if (sourceType === 'kiwi') {
    // KiwiSdrSource takes host/port (no deep-link hash convention).
    return {
      sourceType,
      sourceKwargs: { host: url.hostname, port: port === 80 ? 8073 : port },
      freqHz: null,
      mod: null,
    };
  }

  // openwebrx_remote accepts the full deep-link URL verbatim — the server's
  // parse_openwebrx_url() extracts host/port/freq/mod/sql itself. Passing the
  // whole URL keeps boomerthedog-style links working end to end.
  const freqRaw = hashParams.get('freq');
  const freqHz = freqRaw !== undefined && /^\d+(\.\d+)?$/.test(freqRaw) ? Math.round(Number(freqRaw)) : null;
  return {
    sourceType,
    sourceKwargs: { url: trimmed },
    freqHz,
    mod: hashParams.get('mod') ?? null,
  };
}

// ---- Formatting helpers (shared by pickers/browser) ------------------------

export function formatHz(hz: number): string {
  if (hz >= 1e9) return `${(hz / 1e9).toFixed(3)} GHz`;
  if (hz >= 1e6) return `${(hz / 1e6).toFixed(4)} MHz`;
  if (hz >= 1e3) return `${(hz / 1e3).toFixed(1)} kHz`;
  return `${hz} Hz`;
}
