/** Tests for the PacketListViz model (slice-49).
 *
 *  Tests the pure-logic model: applyDecoderEvent, formatTime, formatAge,
 *  formatFrameType, formatDigipeaters. No SolidJS rendering.
 */

import { describe, it, expect } from 'vitest';
import {
  applyDecoderEvent,
  formatAge,
  formatDigipeaters,
  formatFrameType,
  formatTime,
  initialPacketListState,
  MAX_PACKETS,
} from './packetListModel';
import type { DecoderEventEnvelope } from '@openwebrx-plus/shared-types';

function makeEnvelope(
  decoder: string,
  event: Record<string, unknown>,
): DecoderEventEnvelope {
  return {
    type: 'decoder',
    decoder,
    receiverId: 'rx-1',
    event: event as any,
  };
}

describe('applyDecoderEvent — packet events', () => {
  it('appends a valid packet row', () => {
    const state = initialPacketListState();
    const env = makeEnvelope('ax25', {
      kind: 'packet',
      ts: 100,
      source: 'K1ABC-1',
      destination: 'APRS',
      digipeaters: ['WIDE2-2'],
      control: 3,
      frame_type: 'U',
      info_hex: '6869',
      info_text: 'hi',
      packet_index: 0,
    });
    const next = applyDecoderEvent(state, env);
    expect(next.packets).toHaveLength(1);
    expect(next.packets[0].source).toBe('K1ABC-1');
    expect(next.packets[0].destination).toBe('APRS');
    expect(next.packets[0].frame_type).toBe('U');
    expect(next.packets[0].is_crc_error).toBe(false);
    expect(next.total_decoded).toBe(1);
    expect(next.total_crc_errors).toBe(0);
    expect(next.last_update).toBe(100);
    expect(next.decoder_name).toBe('ax25');
  });

  it('appends multiple packets in sequence', () => {
    let state = initialPacketListState();
    for (let i = 0; i < 3; i++) {
      state = applyDecoderEvent(state, makeEnvelope('ax25', {
        kind: 'packet', ts: i, source: `CALL${i}`, destination: 'APRS',
        digipeaters: [], control: 0, frame_type: 'I', info_hex: '', info_text: '',
        packet_index: i,
      }));
    }
    expect(state.packets).toHaveLength(3);
    expect(state.total_decoded).toBe(3);
    expect(state.packets[2].source).toBe('CALL2');
  });

  it('trims the ring buffer at MAX_PACKETS', () => {
    let state = initialPacketListState();
    for (let i = 0; i < MAX_PACKETS + 5; i++) {
      state = applyDecoderEvent(state, makeEnvelope('ax25', {
        kind: 'packet', ts: i, source: `X${i}`, destination: 'Y',
        digipeaters: [], control: 0, frame_type: 'I', info_hex: '', info_text: '',
        packet_index: i,
      }));
    }
    expect(state.packets).toHaveLength(MAX_PACKETS);
    // The oldest 5 should have been dropped.
    expect(state.packets[0].source).toBe('X5');
    expect(state.packets[MAX_PACKETS - 1].source).toBe(`X${MAX_PACKETS + 4}`);
  });
});

describe('applyDecoderEvent — CRC error events', () => {
  it('appends a CRC error row with is_crc_error=true', () => {
    const state = initialPacketListState();
    const env = makeEnvelope('ax25', {
      kind: 'crc_error', ts: 200, reason: 'CRC mismatch', raw_hex: 'dead', length: 32,
    });
    const next = applyDecoderEvent(state, env);
    expect(next.packets).toHaveLength(1);
    expect(next.packets[0].is_crc_error).toBe(true);
    expect(next.packets[0].error_reason).toBe('CRC mismatch');
    expect(next.packets[0].frame_type).toBe('ERR');
    expect(next.total_crc_errors).toBe(1);
    expect(next.total_decoded).toBe(0);
  });

  it('counts packets and errors separately', () => {
    let state = initialPacketListState();
    state = applyDecoderEvent(state, makeEnvelope('ax25', {
      kind: 'packet', ts: 1, source: 'A', destination: 'B', digipeaters: [],
      control: 0, frame_type: 'I', info_hex: '', info_text: '', packet_index: 0,
    }));
    state = applyDecoderEvent(state, makeEnvelope('ax25', {
      kind: 'crc_error', ts: 2, reason: 'bad', raw_hex: '00', length: 1,
    }));
    state = applyDecoderEvent(state, makeEnvelope('ax25', {
      kind: 'packet', ts: 3, source: 'C', destination: 'D', digipeaters: [],
      control: 0, frame_type: 'I', info_hex: '', info_text: '', packet_index: 1,
    }));
    expect(state.total_decoded).toBe(2);
    expect(state.total_crc_errors).toBe(1);
    expect(state.packets).toHaveLength(3);
  });
});

describe('applyDecoderEvent — rejection', () => {
  it('ignores non-packet-decoder events', () => {
    const state = initialPacketListState();
    const env = makeEnvelope('cw', { kind: 'packet', ts: 1 });
    const next = applyDecoderEvent(state, env);
    expect(next).toBe(state);
  });

  it('ignores unknown event kinds', () => {
    const state = initialPacketListState();
    const env = makeEnvelope('ax25', { kind: 'unknown', ts: 1 });
    const next = applyDecoderEvent(state, env);
    expect(next).toBe(state);
  });
});

describe('formatTime', () => {
  it('formats a timestamp as HH:MM:SS UTC', () => {
    const result = formatTime(1704067200);
    expect(result).toMatch(/^\d{2}:\d{2}:\d{2}Z$/);
  });

  it('returns placeholder for zero timestamp', () => {
    expect(formatTime(0)).toBe('--:--:--');
  });
});

describe('formatAge', () => {
  it('formats age in seconds', () => {
    expect(formatAge(100, 110)).toBe('10s');
  });

  it('formats age in minutes', () => {
    expect(formatAge(100, 160)).toBe('1m');
  });

  it('returns placeholder for zero timestamp', () => {
    expect(formatAge(0, 100)).toBe('--');
  });
});

describe('formatFrameType', () => {
  it('formats I-frame with control byte', () => {
    expect(formatFrameType('I', 0x00)).toBe('I (0x00)');
  });

  it('formats U-frame with control byte', () => {
    expect(formatFrameType('U', 0x03)).toBe('U (0x03)');
  });

  it('formats CRC error as "CRC ERR"', () => {
    expect(formatFrameType('ERR', 0)).toBe('CRC ERR');
  });
});

describe('formatDigipeaters', () => {
  it('returns empty string for no digipeaters', () => {
    expect(formatDigipeaters([])).toBe('');
  });

  it('formats single digipeater', () => {
    expect(formatDigipeaters(['WIDE2-2'])).toBe(' via WIDE2-2');
  });

  it('formats multiple digipeaters with > separator', () => {
    expect(formatDigipeaters(['WIDE1-1', 'WIDE2-2'])).toBe(' via WIDE1-1 > WIDE2-2');
  });
});

describe('initialPacketListState', () => {
  it('returns empty state', () => {
    const state = initialPacketListState();
    expect(state.packets).toHaveLength(0);
    expect(state.total_decoded).toBe(0);
    expect(state.total_crc_errors).toBe(0);
    expect(state.last_update).toBe(0);
    expect(state.decoder_name).toBeNull();
  });
});
