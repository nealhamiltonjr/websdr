/** Tests for the DabServiceListModel (slice-56). */

import { describe, it, expect } from 'vitest';
import {
  applyDecoderEvent,
  formatAge,
  formatTime,
  initialDabServiceListState,
} from './dabServiceListModel';
import type { DecoderEventEnvelope } from '@openwebrx-plus/shared-types';

function makeEnv(event: Record<string, unknown>): DecoderEventEnvelope {
  return { type: 'decoder', decoder: 'dab', receiverId: 'rx-1', event: event as any };
}

describe('applyDecoderEvent', () => {
  it('adds a service on service event', () => {
    const state = initialDabServiceListState();
    const env = makeEnv({ kind: 'service', ts: 100, service_id: 0x12345678, label: 'BBC R1', program_type: 5, subchannel_id: null });
    const next = applyDecoderEvent(state, env);
    expect(next.services).toHaveLength(1);
    expect(next.services[0].label).toBe('BBC R1');
    expect(next.services[0].service_id).toBe(0x12345678);
    expect(next.last_update).toBe(100);
  });

  it('updates an existing service by ID', () => {
    let state = initialDabServiceListState();
    state = applyDecoderEvent(state, makeEnv({ kind: 'service', ts: 1, service_id: 1, label: 'A', program_type: 0, subchannel_id: null }));
    state = applyDecoderEvent(state, makeEnv({ kind: 'service', ts: 2, service_id: 1, label: 'B', program_type: 3, subchannel_id: null }));
    expect(state.services).toHaveLength(1);
    expect(state.services[0].label).toBe('B');
    expect(state.services[0].program_type).toBe(3);
  });

  it('tracks ensemble_index on ensemble events', () => {
    let state = initialDabServiceListState();
    state = applyDecoderEvent(state, makeEnv({ kind: 'ensemble', ts: 5, services: [], ensemble_index: 3 }));
    expect(state.ensemble_index).toBe(3);
  });

  it('ignores non-dab decoders', () => {
    const state = initialDabServiceListState();
    const env: DecoderEventEnvelope = { type: 'decoder', decoder: 'cw', receiverId: 'rx-1', event: { kind: 'service', ts: 1 } as any };
    const next = applyDecoderEvent(state, env);
    expect(next).toBe(state);
  });
});

describe('formatTime', () => {
  it('formats a timestamp', () => {
    expect(formatTime(1704067200)).toMatch(/^\d{2}:\d{2}:\d{2}Z$/);
  });
  it('returns placeholder for zero', () => {
    expect(formatTime(0)).toBe('--:--:--');
  });
});

describe('formatAge', () => {
  it('formats age in seconds', () => {
    expect(formatAge(100, 110)).toBe('10s');
  });
  it('returns placeholder for zero', () => {
    expect(formatAge(0, 100)).toBe('--');
  });
});
