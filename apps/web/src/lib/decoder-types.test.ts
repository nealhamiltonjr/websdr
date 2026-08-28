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
  isTextCharEvent,
  isTextSnapshotEvent,
  isImageEvent,
  isImageScanlineEvent,
  isImageModeEvent,
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

  it('IMAGE_DECODERS includes sstv', () => {
    expect(IMAGE_DECODERS).toContain('sstv');
    expect(IMAGE_DECODERS).toHaveLength(1);
  });

  it('ADSB_DECODERS unchanged', () => {
    expect(ADSB_DECODERS).toEqual(['adsb', 'dump1090', 'dump978']);
  });

  it('AIS_DECODERS unchanged', () => {
    expect(AIS_DECODERS).toEqual(['ais']);
  });

  it('DIGI_MESSAGE_DECODERS unchanged', () => {
    expect(DIGI_MESSAGE_DECODERS).toEqual(['ft8']);
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
