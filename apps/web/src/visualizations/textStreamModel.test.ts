/** Tests for the TextStreamViz model (slice-44).
 *
 *  Tests the pure-logic model: applyDecoderEvent, formatTime, formatAge,
 *  truncateText. No SolidJS rendering — just state transitions.
 */

import { describe, it, expect } from 'vitest';
import {
  applyDecoderEvent,
  formatAge,
  formatTime,
  initialTextStreamState,
  truncateText,
} from './textStreamModel';
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
  it('appends character on cw frame event', () => {
    const state = initialTextStreamState();
    const env = makeEnvelope('cw', { kind: 'frame', ts: 100, char: 'C' });
    const next = applyDecoderEvent(state, env);
    expect(next.text).toBe('C');
    expect(next.charCount).toBe(1);
    expect(next.lastUpdate).toBe(100);
    expect(next.decoderName).toBe('cw');
  });

  it('appends multiple characters in sequence', () => {
    let state = initialTextStreamState();
    state = applyDecoderEvent(state, makeEnvelope('rtty', { kind: 'frame', ts: 1, char: 'H' }));
    state = applyDecoderEvent(state, makeEnvelope('rtty', { kind: 'frame', ts: 2, char: 'I' }));
    expect(state.text).toBe('HI');
    expect(state.charCount).toBe(2);
    expect(state.decoderName).toBe('rtty');
  });

  it('replaces text on snapshot event', () => {
    let state = initialTextStreamState();
    state = applyDecoderEvent(state, makeEnvelope('psk31', { kind: 'frame', ts: 1, char: 'X' }));
    expect(state.text).toBe('X');
    state = applyDecoderEvent(state, makeEnvelope('psk31', { kind: 'text', ts: 2, text: 'HELLO' }));
    expect(state.text).toBe('HELLO');
    expect(state.charCount).toBe(5);
  });

  it('ignores non-text-decoder events', () => {
    const state = initialTextStreamState();
    const env = makeEnvelope('adsb', { kind: 'frame', ts: 1, icao: 'AABBCC' });
    const next = applyDecoderEvent(state, env);
    expect(next).toBe(state); // unchanged
  });

  it('ignores unknown event kinds', () => {
    const state = initialTextStreamState();
    const env = makeEnvelope('cw', { kind: 'unknown', ts: 1 });
    const next = applyDecoderEvent(state, env);
    expect(next).toBe(state);
  });

  it('handles newline characters', () => {
    let state = initialTextStreamState();
    state = applyDecoderEvent(state, makeEnvelope('cw', { kind: 'frame', ts: 1, char: 'A' }));
    state = applyDecoderEvent(state, makeEnvelope('cw', { kind: 'frame', ts: 2, char: '\n' }));
    state = applyDecoderEvent(state, makeEnvelope('cw', { kind: 'frame', ts: 3, char: 'B' }));
    expect(state.text).toBe('A\nB');
    expect(state.charCount).toBe(3);
  });
});

describe('formatTime', () => {
  it('formats a timestamp as HH:MM:SS UTC', () => {
    // 2024-01-01T00:00:00Z = 1704067200
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

  it('formats age in hours', () => {
    expect(formatAge(100, 3700)).toBe('1h');
  });

  it('returns placeholder for zero timestamp', () => {
    expect(formatAge(0, 100)).toBe('--');
  });
});

describe('truncateText', () => {
  it('returns short text unchanged', () => {
    expect(truncateText('hello', 10)).toBe('hello');
  });

  it('truncates long text keeping the tail', () => {
    const text = 'a'.repeat(100);
    const result = truncateText(text, 10);
    expect(result).toBe('…' + 'a'.repeat(9));
    expect(result.length).toBe(10);
  });
});

describe('initialTextStreamState', () => {
  it('returns empty state', () => {
    const state = initialTextStreamState();
    expect(state.text).toBe('');
    expect(state.charCount).toBe(0);
    expect(state.lastUpdate).toBe(0);
    expect(state.decoderName).toBeNull();
  });
});
