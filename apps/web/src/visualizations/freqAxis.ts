// @vitest-environment node
/** Frequency-axis math for FFT visualizations (slice-4.6).
 *
 *  The FFT frames are FftSwap'd (bin 0 = center − rate/2, last bin =
 *  center + rate/2), and both WebGL renderers stretch the bins edge-to-edge
 *  across the canvas. This module owns the freq ↔ pixel-fraction mapping and
 *  the per-mode passband table used by the tuned-frequency marker.
 *
 *  Pure TypeScript — no DOM, no WebGL — so it is unit-testable in node.
 */

/** The display axis of one FFT frame. */
export interface FreqAxis {
  /** Center frequency of the captured span, Hz. */
  centerHz: number;
  /** Sample rate of the captured span, Hz (full span = center ± rate/2). */
  sampleRateHz: number;
}

/** Frequency at a pixel fraction (0 = left edge, 1 = right edge). */
export function freqAtFraction(axis: FreqAxis, frac: number): number {
  return axis.centerHz + (frac - 0.5) * axis.sampleRateHz;
}

/** Pixel fraction of a frequency, clamped into [0, 1]. */
export function fractionAtFreq(axis: FreqAxis, hz: number): number {
  const f = (hz - axis.centerHz) / axis.sampleRateHz + 0.5;
  return Math.max(0, Math.min(1, f));
}

/** Whether a frequency falls inside the axis span (unclamped). */
export function inSpan(axis: FreqAxis, hz: number): boolean {
  const half = axis.sampleRateHz / 2;
  return hz >= axis.centerHz - half && hz <= axis.centerHz + half;
}

/**
 * FFT bin index for a given frequency.
 *
 * The FFT frames are FftSwap'd (bin 0 = center − rate/2, last bin =
 * center + rate/2). `binCount` is the number of bins in the Float32Array
 * (frame.bins.length). Returns null when `hz` is outside the axis span
 * — callers should treat that as "no reading" rather than clamping,
 * because clamping would silently report the wrong frequency's level.
 *
 * Slice-6.2 — used by the S-Meter / Frequency Counter linked readout so
 * they can show the signal level / frequency AT the cursor position
 * (when the user hovers over a sibling FFT canvas).
 */
export function binAtHz(axis: FreqAxis, hz: number, binCount: number): number | null {
  if (binCount <= 0) return null;
  if (!inSpan(axis, hz)) return null;
  const frac = fractionAtFreq(axis, hz); // [0, 1] across the axis span
  // Map [0, 1) → [0, binCount). The `min` guard prevents returning binCount
  // at exactly the right edge (frac === 1 → binCount is out-of-bounds).
  return Math.min(binCount - 1, Math.floor(frac * binCount));
}

/**
 * Read the FFT bin power (dBFS) at a given frequency.
 *
 * Returns null when the frequency is outside the axis span OR when the
 * bins array is empty / not yet arrived (the first frame carries a
 * valid FreqAxis but might be replaced before the S-Meter renders).
 */
export function binPowerAtHz(axis: FreqAxis, bins: Float32Array, hz: number): number | null {
  const idx = binAtHz(axis, hz, bins.length);
  if (idx === null) return null;
  return bins[idx];
}

/** A passband as offsets from the tuned frequency, [loHz, hiHz]. */
export type Passband = readonly [number, number];

/**
 * Per-mode receive passband, as offsets from the tuned frequency.
 *
 * The six modes the backend's AudioChain actually demodulates (see
 * apps/server/openwebrx_plus/dsp/audio.py `_MODE_PROFILES`) match the backend
 * exactly — the shaded band in the UI then shows the real demodulator window.
 * The remaining frontend modes (decoder taps, digital modes) get sensible
 * display defaults.
 */
const PASSBANDS: Partial<Record<string, Passband>> = {
  // --- backend _MODE_PROFILES (exact; the backend mode is a free string —
  //     it sends NFM/WFM, the frontend type union says NBFM/WBFM — cover both) ---
  USB: [150, 2850],
  LSB: [-2850, -150],
  CW: [600, 900],
  AM: [-5000, 5000],
  NFM: [-6000, 6000],
  WFM: [-100000, 100000],
  // --- display defaults for the rest ---
  SAM: [-5000, 5000],
  FM: [-6000, 6000],
  NBFM: [-6000, 6000],
  WBFM: [-100000, 100000],
  FreeDV: [-1500, 1500],
  RTTY: [-600, 600],
  PSK31: [-1000, 1000],
  PSK63: [-1000, 1000],
  Olivia: [-1000, 1000],
  FT8: [-1500, 1500],
  JT65: [-1500, 1500],
  JT9: [-1500, 1500],
  WSPR: [-1500, 1500],
  Q65: [-1500, 1500],
  SSTV: [-1500, 1500],
  FAX: [-1500, 1500],
  Packet: [-1500, 1500],
  DAB: [-760000, 760000],
  'ADS-B': [-1000000, 1000000],
  UAT: [-500000, 500000],
  AIS: [-12500, 12500],
  ATC: [-12500, 12500],
  ACARS: [-12500, 12500],
};

/** Default passband for unknown/unlisted modes: ±3 kHz (a wide SSB window). */
export const DEFAULT_PASSBAND: Passband = [-3000, 3000];

/** Passband offsets for a mode (never throws — falls back to ±3 kHz). */
export function passbandFor(mode: string | undefined | null): Passband {
  if (mode == null) return DEFAULT_PASSBAND;
  return PASSBANDS[mode] ?? DEFAULT_PASSBAND;
}

/** Absolute passband [loHz, hiHz] around a tuned frequency. */
export function passbandAround(mode: string | undefined | null, tunedHz: number): Passband {
  const [lo, hi] = passbandFor(mode);
  return [tunedHz + lo, tunedHz + hi];
}
