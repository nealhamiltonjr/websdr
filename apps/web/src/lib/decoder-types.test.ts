/**
 * Tests for the decoder type guards added in slice-42.
 *
 * Verifies that TEXT_DECODERS, IMAGE_DECODERS, and their type guards
 * correctly classify decoder events by family.
 */

import { describe, it, expect } from 'vitest';
import {
  ADSB_DECODERS,
  AIS_DECODERS,
  TEXT_DECODERS,
  IMAGE_DECODERS,
  DIGI_MESSAGE_DECODERS,
  PACKET_DECODERS,
  ACARS_DECODERS,
  DAB_DECODERS,
  ATC_DECODERS,
  isTextCharEvent,
  isTextSnapshotEvent,
  isImageEvent,
  isImageScanlineEvent,
  isImageModeEvent,
  isPacketEvent,
  isPacketCrcErrorEvent,
  isAcarsMessageEvent,
  isAcarsCrcErrorEvent,
  isDabServiceEvent,
  isDabEnsembleEvent,
  isAtcVoiceStartEvent,
  isAtcVoiceEndEvent,
  isAtcRssiEvent,
  type DecoderEventEnvelope,
} from '@openwebrx-plus/shared-types';

describe('decoder family constants (slice-42)', () => {
  it('TEXT_DECODERS includes cw, rtty, psk31, olivia', () => {
    expect(TEXT_DECODERS).toContain('cw');
    expect(TEXT_DECODERS).toContain('rtty');
    expect(TEXT_DECODERS).toContain('psk31');
    expect(TEXT_DECODERS).toContain('olivia');
    expect(TEXT_DECODERS).toHaveLength(4);
  });

  it('IMAGE_DECODERS includes sstv and fax', () => {
    expect(IMAGE_DECODERS).toContain('sstv');
    expect(IMAGE_DECODERS).toContain('fax');
    expect(IMAGE_DECODERS).toHaveLength(2);
  });

  it('ADSB_DECODERS unchanged', () => {
    expect(ADSB_DECODERS).toEqual(['adsb', 'dump1090', 'dump978']);
  });

  it('AIS_DECODERS unchanged', () => {
    expect(AIS_DECODERS).toEqual(['ais']);
  });

  it('DIGI_MESSAGE_DECODERS includes ft8, wspr, jt65, jt9', () => {
    expect(DIGI_MESSAGE_DECODERS).toContain('ft8');
    expect(DIGI_MESSAGE_DECODERS).toContain('wspr');
    expect(DIGI_MESSAGE_DECODERS).toContain('jt65');
    expect(DIGI_MESSAGE_DECODERS).toContain('jt9');
    expect(DIGI_MESSAGE_DECODERS).toHaveLength(4);
  });

  it('PACKET_DECODERS includes ax25', () => {
    expect(PACKET_DECODERS).toContain('ax25');
    expect(PACKET_DECODERS).toHaveLength(1);
  });
});

describe('isTextCharEvent', () => {
  it('returns true for cw frame events', () => {
    const env: DecoderEventEnvelope = {
      type: 'decoder',
      decoder: 'cw',
      receiverId: 'rx-1',
      event: { kind: 'frame', ts: 0, char: 'C' },
    };
    expect(isTextCharEvent(env)).toBe(true);
  });

  it('returns true for rtty frame events', () => {
    const env: DecoderEventEnvelope = {
      type: 'decoder',
      decoder: 'rtty',
      receiverId: 'rx-1',
      event: { kind: 'frame', ts: 0, char: 'Q' },
    };
    expect(isTextCharEvent(env)).toBe(true);
  });

  it('returns true for psk31 frame events', () => {
    const env: DecoderEventEnvelope = {
      type: 'decoder',
      decoder: 'psk31',
      receiverId: 'rx-1',
      event: { kind: 'frame', ts: 0, char: 'H' },
    };
    expect(isTextCharEvent(env)).toBe(true);
  });

  it('returns true for olivia frame events', () => {
    const env: DecoderEventEnvelope = {
      type: 'decoder',
      decoder: 'olivia',
      receiverId: 'rx-1',
      event: { kind: 'frame', ts: 0, char: 'I' },
    };
    expect(isTextCharEvent(env)).toBe(true);
  });

  it('returns false for adsb frame events (not a text decoder)', () => {
    const env: DecoderEventEnvelope = {
      type: 'decoder',
      decoder: 'adsb',
      receiverId: 'rx-1',
      event: { kind: 'frame', ts: 0, icao: 'AABBCC' },
    };
    expect(isTextCharEvent(env)).toBe(false);
  });

  it('returns false for text snapshot events (kind=text, not frame)', () => {
    const env: DecoderEventEnvelope = {
      type: 'decoder',
      decoder: 'cw',
      receiverId: 'rx-1',
      event: { kind: 'text', ts: 0, text: 'CQ' },
    };
    expect(isTextCharEvent(env)).toBe(false);
  });
});

