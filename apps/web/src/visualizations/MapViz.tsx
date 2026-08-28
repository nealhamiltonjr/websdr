/** MapViz — shared base for the map-based visualizations (slice-8).
 *
 *  Wraps a MapLibre-GL map. Renders markers from a feed-state reducer that
 *  is supplied by the subclass (AircraftMapViz / VesselMapViz). The shared
 *  base handles:
 *    - lazy maplibre initialization (off the main render path)
 *    - resilient tile-source selection (DEMOTILES first, fallback banner if
 *      the user's network blocks the demotiles host — markers still render
 *      as overlays on a gray world)
 *    - marker diffing (add/update/remove on signal change)
 *    - zoom-to-fit button + auto-fit on first non-empty marker set
 *    - graceful attach/detach control surface
 *
 *  The subclass passes its `family` ('aircraft' | 'vessel') plus the
 *  decoder-family array (ADSB_DECODERS / AIS_DECODERS) and a default
 *  decoder name to attach on the one-click CTA.
 */

import {
  createEffect,
  createSignal,
  onCleanup,
  onMount,
  Show,
  type JSX,
} from 'solid-js';
import { receiverRegistry } from '../sessions/ReceiverSession';
import { api, ApiError } from '../lib/api';
import {
  applyAircraftDecoderEvent,
  applyVesselDecoderEvent,
  initialMapState,
  markerBounds,
  markerColor,
  markerOpacity,
  stalenessBucket,
  type MapFeedState,
  type MapMarker,
} from './mapModel';
import type { DecoderEventEnvelope } from '@openwebrx-plus/shared-types';

export interface MapVizConfig {
  family: 'aircraft' | 'vessel';
  /** Decoder names to consume events from (ADSB_DECODERS or AIS_DECODERS). */
  decoderNames: readonly string[];
  /** Default decoder to attach via REST on the one-click CTA. */
  defaultDecoder: string;
}

/** Try to dynamically import maplibre-gl. We import it lazily so a failure
 *  to load (e.g., the package being absent in tests) doesn't crash the
 *  component — we render an "offline map" fallback that still shows the
 *  marker list. */
async function loadMaplibre(): Promise<typeof import('maplibre-gl') | null> {
  try {
    return await import('maplibre-gl');
  } catch {
    return null;
  }
}

/** The DEMOTILES style works without an API key. If it fails to load at
 *  runtime, the maplibre 'error' event flips `tilesFailed` and we show
 *  a banner — the markers still render as overlays on a gray world. */
const DEMOTILES_STYLE = 'https://demotiles.maplibre.org/style.json';

/** Minimal maplibre-gl surface we use — keeps the cast list small. */
interface MaplibreMap {
  addSource(id: string, src: unknown): void;
  removeSource(id: string): void;
  addLayer(layer: unknown): void;
  removeLayer(id: string): void;
  setLayoutProperty(id: string, name: string, value: unknown): void;
  setPaintProperty(id: string, name: string, value: unknown): void;
  getSource(id: string): { setData(d: unknown): void } | undefined;
  addControl(ctrl: unknown, pos?: string): void;
  on(event: string, handler: (e: { error?: Error }) => void): void;
  off(event: string, handler: (e: { error?: Error }) => void): void;
  remove(): void;
  fitBounds(bounds: [[number, number], [number, number]], opts?: unknown): void;
  jumpTo(opts: { center: [number, number]; zoom: number }): void;
  getCanvas(): HTMLCanvasElement;
}

interface MaplibreNs {
  Map: new (opts: Record<string, unknown>) => MaplibreMap;
  NavigationControl: new (opts?: Record<string, unknown>) => unknown;
  ScaleControl: new (opts?: Record<string, unknown>) => unknown;
}

