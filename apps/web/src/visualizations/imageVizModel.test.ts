/** Tests for the ImageViz model (slice-46).
 *
 *  Tests the pure-logic model: applyDecoderEvent, decodeBase64ToBytes,
 *  formatTime, formatAge, progressPercent. No SolidJS rendering.
 */

import { describe, it, expect } from 'vitest';
import {
  applyDecoderEvent,
  decodeBase64ToBytes,
  formatAge,
  formatTime,
  initialImageVizState,
  progressPercent,
  type ImageVizState,
} from './imageVizModel';
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

describe('applyDecoderEvent', () => {
  it('stores image on image event', () => {
    const state = initialImageVizState();
    const env = makeEnvelope('sstv', {
      kind: 'image',
      ts: 100,
      mode: 'SCOTTIE_1',
      width: 320,
      height: 256,
      data: 'AAAA',
      image_index: 0,
    });
    const next = applyDecoderEvent(state, env);
    expect(next.currentImage).not.toBeNull();
    expect(next.currentImage!.width).toBe(320);
    expect(next.currentImage!.height).toBe(256);
    expect(next.imageCount).toBe(1);
    expect(next.scanlineProgress).toBe(0);
    expect(next.lastUpdate).toBe(100);
    expect(next.decoderName).toBe('sstv');
  });

  it('updates scanline progress on scanline event', () => {
    const state = initialImageVizState();
    const env = makeEnvelope('sstv', { kind: 'scanline', ts: 50, scanline: 42 });
    const next = applyDecoderEvent(state, env);
    expect(next.scanlineProgress).toBe(42);
    expect(next.lastUpdate).toBe(50);
  });

  it('stores mode on mode event', () => {
    const state = initialImageVizState();
    const env = makeEnvelope('sstv', {
      kind: 'mode',
      ts: 10,
      mode: 'MARTIN_1',
      vis_code: 44,
    });
    const next = applyDecoderEvent(state, env);
    expect(next.mode).toBe('MARTIN_1');
    expect(next.visCode).toBe(44);
    expect(next.scanlineProgress).toBe(0);
  });

  it('increments image count on subsequent images', () => {
    let state = initialImageVizState();
    const makeImg = (idx: number) =>
      makeEnvelope('sstv', {
        kind: 'image',
        ts: idx * 100,
        mode: 'SCOTTIE_1',
        width: 320,
        height: 256,
        data: 'AAAA',
        image_index: idx,
      });
    state = applyDecoderEvent(state, makeImg(0));
    state = applyDecoderEvent(state, makeImg(1));
    state = applyDecoderEvent(state, makeImg(2));
    expect(state.imageCount).toBe(3);
    expect(state.currentImage!.image_index).toBe(2);
  });

  it('ignores non-image-decoder events', () => {
    const state = initialImageVizState();
    const env = makeEnvelope('cw', { kind: 'image', ts: 1 });
    const next = applyDecoderEvent(state, env);
    expect(next).toBe(state);
  });

  it('ignores unknown event kinds', () => {
    const state = initialImageVizState();
    const env = makeEnvelope('sstv', { kind: 'unknown', ts: 1 });
    const next = applyDecoderEvent(state, env);
    expect(next).toBe(state);
  });
});

describe('decodeBase64ToBytes', () => {
  it('decodes a simple base64 string', () => {
    // "ABC" in base64 is "QUJD"
    const bytes = decodeBase64ToBytes('QUJD');
    expect(bytes.length).toBe(3);
    expect(bytes[0]).toBe(65); // A
    expect(bytes[1]).toBe(66); // B
    expect(bytes[2]).toBe(67); // C
  });

  it('decodes empty string to empty array', () => {
    const bytes = decodeBase64ToBytes('');
    expect(bytes.length).toBe(0);
  });

  it('decodes RGB pixel data correctly', () => {
    // 3 bytes: R=255, G=0, B=128 → base64 "/wCA"
    const bytes = decodeBase64ToBytes('/wCA');
    expect(bytes.length).toBe(3);
    expect(bytes[0]).toBe(255);
    expect(bytes[1]).toBe(0);
    expect(bytes[2]).toBe(128);
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

describe('progressPercent', () => {
  it('returns 0 when no scanlines decoded', () => {
    const state: ImageVizState = {
      ...initialImageVizState(),
      scanlineProgress: 0,
      mode: 'SCOTTIE_1',
    };
    expect(progressPercent(state)).toBe(0);
  });

  it('returns 0 when no mode detected', () => {
    const state: ImageVizState = {
      ...initialImageVizState(),
      scanlineProgress: 50,
      mode: null,
    };
    expect(progressPercent(state)).toBe(0);
  });

  it('computes percentage for SCOTTIE_1 (256 lines)', () => {
    const state: ImageVizState = {
      ...initialImageVizState(),
      scanlineProgress: 128,
      mode: 'SCOTTIE_1',
    };
    expect(progressPercent(state)).toBe(50);
  });

  it('computes percentage for ROBOT_36 (240 lines)', () => {
    const state: ImageVizState = {
      ...initialImageVizState(),
      scanlineProgress: 120,
      mode: 'ROBOT_36',
    };
    expect(progressPercent(state)).toBe(50);
  });

  it('caps at 100%', () => {
    const state: ImageVizState = {
      ...initialImageVizState(),
      scanlineProgress: 300,
      mode: 'SCOTTIE_1',
    };
    expect(progressPercent(state)).toBe(100);
  });
});

describe('initialImageVizState', () => {
  it('returns empty state', () => {
    const state = initialImageVizState();
    expect(state.currentImage).toBeNull();
    expect(state.imageCount).toBe(0);
    expect(state.scanlineProgress).toBe(0);
    expect(state.mode).toBeNull();
    expect(state.visCode).toBeNull();
    expect(state.lastUpdate).toBe(0);
    expect(state.decoderName).toBeNull();
  });
});
