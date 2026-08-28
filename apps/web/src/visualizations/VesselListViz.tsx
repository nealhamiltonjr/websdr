/** VesselListViz — the AIS vessel table (slice-6.4 — ADR-003 family #3).
 *
 *  Subscribes to the receiver's decoderStream and renders the live vessel
 *  table, plus a self-contained attach/detach control for the bundled AIS
 *  decoder. The in-process "ais" plugin is always available — no external
 *  binary needed. The receiver needs 48 kS/s IQ (the AIS fixture rate, 5
 *  samples/bit at 9600 baud) — typically via a VFO tap on 162 MHz.
 */

import { createSignal, onCleanup, onMount, Show, For } from 'solid-js';
import { AIS_DECODERS } from '@openwebrx-plus/shared-types';
import { receiverRegistry } from '../sessions/ReceiverSession';
import { api, ApiError } from '../lib/api';
import { registerViz, type VizProps } from './registry';

/** AIS decoder name — only the in-process plugin exists in v1. */
const DEFAULT_DECODER = 'ais';
const aisDecoderNames: readonly string[] = AIS_DECODERS;

/** In-memory vessel table, mirroring aircraftModel.ts pattern. */
interface VesselRow {
  mmsi: string;
  type: number;
  vessel_name: string | null;
  callsign: string | null;
  imo: number | null;
  ship_type: number | null;
  speed_kn: number | null;
  longitude: number | null;
  latitude: number | null;
  course_deg: number | null;
  heading_deg: number | null;
  nav_status: number | null;
  destination: string | null;
  frames: number;
  last_seen: number;
  rssi_dbfs: number;
}

interface VesselFeedState {
  vessels: VesselRow[];
  frames: number;
  crcFailures: number;
}

const initialVesselState = (): VesselFeedState => ({
  vessels: [],
  frames: 0,
  crcFailures: 0,
});

/** Apply one AIS decoder event to the vessel state (pure). */
function applyAisEvent(state: VesselFeedState, envelope: { decoder: string; event: { kind: string; [k: string]: unknown } }): VesselFeedState {
  if (!aisDecoderNames.includes(envelope.decoder)) return state;
  const ev = envelope.event;
  if (ev.kind === 'frame') {
    const row = (ev as unknown) as {
      mmsi: string;
      type: number;
      vessel_name?: string | null;
      callsign?: string | null;
      imo?: number | null;
      ship_type?: number | null;
      speed_kn?: number | null;
      longitude?: number | null;
      latitude?: number | null;
      course_deg?: number | null;
      heading_deg?: number | null;
      nav_status?: number | null;
      destination?: string | null;
      ts: number;
      rssi_dbfs: number;
    };
    const existing = state.vessels.find((v) => v.mmsi === row.mmsi);
    let vessels: VesselRow[];
    if (existing) {
      // Update only the fields this message carried; the latest message type
      // stamps the row so the UI shows what was last received.
      const updated: VesselRow = {
        ...existing,
        type: row.type,
        vessel_name: row.vessel_name ?? existing.vessel_name,
        callsign: row.callsign ?? existing.callsign,
        imo: row.imo ?? existing.imo,
        ship_type: row.ship_type ?? existing.ship_type,
        speed_kn: row.speed_kn ?? existing.speed_kn,
        longitude: row.longitude ?? existing.longitude,
        latitude: row.latitude ?? existing.latitude,
        course_deg: row.course_deg ?? existing.course_deg,
        heading_deg: row.heading_deg ?? existing.heading_deg,
        nav_status: row.nav_status ?? existing.nav_status,
        destination: row.destination ?? existing.destination,
        frames: existing.frames + 1,
        last_seen: row.ts,
        rssi_dbfs: row.rssi_dbfs,
      };
      vessels = state.vessels.map((v) => (v.mmsi === row.mmsi ? updated : v));
    } else {
      vessels = [
        ...state.vessels,
        {
          mmsi: row.mmsi,
          type: row.type,
          vessel_name: row.vessel_name ?? null,
          callsign: row.callsign ?? null,
          imo: row.imo ?? null,
          ship_type: row.ship_type ?? null,
          speed_kn: row.speed_kn ?? null,
          longitude: row.longitude ?? null,
          latitude: row.latitude ?? null,
          course_deg: row.course_deg ?? null,
          heading_deg: row.heading_deg ?? null,
          nav_status: row.nav_status ?? null,
          destination: row.destination ?? null,
          frames: 1,
          last_seen: row.ts,
          rssi_dbfs: row.rssi_dbfs,
        },
      ];
    }
    return { ...state, vessels, frames: state.frames + 1 };
  }
  if (ev.kind === 'vessel') {
    // Snapshot — replace the table wholesale.
    const snapshot = (ev as unknown) as {
      ts: number;
      vessels: VesselRow[];
    };
    return { ...state, vessels: snapshot.vessels };
  }
  return state;
}

