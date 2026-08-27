// @vitest-environment node
/** Tests for the MapViz pure model — decoder event → marker fold. */

import { describe, expect, it } from 'vitest';
import {
  applyAircraftDecoderEvent,
  applyVesselDecoderEvent,
  initialMapState,
  markerBounds,
  markerColor,
  markerOpacity,
  snapshotToMarkers,
  stalenessBucket,
  type MapMarker,
} from './mapModel';
import type { DecoderEventEnvelope } from '@openwebrx-plus/shared-types';

function adsbEnvelope(event: Record<string, unknown>): DecoderEventEnvelope {
  return {
    type: 'decoder',
    decoder: 'adsb',
    receiverId: 'rx-test',
    event: { kind: 'unknown', ...event },
  };
}

function aisEnvelope(event: Record<string, unknown>): DecoderEventEnvelope {
  return {
    type: 'decoder',
    decoder: 'ais',
    receiverId: 'rx-test',
    event: { kind: 'unknown', ...event },
  };
}

describe('stalenessBucket', () => {
  it('fresh under 30 s', () => {
    expect(stalenessBucket(100, 75)).toBe('fresh');
    expect(stalenessBucket(100, 99.9)).toBe('fresh');
  });
  it('stale 30-120 s', () => {
    expect(stalenessBucket(100, 50)).toBe('stale');
    expect(stalenessBucket(200, 100)).toBe('stale');
  });
  it('stale-2 120-300 s', () => {
    expect(stalenessBucket(200, 0)).toBe('stale-2');
    expect(stalenessBucket(500, 250)).toBe('stale-2');
  });
  it('dead beyond 300 s', () => {
    expect(stalenessBucket(1000, 100)).toBe('dead');
    expect(stalenessBucket(1000, 0)).toBe('dead');
  });
  it('clamps negative age to fresh', () => {
    expect(stalenessBucket(50, 100)).toBe('fresh');
  });
});

