// @vitest-environment node
import { describe, it, expect } from 'vitest';
import {
  freqAtFraction,
  fractionAtFreq,
  inSpan,
  passbandFor,
  passbandAround,
  DEFAULT_PASSBAND,
  type FreqAxis,
} from './freqAxis';

const AXIS: FreqAxis = { centerHz: 14_150_000, sampleRateHz: 250_000 };

describe('freq ↔ fraction mapping', () => {
  it('left edge = center − rate/2, right edge = center + rate/2', () => {
    expect(freqAtFraction(AXIS, 0)).toBe(14_150_000 - 125_000);
    expect(freqAtFraction(AXIS, 1)).toBe(14_150_000 + 125_000);
  });

  it('center maps to 0.5', () => {
    expect(freqAtFraction(AXIS, 0.5)).toBe(14_150_000);
  });

  it('round-trips', () => {
    for (const frac of [0, 0.1, 0.25, 0.5, 0.73, 0.999, 1]) {
      const hz = freqAtFraction(AXIS, frac);
      expect(fractionAtFreq(AXIS, hz)).toBeCloseTo(frac, 9);
    }
  });

  it('fractionAtFreq clamps out-of-span frequencies into [0, 1]', () => {
    expect(fractionAtFreq(AXIS, 0)).toBe(0);
    expect(fractionAtFreq(AXIS, 1e12)).toBe(1);
  });

  it('inSpan is exact at the edges and outside', () => {
    expect(inSpan(AXIS, 14_150_000 - 125_000)).toBe(true);
    expect(inSpan(AXIS, 14_150_000 + 125_000)).toBe(true);
    expect(inSpan(AXIS, 14_150_000 - 125_001)).toBe(false);
    expect(inSpan(AXIS, 14_150_000 + 125_001)).toBe(false);
  });

  it('quarter/half/three-quarter spans are linear', () => {
    expect(freqAtFraction(AXIS, 0.75)).toBe(14_150_000 + 62_500);
    expect(freqAtFraction(AXIS, 0.25)).toBe(14_150_000 - 62_500);
  });
});

describe('passband table', () => {
  it('matches the backend _MODE_PROFILES exactly for the six demod modes', () => {
    // See apps/server/openwebrx_plus/dsp/audio.py — the shaded band in the UI
    // must show the REAL demodulator window.
    expect(passbandFor('USB')).toEqual([150, 2850]);
    expect(passbandFor('LSB')).toEqual([-2850, -150]);
    expect(passbandFor('CW')).toEqual([600, 900]);
    expect(passbandFor('AM')).toEqual([-5000, 5000]);
    expect(passbandFor('NFM')).toEqual([-6000, 6000]);
    expect(passbandFor('WFM')).toEqual([-100000, 100000]);
  });

  it('covers every frontend mode without falling back', () => {
    const modes = [
      'USB', 'LSB', 'AM', 'SAM', 'FM', 'NBFM', 'WBFM', 'CW', 'FreeDV',
      'RTTY', 'PSK31', 'PSK63', 'Olivia', 'FT8', 'JT65', 'JT9', 'WSPR',
      'Q65', 'SSTV', 'FAX', 'Packet', 'DAB', 'ADS-B', 'UAT', 'AIS', 'ATC',
      'ACARS',
    ];
    for (const m of modes) {
      expect(passbandFor(m)).not.toBe(DEFAULT_PASSBAND);
    }
  });

  it('falls back to ±3 kHz for unknown / missing modes', () => {
    expect(passbandFor('nonsense-mode')).toBe(DEFAULT_PASSBAND);
    expect(passbandFor(undefined)).toBe(DEFAULT_PASSBAND);
    expect(passbandFor(null)).toBe(DEFAULT_PASSBAND);
  });

  it('all passbands are ordered lo < hi', () => {
    const modes = ['USB', 'LSB', 'CW', 'AM', 'NFM', 'WFM', 'FT8', 'DAB', 'AIS'];
    for (const m of modes) {
      const [lo, hi] = passbandFor(m);
      expect(lo).toBeLessThan(hi);
    }
  });

  it('passbandAround shifts by the tuned frequency', () => {
    expect(passbandAround('USB', 7_200_000)).toEqual([7_200_150, 7_202_850]);
    expect(passbandAround('AM', 100_000_000)).toEqual([99_995_000, 100_005_000]);
  });
});
