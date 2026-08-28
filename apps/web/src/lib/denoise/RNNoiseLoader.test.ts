/**
 * Tests for the RNNoise WASM loader (slice-19).
 *
 * Covers the not-deployed path (the WASM package isn't shipped in the
 * repo — operators build it separately per packages/rnnoise-wasm/
 * README.md). When the WASM IS available (deployed to public/pkg/),
 * the same tests verify the load + createDenoiser surface.
 *
 * Mirrors the slice-18 test pattern (test_ai_denoise_rust.py):
 * module imports cleanly, loader returns null gracefully, create
 * path returns null when unavailable, cache works.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import {
  loadRNNoiseModule,
  createDenoiser,
  isRNNoiseAvailable,
  isRNNoiseLoadAttempted,
  resetRNNoiseCache,
  RNNOISE_FRAME_SIZE,
  RNNOISE_SAMPLE_RATE,
} from './RNNoiseLoader';

describe('RNNoiseLoader', () => {
  beforeEach(() => {
    resetRNNoiseCache();
  });

  it('exposes the public API surface', () => {
    expect(typeof loadRNNoiseModule).toBe('function');
    expect(typeof createDenoiser).toBe('function');
    expect(typeof isRNNoiseAvailable).toBe('function');
    expect(typeof isRNNoiseLoadAttempted).toBe('function');
    expect(typeof resetRNNoiseCache).toBe('function');
    expect(RNNOISE_FRAME_SIZE).toBe(480);
    expect(RNNOISE_SAMPLE_RATE).toBe(48000);
  });

  it('reports "not attempted" before any load call', () => {
    expect(isRNNoiseLoadAttempted()).toBe(false);
    expect(isRNNoiseAvailable()).toBe(false);
  });

  it('returns null when the WASM package is not deployed', async () => {
    // In the test env there's no /pkg/rnnoise_wasm.js — the fetch
    // HEAD probe fails (404 or network error in jsdom), the loader
    // treats this as not-deployed.
    const mod = await loadRNNoiseModule();
    expect(mod).toBeNull();
    expect(isRNNoiseLoadAttempted()).toBe(true);
    expect(isRNNoiseAvailable()).toBe(false);
  });

  it('caches the not-deployed result (no repeat probing)', async () => {
    const first = await loadRNNoiseModule();
    const second = await loadRNNoiseModule();
    expect(first).toBe(second);
    expect(first).toBeNull();
  });

  it('createDenoiser returns null when not loaded', () => {
    // Reset cache to ensure we haven't probed.
    resetRNNoiseCache();
    const d = createDenoiser();
    expect(d).toBeNull();
  });

  it('resetRNNoiseCache clears the cache (allows re-probing)', async () => {
    await loadRNNoiseModule();
    expect(isRNNoiseLoadAttempted()).toBe(true);
    resetRNNoiseCache();
    expect(isRNNoiseLoadAttempted()).toBe(false);
  });

  it('constants match the RNNoise canonical defaults', () => {
    // RNNoise ships with frame_size=480 baked in (matching
    // DeepFilterNet's frame_size for cross-vendor compatibility).
    // Sample rate is 48 kHz (the rate the RNN was trained on).
    expect(RNNOISE_FRAME_SIZE).toBe(480);
    expect(RNNOISE_SAMPLE_RATE).toBe(48000);
  });
});

describe('RNNoiseDenoiser stub behavior (not-loaded path)', () => {
  beforeEach(() => {
    resetRNNoiseCache();
  });

  it('createDenoiser returns null when no module is loaded', () => {
    expect(createDenoiser()).toBeNull();
    expect(createDenoiser(8000)).toBeNull();
    expect(createDenoiser(48000)).toBeNull();
  });
});