describe('markerColor + markerOpacity', () => {
  it('colors every bucket for both families', () => {
    const families = ['aircraft', 'vessel'] as const;
    const buckets = ['fresh', 'stale', 'stale-2', 'dead'] as const;
    for (const f of families) {
      for (const b of buckets) {
        const c = markerColor(f, b);
        expect(c).toMatch(/^#[0-9a-f]{6}$/i);
      }
    }
  });
  it('opacities decrease with staleness', () => {
    expect(markerOpacity('fresh')).toBeGreaterThan(markerOpacity('stale'));
    expect(markerOpacity('stale')).toBeGreaterThan(markerOpacity('stale-2'));
    expect(markerOpacity('stale-2')).toBeGreaterThan(markerOpacity('dead'));
  });
  it('aircraft fresh is cyan, vessel fresh is lime', () => {
    expect(markerColor('aircraft', 'fresh')).toBe('#22d3ee');
    expect(markerColor('vessel', 'fresh')).toBe('#a3e635');
  });
});

describe('snapshotToMarkers', () => {
  it('drops rows without lat+lon', () => {
    const markers = snapshotToMarkers('aircraft', [
      { icao: 'A', callsign: null, altitude_ft: null, frames: 1, last_seen: 0, rssi_dbfs: -10, lat: 1, lon: 2 },
      { icao: 'B', callsign: null, altitude_ft: null, frames: 1, last_seen: 0, rssi_dbfs: -10 },
    ], 0);
    expect(markers).toHaveLength(1);
    expect(markers[0].id).toBe('A');
  });
  it('vessel heading prefers heading_deg over course_deg', () => {
    const a = snapshotToMarkers('vessel', [
      { mmsi: '1', type: 1, vessel_name: null, callsign: null, imo: null, ship_type: null, speed_kn: null, longitude: 0, latitude: 0, course_deg: 90, heading_deg: 180, nav_status: null, destination: null, frames: 1, last_seen: 0, rssi_dbfs: -10 },
    ], 0)[0];
    expect(a?.heading).toBe(180);
    const b = snapshotToMarkers('vessel', [
      { mmsi: '1', type: 1, vessel_name: null, callsign: null, imo: null, ship_type: null, speed_kn: null, longitude: 0, latitude: 0, course_deg: 90, heading_deg: null, nav_status: null, destination: null, frames: 1, last_seen: 0, rssi_dbfs: -10 },
    ], 0)[0];
    expect(b?.heading).toBe(90);
  });
  it('aircraft speed from groundspeed_kt', () => {
    const m = snapshotToMarkers('aircraft', [
      { icao: 'A', callsign: 'OWRX', altitude_ft: 12500, frames: 1, last_seen: 0, rssi_dbfs: -10, lat: 1, lon: 2, groundspeed_kt: 250 },
    ], 0)[0];
    expect(m?.speed_kn).toBe(250);
    expect(m?.altitude_ft).toBe(12500);
  });
  it('label falls back to id when callsign/name missing', () => {
    const a = snapshotToMarkers('aircraft', [
      { icao: 'AABBCC', callsign: null, altitude_ft: null, frames: 1, last_seen: 0, rssi_dbfs: -10, lat: 0, lon: 0 },
    ], 0)[0];
    expect(a?.label).toBe('AABBCC');
    expect(a?.sublabel).toBe('AABBCC');
  });
});

describe('applyAircraftDecoderEvent', () => {
  it('starts empty and ignores non-adsb', () => {
    expect(initialMapState().markers).toEqual([]);
    const s = applyAircraftDecoderEvent(initialMapState(), aisEnvelope({ kind: 'frame' }), 0);
    expect(s.frameCount).toBe(0);
  });
  it('folds aircraft snapshot into markers', () => {
    const s = applyAircraftDecoderEvent(
      initialMapState(),
      adsbEnvelope({
        kind: 'aircraft',
        aircraft: [
          { icao: 'A', callsign: 'OWRX', altitude_ft: 12500, frames: 1, last_seen: 100, rssi_dbfs: -10, lat: 37.42, lon: -121.63 },
          { icao: 'B', callsign: null, altitude_ft: null, frames: 1, last_seen: 100, rssi_dbfs: -10 },
        ],
      }),
      100,
    );
    expect(s.markers).toHaveLength(1);
    expect(s.markers[0].id).toBe('A');
    expect(s.markers[0].lat).toBe(37.42);
    expect(s.markers[0].lon).toBe(-121.63);
    expect(s.markers[0].family).toBe('aircraft');
  });
  it('counts frames', () => {
    let s = initialMapState();
    s = applyAircraftDecoderEvent(s, adsbEnvelope({ kind: 'frame', df: 17, icao: 'A', raw: '8D', parity: 'data', rssi_dbfs: -10 }), 0);
    s = applyAircraftDecoderEvent(s, adsbEnvelope({ kind: 'frame', df: 11, icao: 'B', raw: '5D', parity: 'address', rssi_dbfs: -10 }), 0);
    expect(s.frameCount).toBe(2);
  });
  it('surfaces decoder_state', () => {
    const s = applyAircraftDecoderEvent(
      initialMapState(),
      adsbEnvelope({ kind: 'decoder_state', state: 'restarting', reason: 'crashed', restarts: 1 }),
      0,
    );
    expect(s.decoderState).toEqual({ state: 'restarting', reason: 'crashed' });
  });
  it('leaves unknown event kinds untouched', () => {
    const s = applyAircraftDecoderEvent(initialMapState(), adsbEnvelope({ kind: 'future' }), 0);
    expect(s).toEqual(initialMapState());
  });
  it('dump1090 family folds identically', () => {
    const envelope: DecoderEventEnvelope = {
      type: 'decoder', decoder: 'dump1090', receiverId: 'rx', event: { kind: 'aircraft', aircraft: [
        { icao: 'D', callsign: null, altitude_ft: 0, frames: 1, last_seen: 0, rssi_dbfs: -10, lat: 1, lon: 1 },
      ] },
    };
    const s = applyAircraftDecoderEvent(initialMapState(), envelope, 0);
    expect(s.markers).toHaveLength(1);
  });
});

describe('applyVesselDecoderEvent', () => {
  it('ignores non-ais', () => {
    const s = applyVesselDecoderEvent(initialMapState(), adsbEnvelope({ kind: 'frame' }), 0);
    expect(s.frameCount).toBe(0);
  });
  it('folds vessel snapshot into markers', () => {
    const s = applyVesselDecoderEvent(
      initialMapState(),
      aisEnvelope({
        kind: 'vessel',
        vessels: [
          { mmsi: '123456789', type: 5, vessel_name: 'TEST', callsign: null, imo: null, ship_type: 70, speed_kn: 12.5, longitude: -122.4, latitude: 37.8, course_deg: 90, heading_deg: 95, nav_status: 0, destination: 'SF', frames: 1, last_seen: 200, rssi_dbfs: -10 },
        ],
      }),
      200,
    );
    expect(s.markers).toHaveLength(1);
    expect(s.markers[0].label).toBe('TEST');
    expect(s.markers[0].family).toBe('vessel');
  });
  it('drops vessels without position', () => {
    const s = applyVesselDecoderEvent(
      initialMapState(),
      aisEnvelope({
        kind: 'vessel',
        vessels: [
          { mmsi: '1', type: 5, vessel_name: null, callsign: null, imo: null, ship_type: null, speed_kn: null, longitude: null, latitude: null, course_deg: null, heading_deg: null, nav_status: null, destination: null, frames: 1, last_seen: 0, rssi_dbfs: -10 },
        ],
      }),
      0,
    );
    expect(s.markers).toHaveLength(0);
  });
  it('counts frames', () => {
    let s = applyVesselDecoderEvent(initialMapState(), aisEnvelope({ kind: 'frame', type: 1, mmsi: '1', raw: '', rssi_dbfs: -10 }), 0);
    s = applyVesselDecoderEvent(s, aisEnvelope({ kind: 'frame', type: 5, mmsi: '2', raw: '', rssi_dbfs: -10 }), 0);
    expect(s.frameCount).toBe(2);
  });
});

describe('markerBounds', () => {
  it('null for empty', () => {
    expect(markerBounds([])).toBeNull();
  });
  it('tight bbox for two markers', () => {
    const ms: MapMarker[] = [
      { id: 'A', label: 'A', sublabel: 'A', lat: 40, lon: -120, heading: null, speed_kn: null, staleness: 'fresh', last_seen: 0, family: 'aircraft', altitude_ft: 0 },
      { id: 'B', label: 'B', sublabel: 'B', lat: 35, lon: -110, heading: null, speed_kn: null, staleness: 'fresh', last_seen: 0, family: 'aircraft', altitude_ft: 0 },
    ];
    expect(markerBounds(ms)).toEqual([-120, 35, -110, 40]);
  });
  it('single point gets a small spread', () => {
    const ms: MapMarker[] = [
      { id: 'A', label: 'A', sublabel: 'A', lat: 40, lon: -120, heading: null, speed_kn: null, staleness: 'fresh', last_seen: 0, family: 'aircraft', altitude_ft: 0 },
    ];
    const bbox = markerBounds(ms);
    expect(bbox).not.toBeNull();
    expect(bbox![0]).toBeLessThan(bbox![2]);
    expect(bbox![1]).toBeLessThan(bbox![3]);
  });
});