describe('isTextSnapshotEvent', () => {
  it('returns true for cw text snapshot events', () => {
    const env: DecoderEventEnvelope = {
      type: 'decoder',
      decoder: 'cw',
      receiverId: 'rx-1',
      event: { kind: 'text', ts: 0, text: 'CQ CQ' },
    };
    expect(isTextSnapshotEvent(env)).toBe(true);
  });

  it('returns false for frame events (kind=frame, not text)', () => {
    const env: DecoderEventEnvelope = {
      type: 'decoder',
      decoder: 'cw',
      receiverId: 'rx-1',
      event: { kind: 'frame', ts: 0, char: 'C' },
    };
    expect(isTextSnapshotEvent(env)).toBe(false);
  });
});

describe('isImageEvent', () => {
  it('returns true for sstv image events', () => {
    const env: DecoderEventEnvelope = {
      type: 'decoder',
      decoder: 'sstv',
      receiverId: 'rx-1',
      event: {
        kind: 'image',
        ts: 0,
        mode: 'SCOTTIE_1',
        width: 320,
        height: 256,
        data: 'base64data',
        image_index: 0,
      },
    };
    expect(isImageEvent(env)).toBe(true);
  });

  it('returns false for non-sstv decoders', () => {
    const env: DecoderEventEnvelope = {
      type: 'decoder',
      decoder: 'cw',
      receiverId: 'rx-1',
      event: { kind: 'image', ts: 0, mode: 'X', width: 1, height: 1, data: '', image_index: 0 },
    };
    expect(isImageEvent(env)).toBe(false);
  });
});

describe('isImageScanlineEvent', () => {
  it('returns true for sstv scanline events', () => {
    const env: DecoderEventEnvelope = {
      type: 'decoder',
      decoder: 'sstv',
      receiverId: 'rx-1',
      event: { kind: 'scanline', ts: 0, scanline: 42 },
    };
    expect(isImageScanlineEvent(env)).toBe(true);
  });
});

describe('isImageModeEvent', () => {
  it('returns true for sstv mode events', () => {
    const env: DecoderEventEnvelope = {
      type: 'decoder',
      decoder: 'sstv',
      receiverId: 'rx-1',
      event: { kind: 'mode', ts: 0, mode: 'SCOTTIE_1', vis_code: 60 },
    };
    expect(isImageModeEvent(env)).toBe(true);
  });
});

// ============================================================================
// Packet decoder type guards (slice-48)
// ============================================================================

describe('isPacketEvent', () => {
  it('returns true for ax25 packet events', () => {
    const env: DecoderEventEnvelope = {
      type: 'decoder',
      decoder: 'ax25',
      receiverId: 'rx-1',
      event: {
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
      },
    };
    expect(isPacketEvent(env)).toBe(true);
  });

  it('returns false for non-ax25 decoders', () => {
    const env: DecoderEventEnvelope = {
      type: 'decoder',
      decoder: 'cw',
      receiverId: 'rx-1',
      event: { kind: 'packet', ts: 0, source: '', destination: '', digipeaters: [], control: 0, frame_type: '', info_hex: '', info_text: '', packet_index: 0 },
    };
    expect(isPacketEvent(env)).toBe(false);
  });

  it('returns false for crc_error events', () => {
    const env: DecoderEventEnvelope = {
      type: 'decoder',
      decoder: 'ax25',
      receiverId: 'rx-1',
      event: { kind: 'crc_error', ts: 0, reason: 'CRC mismatch', raw_hex: '00', length: 1 },
    };
    expect(isPacketEvent(env)).toBe(false);
  });
});

describe('isPacketCrcErrorEvent', () => {
  it('returns true for ax25 crc_error events', () => {
    const env: DecoderEventEnvelope = {
      type: 'decoder',
      decoder: 'ax25',
      receiverId: 'rx-1',
      event: { kind: 'crc_error', ts: 100, reason: 'CRC mismatch', raw_hex: 'deadbeef', length: 32 },
    };
    expect(isPacketCrcErrorEvent(env)).toBe(true);
  });

  it('returns false for packet events', () => {
    const env: DecoderEventEnvelope = {
      type: 'decoder',
      decoder: 'ax25',
      receiverId: 'rx-1',
      event: { kind: 'packet', ts: 0, source: 'X', destination: 'Y', digipeaters: [], control: 0, frame_type: 'I', info_hex: '', info_text: '', packet_index: 0 },
    };
    expect(isPacketCrcErrorEvent(env)).toBe(false);
  });
});

