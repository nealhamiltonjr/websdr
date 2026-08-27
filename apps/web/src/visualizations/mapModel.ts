/** MapViz model — pure state reducer over decoder events for map markers.
 *
 *  Two parallel reducers live here: one for ADS-B aircraft (turns AdsbAircraftRow
 *  into MapAircraftMarker — only rows with lat+lon survive), and one for AIS
 *  vessels (turns AisVesselRow into MapVesselMarker). Both produce the same
 *  MapMarker shape so the rendering layer can stay generic.
 *
 *  The reducer is pure: no DOM, no maplibre imports. Tests cover every
 *  branch (frame, snapshot, decoder_state, unknown).
 *
 *  Staleness bucket: "fresh" < 30 s, "stale" 30-120 s, "stale-2" 120-300 s,
 *  "dead" > 300 s. The bucket drives marker opacity in the renderer.
 */

import {
  ADSB_DECODERS,
  AIS_DECODERS,
  type AdsbAircraftRow,
  type AisVesselRow,
  type DecoderEventEnvelope,
} from '@openwebrx-plus/shared-types';

const adsbDecoderNames: readonly string[] = ADSB_DECODERS;
const aisDecoderNames: readonly string[] = AIS_DECODERS;

export type StalenessBucket = 'fresh' | 'stale' | 'stale-2' | 'dead';

export interface MapMarker {
  /** Stable id for keying/animation (ICAO hex or MMSI string). */
  id: string;
  /** Primary label (callsign or vessel name). */
  label: string;
  /** Secondary label (ICAO hex or MMSI). */
  sublabel: string;
  lat: number;
  lon: number;
  /** Heading in degrees true north, when known (for icon rotation). */
  heading: number | null;
  /** Speed in knots, when known (scales the trailing vector). */
  speed_kn: number | null;
  /** Staleness bucket computed from last_seen vs. now. */
  staleness: StalenessBucket;
  /** Raw epoch seconds of the last decoder update. */
  last_seen: number;
  /** Decoder family — drives icon + color. */
  family: 'aircraft' | 'vessel';
  /** Optional altitude for aircraft (vessels: always null). */
  altitude_ft: number | null;
}

export interface MapFeedState {
  markers: MapMarker[];
  /** Total frame events seen (for the header counter). */
  frameCount: number;
  /** Last subprocess lifecycle transition (null while healthy). */
  decoderState: { state: string; reason?: string } | null;
}

export function initialMapState(): MapFeedState {
  return { markers: [], frameCount: 0, decoderState: null };
}

const STALE_THRESHOLDS: readonly [number, StalenessBucket][] = [
  [30, 'fresh'],
  [120, 'stale'],
  [300, 'stale-2'],
];

/** Pick the staleness bucket for a (now, last_seen) pair. */
export function stalenessBucket(now: number, lastSeen: number): StalenessBucket {
  const age = Math.max(0, now - lastSeen);
  for (const [cutoff, bucket] of STALE_THRESHOLDS) {
    if (age < cutoff) return bucket;
  }
  return 'dead';
}

function rowToAircraftMarker(
  row: AdsbAircraftRow,
  now: number,
): MapMarker | null {
  if (typeof row.lat !== 'number' || typeof row.lon !== 'number') return null;
  return {
    id: row.icao,
    label: row.callsign ?? row.icao,
    sublabel: row.icao,
    lat: row.lat,
    lon: row.lon,
    heading: null, // ADS-B CPR doesn't give us track; future work.
    speed_kn: typeof row.groundspeed_kt === 'number' ? row.groundspeed_kt : null,
    staleness: stalenessBucket(now, row.last_seen),
    last_seen: row.last_seen,
    family: 'aircraft',
    altitude_ft: row.altitude_ft ?? null,
  };
}

function rowToVesselMarker(
  row: AisVesselRow,
  now: number,
): MapMarker | null {
  if (row.latitude === null || row.longitude === null) return null;
  return {
    id: row.mmsi,
    label: row.vessel_name ?? row.mmsi,
    sublabel: row.mmsi,
    lat: row.latitude,
    lon: row.longitude,
    heading: row.heading_deg ?? row.course_deg ?? null,
    speed_kn: row.speed_kn,
    staleness: stalenessBucket(now, row.last_seen),
    last_seen: row.last_seen,
    family: 'vessel',
    altitude_ft: null,
  };
}

