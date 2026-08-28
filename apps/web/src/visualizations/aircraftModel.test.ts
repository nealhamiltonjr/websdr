// @vitest-environment node
/** Tests for the AircraftListViz pure model — decoder event → state fold. */

import { describe, expect, it } from 'vitest';
import {
  applyDecoderEvent,
  formatAge,
  formatAltitude,
  formatFrameSummary,
  formatPosition,
  initialAircraftState,
} from './aircraftModel';
import type { DecoderEventEnvelope } from '@openwebrx-plus/shared-types';

function adsbEvent(event: Record<string, unknown>): DecoderEventEnvelope {
  return {
    type: 'decoder',
    decoder: 'adsb',
    receiverId: 'rx-test',
    event: { kind: 'unknown', ...event },
  };
}

function dump1090Event(event: Record<string, unknown>): DecoderEventEnvelope {
  return {
    type: 'decoder',
    decoder: 'dump1090',
    receiverId: 'rx-test',
    event: { kind: 'unknown', ...event },
  };
}

describe('applyDecoderEvent', () => {
  it('starts empty', () => {
    const s = initialAircraftState();
    expect(s.rows).toEqual([]);
    expect(s.lastFrame).toBeNull();
    expect(s.frameCount).toBe(0);
  });

  it('ignores non-adsb-family decoders', () => {
    const envelope: DecoderEventEnvelope = {
      type: 'decoder',
      decoder: 'ais',
      receiverId: 'rx-test',
      event: { kind: 'frame', something: true },
    };
    const s = applyDecoderEvent(initialAircraftState(), envelope);
    expect(s.frameCount).toBe(0);
  });

  it('accepts the subprocess dump1090 family member', () => {
    let s = applyDecoderEvent(
      initialAircraftState(),
      dump1090Event({
        kind: 'frame',
        df: 17,
        icao: '4D22AA',
        raw: '8D4D22AA203D7498B6F9A1',
        parity: 'data',
        rssi_dbfs: -0.9,
      }),
    );
    expect(s.frameCount).toBe(1);
    s = applyDecoderEvent(
      s,
      dump1090Event({ kind: 'aircraft', aircraft: [] }),
    );
    expect(s.rows).toEqual([]);
  });

  it('tracks position-bearing rows from the subprocess decoder', () => {
    const s = applyDecoderEvent(
      initialAircraftState(),
      dump1090Event({
        kind: 'aircraft',
        aircraft: [
          {
            icao: '4D22AA',
            callsign: 'OWRX001',
            altitude_ft: 12500,
            frames: 4,
            last_seen: 100,
            rssi_dbfs: -0.9,
            lat: 37.42,
            lon: -121.63,
            groundspeed_kt: 250,
            position_source: 'synthetic',
          },
          {
            icao: 'AABBCC',
            callsign: null,
            altitude_ft: null,
            frames: 2,
            last_seen: 99,
            rssi_dbfs: -18.2,
          },
        ],
      }),
    );
    expect(s.positionCount).toBe(1);
    expect(formatPosition(s.rows[0])).toBe('37.42, -121.63');
    expect(formatPosition(s.rows[1])).toBe('—');
  });

  it('surfaces subprocess decoder_state lifecycle events', () => {
    let s = applyDecoderEvent(
      initialAircraftState(),
      dump1090Event({ kind: 'decoder_state', state: 'restarting', attempt: 1, delay: 0.5 }),
    );
    expect(s.decoderState).toEqual({ state: 'restarting', reason: undefined });
    s = applyDecoderEvent(
      s,
      dump1090Event({
        kind: 'decoder_state',
        state: 'failed',
        reason: 'decoder exited rc=1 after 2 restarts',
        restarts: 2,
      }),
    );
    expect(s.decoderState).toEqual({
      state: 'failed',
      reason: 'decoder exited rc=1 after 2 restarts',
    });
    // Unknown kinds (and only-adsb-known kinds) leave decoderState untouched
    const s2 = applyDecoderEvent(s, adsbEvent({ kind: 'future' }));
    expect(s2.decoderState).toEqual({
      state: 'failed',
      reason: 'decoder exited rc=1 after 2 restarts',
    });
  });

  it('folds aircraft snapshots', () => {
    const s = applyDecoderEvent(
      initialAircraftState(),
      adsbEvent({
        kind: 'aircraft',
        aircraft: [
          {
            icao: '4D22AA',
            callsign: 'OWRX001',
            altitude_ft: 12500,
            frames: 4,
            last_seen: 100,
            rssi_dbfs: -0.9,
          },
        ],
      }),
    );
    expect(s.rows).toHaveLength(1);
    expect(s.rows[0].callsign).toBe('OWRX001');
  });

  it('counts frames and renders a summary line', () => {
    let s = initialAircraftState();
    s = applyDecoderEvent(
      s,
      adsbEvent({
        kind: 'frame',
        df: 17,
        icao: '4D22AA',
        callsign: 'OWRX001',
        altitude_ft: 12500,
        raw: '8D4D22AA203D7498B6F9A1',
        parity: 'data',
        rssi_dbfs: -0.9,
      }),
    );
    s = applyDecoderEvent(
      s,
      adsbEvent({
        kind: 'frame',
        df: 11,
        icao: 'AABBCC',
        raw: '5DAABBCCFFAE4BCD3E',
        parity: 'data',
        rssi_dbfs: -18.0,
      }),
    );
    expect(s.frameCount).toBe(2);
    expect(s.lastFrame).toBe('DF11 · AABBCC');
    // Snapshots and frames fold independently
    const s2 = applyDecoderEvent(s, adsbEvent({ kind: 'aircraft', aircraft: [] }));
    expect(s2.frameCount).toBe(2);
    expect(s2.rows).toEqual([]);
  });

  it('leaves unknown event kinds untouched', () => {
    const s = applyDecoderEvent(initialAircraftState(), adsbEvent({ kind: 'future' }));
    expect(s).toEqual(initialAircraftState());
  });
});

describe('formatting helpers', () => {
  it('formatFrameSummary joins present fields', () => {
    expect(
      formatFrameSummary({
        kind: 'frame',
        ts: 0,
        df: 17,
        icao: '4D22AA',
        callsign: 'OWRX001',
        altitude_ft: 12500,
        raw: '8D',
        parity: 'data',
        rssi_dbfs: -1,
      }),
    ).toBe('DF17 · 4D22AA · OWRX001 · 12,500 ft');
    expect(
      formatFrameSummary({
        kind: 'frame',
        ts: 0,
        df: 11,
        icao: 'AABBCC',
        raw: '5D',
        parity: 'address',
        rssi_dbfs: -18,
      }),
    ).toBe('DF11 · AABBCC');
  });

  it('formatAge buckets seconds', () => {
    expect(formatAge(100, 99.6)).toBe('0s');
    expect(formatAge(100, 98)).toBe('2s');
    expect(formatAge(3600, 0)).toBe('1h');
  });

  it('formatAltitude renders null as dash', () => {
    expect(formatAltitude(null)).toBe('—');
    expect(formatAltitude(12500)).toBe('12,500 ft');
  });

  it('formatPosition renders missing coords as dash', () => {
    expect(formatPosition({
      icao: 'AABBCC',
      callsign: null,
      altitude_ft: null,
      frames: 1,
      last_seen: 0,
      rssi_dbfs: -10,
    })).toBe('—');
  });
});
