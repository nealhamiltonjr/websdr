// @vitest-environment node
import { describe, it, expect } from 'vitest';
import {
  freqAtFraction,
  fractionAtFreq,
  inSpan,
  binAtHz,
  binPowerAtHz,
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

describe('binAtHz / binPowerAtHz (slice-6.2 linked readout)', () => {
  // 8 bins across a 250 kHz span around 14.150 MHz → each bin covers 31.25 kHz.
  // bin 0 = 14.150 MHz − 125 kHz = 14.025 MHz
  // bin 7 = 14.150 MHz + 125 kHz = 14.275 MHz (right edge, clamp target)
  const BINS_8 = new Float32Array([-80, -75, -70, -65, -60, -55, -50, -45]);

  it('binAtHz returns null when binCount <= 0', () => {
    expect(binAtHz(AXIS, 14_150_000, 0)).toBeNull();
    expect(binAtHz(AXIS, 14_150_000, -1)).toBeNull();
  });

  it('binAtHz returns null when the frequency is outside the axis span', () => {
    expect(binAtHz(AXIS, 14_150_000 - 125_001, 8)).toBeNull();
    expect(binAtHz(AXIS, 14_150_000 + 125_001, 8)).toBeNull();
    expect(binAtHz(AXIS, 0, 8)).toBeNull();
  });

  it('binAtHz maps the left edge → bin 0', () => {
    expect(binAtHz(AXIS, 14_150_000 - 125_000, 8)).toBe(0);
  });

  it('binAtHz maps the center → the middle bin', () => {
    // center = 14.150 MHz, frac = 0.5, 0.5 * 8 = 4 → bin 4
    expect(binAtHz(AXIS, 14_150_000, 8)).toBe(4);
  });

  it('binAtHz clamps the right edge → last bin (not binCount)', () => {
    // right edge = 14.275 MHz, frac = 1.0, 1.0 * 8 = 8 → OOB without clamp
    expect(binAtHz(AXIS, 14_150_000 + 125_000, 8)).toBe(7);
  });

  it('binAtHz maps a frequency between bins to the floor bin index', () => {
    // frac = 0.25 (quarter-span) → 0.25 * 8 = 2 → bin 2
    expect(binAtHz(AXIS, 14_150_000 - 62_500, 8)).toBe(2);
    // frac = 0.625 → 0.625 * 8 = 5 → bin 5
    expect(binAtHz(AXIS, 14_150_000 + 31_250, 8)).toBe(5);
  });

  it('binPowerAtHz returns the bin value at the frequency', () => {
    // bin 4 → -60 dBFS at the center frequency
    expect(binPowerAtHz(AXIS, BINS_8, 14_150_000)).toBe(-60);
    // bin 0 → -80 dBFS at the left edge
    expect(binPowerAtHz(AXIS, BINS_8, 14_150_000 - 125_000)).toBe(-80);
    // bin 7 → -45 dBFS at the right edge
    expect(binPowerAtHz(AXIS, BINS_8, 14_150_000 + 125_000)).toBe(-45);
  });

  it('binPowerAtHz returns null for out-of-span frequencies', () => {
    expect(binPowerAtHz(AXIS, BINS_8, 0)).toBeNull();
    expect(binPowerAtHz(AXIS, BINS_8, 14_150_000 + 1_000_000)).toBeNull();
  });

  it('binPowerAtHz returns null when the bins array is empty', () => {
    expect(binPowerAtHz(AXIS, new Float32Array(0), 14_150_000)).toBeNull();
  });
});