/** Filter + project a snapshot into markers for a given family.
 *
 *  Pure helper used by both reducers; exported for tests.
 */
export function snapshotToMarkers(
  family: 'aircraft' | 'vessel',
  rows: Array<AdsbAircraftRow | AisVesselRow>,
  now: number,
): MapMarker[] {
  const out: MapMarker[] = [];
  for (const row of rows) {
    const m =
      family === 'aircraft'
        ? rowToAircraftMarker(row as AdsbAircraftRow, now)
        : rowToVesselMarker(row as AisVesselRow, now);
    if (m !== null) out.push(m);
  }
  return out;
}

/** Fold an ADS-B-family decoder event into the map state. */
export function applyAircraftDecoderEvent(
  state: MapFeedState,
  envelope: DecoderEventEnvelope,
  now: number,
): MapFeedState {
  if (!adsbDecoderNames.includes(envelope.decoder)) return state;
  const event = envelope.event;

  if (event.kind === 'aircraft') {
    const snapshot = event as unknown as { aircraft?: AdsbAircraftRow[] };
    return {
      ...state,
      markers: snapshotToMarkers('aircraft', snapshot.aircraft ?? [], now),
    };
  }
  if (event.kind === 'frame') {
    return { ...state, frameCount: state.frameCount + 1 };
  }
  if (event.kind === 'decoder_state') {
    const ev = event as unknown as { state: string; reason?: string };
    return { ...state, decoderState: { state: ev.state, reason: ev.reason } };
  }
  return state;
}

/** Fold an AIS-family decoder event into the map state. */
export function applyVesselDecoderEvent(
  state: MapFeedState,
  envelope: DecoderEventEnvelope,
  now: number,
): MapFeedState {
  if (!aisDecoderNames.includes(envelope.decoder)) return state;
  const event = envelope.event;

  if (event.kind === 'vessel') {
    const snapshot = event as unknown as { vessels?: AisVesselRow[] };
    return {
      ...state,
      markers: snapshotToMarkers('vessel', snapshot.vessels ?? [], now),
    };
  }
  if (event.kind === 'frame') {
    return { ...state, frameCount: state.frameCount + 1 };
  }
  return state;
}

/** Bounding box over a marker set: [west, south, east, north].
 *  Returns null if there are zero markers (so the renderer can keep its
 *  last good view rather than fit-to-empty-which-zooms-to-0,0). */
export function markerBounds(markers: readonly MapMarker[]): [number, number, number, number] | null {
  if (markers.length === 0) return null;
  let west = Infinity, south = Infinity, east = -Infinity, north = -Infinity;
  for (const m of markers) {
    if (m.lon < west) west = m.lon;
    if (m.lon > east) east = m.lon;
    if (m.lat < south) south = m.lat;
    if (m.lat > north) north = m.lat;
  }
  // Guard against a single point (give it a tiny spread so MapLibre's
  // fitBounds doesn't reject it).
  if (west === east) {
    west -= 0.001;
    east += 0.001;
  }
  if (south === north) {
    south -= 0.001;
    north += 0.001;
  }
  return [west, south, east, north];
}

/** Color for a marker, given family + staleness. */
export function markerColor(family: 'aircraft' | 'vessel', staleness: StalenessBucket): string {
  if (family === 'aircraft') {
    switch (staleness) {
      case 'fresh': return '#22d3ee'; // cyan-400
      case 'stale': return '#0891b2'; // cyan-600
      case 'stale-2': return '#155e75'; // cyan-800
      case 'dead': return '#164e63'; // cyan-950
    }
  }
  switch (staleness) {
    case 'fresh': return '#a3e635'; // lime-400
    case 'stale': return '#65a30d'; // lime-600
    case 'stale-2': return '#3f6212'; // lime-800
    case 'dead': return '#1a2e05'; // lime-950
  }
}

/** Opacity for a marker (0-1) — fades with staleness. */
export function markerOpacity(staleness: StalenessBucket): number {
  switch (staleness) {
    case 'fresh': return 1.0;
    case 'stale': return 0.75;
    case 'stale-2': return 0.45;
    case 'dead': return 0.2;
  }
}
