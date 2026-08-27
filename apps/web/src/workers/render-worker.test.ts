// @vitest-environment node
/** Tests for the render worker message protocol — pure type-level checks.

  These tests verify the protocol surface compiles + the message kinds
  cover the four operations (init/fft/resize/dispose). The actual worker
  runtime (OffscreenCanvas + WebGL2 in a Worker scope) isn't reachable
  from a vitest node env; the protocol checks + the file's tsc-clean
  compile are the unit-level coverage. E2E verification lives in the
  agent-browser suite.
*/

import { describe, it, expect } from 'vitest';
import type { RenderWorkerMessage } from './render-worker';

describe('RenderWorkerMessage protocol (slice-11)', () => {
  it('init carries canvas + config', () => {
    const msg: RenderWorkerMessage = {
      kind: 'init',
      canvas: {} as OffscreenCanvas,  // type-only test
      config: { minDb: -100, maxDb: -20, colorMap: 'turbo', historyRows: 1024 },
    };
    expect(msg.kind).toBe('init');
    expect(msg.config.historyRows).toBe(1024);
  });

  it('fft carries bins + centerFreq + sampleRate', () => {
    const bins = new Float32Array(512);
    const msg: RenderWorkerMessage = {
      kind: 'fft',
      bins,
      centerFreq: 14_150_000,
      sampleRate: 2_400_000,
    };
    expect(msg.kind).toBe('fft');
    expect(msg.bins.length).toBe(512);
    expect(msg.centerFreq).toBe(14_150_000);
  });

  it('resize carries width + height', () => {
    const msg: RenderWorkerMessage = { kind: 'resize', width: 800, height: 400 };
    expect(msg.kind).toBe('resize');
    expect(msg.width).toBe(800);
    expect(msg.height).toBe(400);
  });

  it('dispose is a sentinel-only message', () => {
    const msg: RenderWorkerMessage = { kind: 'dispose' };
    expect(msg.kind).toBe('dispose');
  });

  it('the four message kinds cover the full protocol', () => {
    const kinds: RenderWorkerMessage['kind'][] = ['init', 'fft', 'resize', 'dispose'];
    expect(new Set(kinds).size).toBe(4);
  });
});