function MapViz(props: { receiverId: string; config: MapVizConfig }): JSX.Element {
  const cfg = props.config;
  const [state, setState] = createSignal<MapFeedState>(initialMapState());
  const [now, setNow] = createSignal(Date.now() / 1000);
  const [attached, setAttached] = createSignal(false);
  const [busy, setBusy] = createSignal(false);
  const [error, setError] = createSignal<string | null>(null);
  const [mapReady, setMapReady] = createSignal(false);
  const [tilesFailed, setTilesFailed] = createSignal<string | null>(null);

  // The ref-callback pattern (SolidJS): assigns the DOM node when the
  // div is created so we can pass it into maplibre's `container` option.
  let container: HTMLDivElement | undefined;
  let map: MaplibreMap | null = null;
  const sourceId = `${cfg.family}-markers`;
  const layerId = `${cfg.family}-layer`;
  const labelLayerId = `${cfg.family}-labels`;

  const applyEvent = (env: DecoderEventEnvelope): void => {
    setState((prev) =>
      cfg.family === 'aircraft'
        ? applyAircraftDecoderEvent(prev, env, now())
        : applyVesselDecoderEvent(prev, env, now()),
    );
  };

  const refreshStatus = async (): Promise<void> => {
    try {
      const decoders = await api.listReceiverDecoders(props.receiverId);
      setAttached(decoders.some((d) => cfg.decoderNames.includes(d.name)));
      setError(null);
    } catch (e) {
      if (e instanceof ApiError) setError(e.detail);
    }
  };

  const toggle = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      if (attached()) {
        const decoders = await api.listReceiverDecoders(props.receiverId);
        for (const d of decoders) {
          if (cfg.decoderNames.includes(d.name)) {
            await api.detachDecoder(props.receiverId, d.name);
          }
        }
        setAttached(false);
        setState(initialMapState());
      } else {
        await api.attachDecoder(props.receiverId, cfg.defaultDecoder);
        setAttached(true);
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setBusy(false);
    }
  };

  const pushMarkers = (markers: readonly MapMarker[]): void => {
    if (!map || !mapReady()) return;
    const source = map.getSource(sourceId);
    if (!source) return;
    const nowSec = now();
    const features = markers.map((m) => ({
      type: 'Feature' as const,
      geometry: { type: 'Point' as const, coordinates: [m.lon, m.lat] },
      properties: {
        id: m.id,
        label: m.label,
        color: markerColor(m.family, m.staleness),
        opacity: markerOpacity(stalenessBucket(nowSec, m.last_seen)),
      },
    }));
    source.setData({ type: 'FeatureCollection', features });
  };

  const fitToMarkers = (): void => {
    if (!map) return;
    const bbox = markerBounds(state().markers);
    if (!bbox) return;
    map.fitBounds(
      [[bbox[0], bbox[1]], [bbox[2], bbox[3]]],
      { padding: 60, maxZoom: 14 },
    );
  };

  onMount(() => {
    const session = receiverRegistry.getOrCreate(props.receiverId);
    const unsub = session.decoderStream.subscribe(applyEvent);
    const ageTimer = setInterval(() => setNow(Date.now() / 1000), 1000);
    void refreshStatus();
    const statusTimer = setInterval(() => void refreshStatus(), 10_000);

    // Lazy-init the maplibre map. We do this AFTER the local container
    // div is created so the maplibre Map constructor sees a real DOM
    // node. (container is assigned by the ref callback in the JSX.)
    void (async () => {
      const ml = (await loadMaplibre()) as MaplibreNs | null;
      if (!ml || container === undefined) {
        setTilesFailed('maplibre unavailable — install maplibre-gl to enable the map');
        return;
      }
      try {
        map = new ml.Map({
          container,
          style: DEMOTILES_STYLE,
          center: [0, 25],
          zoom: 1.5,
          attributionControl: true,
        });
        const errorHandler = (e: { error?: Error }): void => {
          const err = e?.error;
          if (err && (err.message.includes('style') || err.message.includes('Network') || err.message.includes('tile'))) {
            setTilesFailed(`tiles unavailable (${err.message})`);
          }
        };
        map.on('error', errorHandler);
        map.on('load', () => {
          if (!map) return;
          map.addSource(sourceId, {
            type: 'geojson',
            data: { type: 'FeatureCollection', features: [] },
          });
          map.addLayer({
            id: layerId,
            type: 'circle',
            source: sourceId,
            paint: {
              'circle-radius': 6,
              'circle-color': ['get', 'color'],
              'circle-opacity': ['get', 'opacity'],
              'circle-stroke-color': '#ffffff',
              'circle-stroke-width': 1,
              'circle-stroke-opacity': ['get', 'opacity'],
            },
          });
          map.addLayer({
            id: labelLayerId,
            type: 'symbol',
            source: sourceId,
            layout: {
              'text-field': ['get', 'label'],
              'text-size': 11,
              'text-offset': [0, 1.2],
              'text-allow-overlap': true,
            },
            paint: {
              'text-color': '#e5e7eb',
              'text-halo-color': '#000000',
              'text-halo-width': 1.5,
            },
          });
          setMapReady(true);
        });
        onCleanup(() => {
          map?.off('error', errorHandler);
          map?.remove();
          map = null;
        });
      } catch (e) {
        setTilesFailed(`maplibre init failed: ${e instanceof Error ? e.message : String(e)}`);
      }
    })();

    onCleanup(() => {
      unsub();
      clearInterval(ageTimer);
      clearInterval(statusTimer);
    });
  });

  // Push markers whenever the state or map readiness changes.
  createEffect(() => {
    pushMarkers(state().markers);
  });

  const totalMarkers = () => state().markers.length;

  return (
    <div class="relative flex h-full w-full flex-col bg-base-900">
      {/* header: family label + counters + actions */}
      <div class="flex h-9 shrink-0 items-center justify-between border-b border-base-800 px-2">
        <div class="flex items-center gap-2 text-[10px] text-base-300 font-mono">
          <span class="text-cyan-450">
            {cfg.family === 'aircraft' ? 'ADS-B MAP' : 'AIS MAP'}
          </span>
          <Show when={totalMarkers() > 0}>
            <span class="rounded bg-base-800 px-1">{totalMarkers()} markers</span>
          </Show>
          <Show when={state().frameCount > 0}>
            <span class="rounded bg-base-800 px-1">{state().frameCount} frames</span>
          </Show>
        </div>
        <div class="flex items-center gap-1">
          <button
            type="button"
            class="rounded px-1.5 py-0.5 text-[10px] bg-base-800 text-base-200 hover:bg-base-700 disabled:opacity-40"
            disabled={!mapReady() || totalMarkers() === 0}
            onClick={fitToMarkers}
            title="Fit view to all markers"
          >
            fit
          </button>
          <button
            type="button"
            disabled={busy()}
            class={`rounded px-1.5 py-0.5 text-[10px] ${
              attached()
                ? 'bg-rose-450/15 text-rose-450 hover:bg-rose-450/25'
                : 'bg-amber-450/20 text-amber-450 hover:bg-amber-450/30'
            } disabled:opacity-50`}
            onClick={() => void toggle()}
            title={attached() ? `Detach ${cfg.defaultDecoder}` : `Attach ${cfg.defaultDecoder}`}
          >
            {attached() ? 'detach' : `+ ${cfg.defaultDecoder}`}
          </button>
        </div>
      </div>

      {/* error line */}
      <Show when={error()}>
        <div class="shrink-0 bg-rose-450/10 px-2 py-0.5 text-[10px] text-rose-350">
          {error()}
        </div>
      </Show>

      {/* map canvas (or list fallback when maplibre unavailable) */}
      <div class="relative min-h-0 flex-1">
        <div ref={container} class="absolute inset-0" />
        <Show when={tilesFailed()}>
          <div class="absolute left-0 right-0 top-0 z-10 bg-amber-450/10 px-2 py-1 text-[10px] text-amber-450">
            {tilesFailed()}
          </div>
        </Show>
        <Show when={!attached() && totalMarkers() === 0}>
          <div class="absolute inset-0 flex flex-col items-center justify-center gap-2 px-4 text-center text-[10px] text-base-400 pointer-events-none">
            <span>{cfg.family === 'aircraft' ? 'no ADS-B decoder attached' : 'no AIS decoder attached'}</span>
            <span class="text-base-500">
              {cfg.family === 'aircraft'
                ? 'attach a 2 MSPS ADS-B source (e.g. RTL-SDR @ 1090 MHz)'
                : 'attach a 48 kSps AIS source (VFO tap on 162 MHz)'}
            </span>
          </div>
        </Show>
      </div>
    </div>
  );
}

export default MapViz;