// ============================================================================
// ACARS / DAB / ATC type guards (slice-57)
// ============================================================================

describe('ACARS + DAB + ATC family constants', () => {
  it('ACARS_DECODERS includes acars', () => {
    expect(ACARS_DECODERS).toContain('acars');
    expect(ACARS_DECODERS).toHaveLength(1);
  });
  it('DAB_DECODERS includes dab', () => {
    expect(DAB_DECODERS).toContain('dab');
    expect(DAB_DECODERS).toHaveLength(1);
  });
  it('ATC_DECODERS includes atc', () => {
    expect(ATC_DECODERS).toContain('atc');
    expect(ATC_DECODERS).toHaveLength(1);
  });
});

describe('isAcarsMessageEvent', () => {
  it('returns true for acars message events', () => {
    const env: DecoderEventEnvelope = {
      type: 'decoder', decoder: 'acars', receiverId: 'rx-1',
      event: { kind: 'message', ts: 1, address: 'N123AB', mode: '2', ack: ' ', label: 'H1', block_id: '#', text: 'hi', raw_hex: '', message_index: 0 } as any,
    };
    expect(isAcarsMessageEvent(env)).toBe(true);
  });
  it('returns false for non-acars decoders', () => {
    const env: DecoderEventEnvelope = { type: 'decoder', decoder: 'cw', receiverId: 'rx-1', event: { kind: 'message', ts: 1 } as any };
    expect(isAcarsMessageEvent(env)).toBe(false);
  });
});

describe('isDabServiceEvent', () => {
  it('returns true for dab service events', () => {
    const env: DecoderEventEnvelope = {
      type: 'decoder', decoder: 'dab', receiverId: 'rx-1',
      event: { kind: 'service', ts: 1, service_id: 1, label: 'Radio', program_type: 0, subchannel_id: null } as any,
    };
    expect(isDabServiceEvent(env)).toBe(true);
  });
});

describe('isDabEnsembleEvent', () => {
  it('returns true for dab ensemble events', () => {
    const env: DecoderEventEnvelope = {
      type: 'decoder', decoder: 'dab', receiverId: 'rx-1',
      event: { kind: 'ensemble', ts: 1, services: [], ensemble_index: 0 } as any,
    };
    expect(isDabEnsembleEvent(env)).toBe(true);
  });
});

describe('isAtcVoiceStartEvent', () => {
  it('returns true for atc voice_start', () => {
    const env: DecoderEventEnvelope = {
      type: 'decoder', decoder: 'atc', receiverId: 'rx-1',
      event: { kind: 'voice_start', ts: 1, rssi_dbfs: -20, frequency_hz: 118000000 } as any,
    };
    expect(isAtcVoiceStartEvent(env)).toBe(true);
  });
});

describe('isAtcVoiceEndEvent', () => {
  it('returns true for atc voice_end', () => {
    const env: DecoderEventEnvelope = {
      type: 'decoder', decoder: 'atc', receiverId: 'rx-1',
      event: { kind: 'voice_end', ts: 1, rssi_dbfs: -50, frequency_hz: 118000000 } as any,
    };
    expect(isAtcVoiceEndEvent(env)).toBe(true);
  });
});

describe('isAtcRssiEvent', () => {
  it('returns true for atc rssi', () => {
    const env: DecoderEventEnvelope = {
      type: 'decoder', decoder: 'atc', receiverId: 'rx-1',
      event: { kind: 'rssi', ts: 1, rssi_dbfs: -30, frequency_hz: 118000000 } as any,
    };
    expect(isAtcRssiEvent(env)).toBe(true);
  });
});

describe('isAcarsCrcErrorEvent', () => {
  it('returns true for acars crc_error', () => {
    const env: DecoderEventEnvelope = {
      type: 'decoder', decoder: 'acars', receiverId: 'rx-1',
      event: { kind: 'crc_error', ts: 1, reason: 'bad', raw_hex: '00', length: 1 } as any,
    };
    expect(isAcarsCrcErrorEvent(env)).toBe(true);
  });
  it('returns false for message events', () => {
    const env: DecoderEventEnvelope = {
      type: 'decoder', decoder: 'acars', receiverId: 'rx-1',
      event: { kind: 'message', ts: 1, address: 'X', mode: '', ack: '', label: '', block_id: '', text: '', raw_hex: '', message_index: 0 } as any,
    };
    expect(isAcarsCrcErrorEvent(env)).toBe(false);
  });
});