const formatAge = (lastSeen: number, now: number): string => {
  const s = Math.max(0, Math.floor(now - lastSeen));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h`;
};

const formatCoord = (v: number | null, suffix: string): string =>
  v === null ? '---' : `${v.toFixed(4)}${suffix}`;

const SHIP_TYPES: Record<number, string> = {
  30: 'Tug',
  31: 'Trawler',
  35: 'Pleasure',
  36: 'Sail',
  37: 'Yacht',
  40: 'High-speed craft',
  50: 'Pilot',
  51: 'SAR',
  52: 'Tug',
  53: 'Port tender',
  54: 'Anti-pollution',
  55: 'Spare',
  58: 'Medical',
  60: 'Passenger',
  70: 'Cargo',
  80: 'Tanker',
  90: 'Other',
};

const formatShipType = (t: number | null): string => {
  if (t === null) return '---';
  return SHIP_TYPES[t] ?? `Type ${t}`;
};

const NAV_STATUSES: Record<number, string> = {
  0: 'Under way (engine)',
  1: 'At anchor',
  2: 'Not under command',
  3: 'Restricted maneuverability',
  4: 'Constrained by draught',
  5: 'Moored',
  6: 'Aground',
  7: 'Engaged in fishing',
  8: 'Under way (sailing)',
  15: 'Undefined',
};

const formatNavStatus = (s: number | null): string => {
  if (s === null) return '---';
  return NAV_STATUSES[s] ?? `Status ${s}`;
};

function VesselListViz(props: VizProps): import('solid-js').JSX.Element {
  const [state, setState] = createSignal<VesselFeedState>(initialVesselState());
  const [attachedName, setAttachedName] = createSignal<string | null>(null);
  const [busy, setBusy] = createSignal(false);
  const [error, setError] = createSignal<string | null>(null);
  const [now, setNow] = createSignal(Date.now() / 1000);

  const refreshStatus = async () => {
    try {
      const decoders = await api.listReceiverDecoders(props.receiverId);
      setAttachedName(
        decoders.find((d) => aisDecoderNames.includes(d.name))?.name ?? null,
      );
      setError(null);
    } catch (e) {
      // Ignore — probably no receiver yet.
      if (e instanceof ApiError) console.warn('[VesselListViz] status', e);
    }
  };

  const attach = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.attachDecoder(props.receiverId, DEFAULT_DECODER);
      setAttachedName(DEFAULT_DECODER);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const detach = async () => {
    setBusy(true);
    setError(null);
    try {
      const name = attachedName() ?? DEFAULT_DECODER;
      await api.detachDecoder(props.receiverId, name);
      setAttachedName(null);
      setState(initialVesselState());
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  onMount(() => {
    const session = receiverRegistry.getOrCreate(props.receiverId);
    const unsub = session.decoderStream.subscribe((env) => {
      setState((s) => applyAisEvent(s, env as { decoder: string; event: { kind: string; [k: string]: unknown } }));
    });
    refreshStatus();
    const tick = setInterval(() => setNow(Date.now() / 1000), 1000);
    onCleanup(() => {
      unsub();
      clearInterval(tick);
    });
  });

  const vessels = () => [...state().vessels].sort((a, b) => b.last_seen - a.last_seen);

  return (
    <div class="flex h-full w-full flex-col bg-base-900 text-base-100">
      <header class="flex h-8 items-center justify-between border-b border-base-800 px-2">
        <span class="font-mono text-xs text-cyan-450">AIS VESSELS</span>
        <span class="font-mono text-[10px] text-base-400">
          {state().frames} frames · {state().vessels.length} vessels
        </span>
      </header>
      <Show when={attachedName()} fallback={
        <div class="flex h-full flex-col items-center justify-center gap-2 p-4 text-center">
          <div class="font-mono text-sm text-base-300">
            No AIS decoder attached
          </div>
          <div class="font-mono text-[10px] text-base-400">
            48 kS/s IQ · 162 MHz · 5 samples/bit
          </div>
          <button
            class="mt-2 rounded bg-cyan-600 px-3 py-1 font-mono text-xs text-white hover:bg-cyan-500 disabled:opacity-50"
            disabled={busy()}
            onClick={attach}
          >
            {busy() ? 'Attaching…' : 'Attach AIS decoder'}
          </button>
          <Show when={error()}>
            <div class="mt-1 font-mono text-[10px] text-rose-400">{error()}</div>
          </Show>
        </div>
      }>
        <div class="flex-1 overflow-auto">
          <table class="w-full border-collapse font-mono text-[10px]">
            <thead class="sticky top-0 bg-base-800 text-base-300">
              <tr>
                <th class="border-b border-base-700 px-1.5 py-1 text-left">MMSI</th>
                <th class="border-b border-base-700 px-1.5 py-1 text-left">Name</th>
                <th class="border-b border-base-700 px-1.5 py-1 text-left">Type</th>
                <th class="border-b border-base-700 px-1.5 py-1 text-right">Lat</th>
                <th class="border-b border-base-700 px-1.5 py-1 text-right">Lon</th>
                <th class="border-b border-base-700 px-1.5 py-1 text-right">Spd</th>
                <th class="border-b border-base-700 px-1.5 py-1 text-right">Crs</th>
                <th class="border-b border-base-700 px-1.5 py-1 text-left">Status</th>
                <th class="border-b border-base-700 px-1.5 py-1 text-right">Age</th>
              </tr>
            </thead>
            <tbody>
              <For each={vessels()}>
                {(v) => (
                  <tr class="hover:bg-base-800">
                    <td class="px-1.5 py-0.5 text-cyan-400">{v.mmsi}</td>
                    <td class="px-1.5 py-0.5">{v.vessel_name ?? '---'}</td>
                    <td class="px-1.5 py-0.5 text-base-300">{formatShipType(v.ship_type)}</td>
                    <td class="px-1.5 py-0.5 text-right">{formatCoord(v.latitude, 'N')}</td>
                    <td class="px-1.5 py-0.5 text-right">{formatCoord(v.longitude, 'E')}</td>
                    <td class="px-1.5 py-0.5 text-right">{v.speed_kn !== null ? `${v.speed_kn.toFixed(1)}` : '---'}</td>
                    <td class="px-1.5 py-0.5 text-right">{v.course_deg !== null ? `${v.course_deg.toFixed(0)}°` : '---'}</td>
                    <td class="px-1.5 py-0.5 text-base-300">{formatNavStatus(v.nav_status)}</td>
                    <td class="px-1.5 py-0.5 text-right text-base-400">{formatAge(v.last_seen, now())}</td>
                  </tr>
                )}
              </For>
            </tbody>
          </table>
        </div>
        <footer class="flex h-7 items-center justify-between border-t border-base-800 px-2">
          <span class="font-mono text-[10px] text-base-400">
            decoder: <span class="text-cyan-450">{attachedName()}</span>
          </span>
          <button
            class="rounded bg-rose-600/80 px-2 py-0.5 font-mono text-[10px] text-white hover:bg-rose-500 disabled:opacity-50"
            disabled={busy()}
            onClick={detach}
          >
            Detach
          </button>
        </footer>
      </Show>
    </div>
  );
}

registerViz({
  type: 'vessel-list',
  displayName: 'AIS Vessels',
  icon: 'ship',
  defaultWidth: 480,
  defaultHeight: 320,
  live: true,
  component: VesselListViz,
});

export default VesselListViz;
