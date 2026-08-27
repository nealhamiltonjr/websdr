// @vitest-environment node
import { describe, it, expect } from 'vitest';
import { DSP_MODE_OPTIONS, DEFAULT_GAIN_RANGE, formatGainDb } from './tuningControls';

describe('formatGainDb', () => {
  it('formats manual gains with an explicit sign and one decimal', () => {
    expect(formatGainDb(6)).toBe('+6.0 dB');
    expect(formatGainDb(-3.5)).toBe('-3.5 dB');
    expect(formatGainDb(0)).toBe('+0.0 dB');
    expect(formatGainDb(32.5)).toBe('+32.5 dB');
  });

  it('formats auto (null) as "auto"', () => {
    expect(formatGainDb(null)).toBe('auto');
  });
});

describe('DSP_MODE_OPTIONS (ADR-002 surface)', () => {
  it('lists all four modes in the ADR order', () => {
    expect(DSP_MODE_OPTIONS.map((o) => o.value)).toEqual(['raw', 'classic', 'ai', 'cascade']);
  });

  it('raw + classic are available; ai + cascade are gated on DeepFilterNet', () => {
    const byValue = Object.fromEntries(DSP_MODE_OPTIONS.map((o) => [o.value, o]));
    expect(byValue.raw.available).toBe(true);
    expect(byValue.classic.available).toBe(true);
    expect(byValue.ai.available).toBe(false);
    expect(byValue.cascade.available).toBe(false);
    // The disabled options must explain WHY (shown as the tooltip).
    expect(byValue.ai.hint).toMatch(/DeepFilterNet/);
    expect(byValue.cascade.hint).toMatch(/ADR-002/);
  });
});

describe('DEFAULT_GAIN_RANGE', () => {
  it('covers the RTL-SDR-class 0–49 dB span', () => {
    expect(DEFAULT_GAIN_RANGE).toEqual([0, 49]);
  });
});
