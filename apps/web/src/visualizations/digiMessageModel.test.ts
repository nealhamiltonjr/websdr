/** Tests for the DigiMessageList model (slice-21).
 *
 *  Pure state-reducer tests — exercises the applyDecoderEvent fold
 *  without a DOM. Mirrors the aircraftModel.test.ts pattern.
 */
import { describe, it, expect } from 'vitest';
import {
  applyDecoderEvent,
  formatAge,
  formatMessageSummary,
  formatTime,
  initialDigiMessageState,
  MAX_MESSAGES,
} from './digiMessageModel';
import type { DecoderEventEnvelope, DigiMessageEvent } from '@openwebrx-plus/shared-types';

function makeEnvelope(
  decoder: string,
  event:
    | DigiMessageEvent
    | { kind: 'messages'; ts: number; messages: DigiMessageEvent[] }
    | { kind: 'decoder_state'; state: string; reason?: string }
    | { kind: string; [key: string]: unknown }, // generic fallback for the "ignored" case
): DecoderEventEnvelope {
  return {
    type: 'decoder',
    decoder,
    receiverId: 'rx-test',
    event: event as { kind: string; [key: string]: unknown },
  };
}

function makeMessage(overrides: Partial<DigiMessageEvent> = {}): DigiMessageEvent {
  return {
    kind: 'message',
    ts: 1700000000,
    mode: 'FT8',
    text: 'K1ABC KO51 -12',
    callsign: 'K1ABC',
    grid_locator: 'KO51',
    snr_db: -12,
    audio_offset_hz: 1500,
    slot_utc: 1700000000,
    ...overrides,
  };
}

describe('DigiMessageList model', () => {
  it('starts empty', () => {
    const s = initialDigiMessageState();
    expect(s.messages).toEqual([]);
    expect(s.messageCount).toBe(0);
    expect(s.lastMessage).toBeNull();
    expect(s.mode).toBeNull();
    expect(s.decoderState).toBeNull();
  });

  it('ignores non-FT8-family decoder events', () => {
    const env = makeEnvelope('adsb', { kind: 'aircraft', ts: 0, aircraft: [] });
    const next = applyDecoderEvent(initialDigiMessageState(), env);
    expect(next).toEqual(initialDigiMessageState());
  });

  it('handles a single message event', () => {
    const msg = makeMessage();
    const env = makeEnvelope('ft8', msg);
    const next = applyDecoderEvent(initialDigiMessageState(), env);
    expect(next.messageCount).toBe(1);
    expect(next.messages.length).toBe(1);
    expect(next.messages[0].text).toBe('K1ABC KO51 -12');
    expect(next.lastMessage).toBe('K1ABC KO51 -12');
    expect(next.mode).toBe('FT8');
  });

  it('appends new messages to the front (newest-first)', () => {
    const msg1 = makeMessage({ ts: 1700000001, text: 'MSG1' });
    const msg2 = makeMessage({ ts: 1700000002, text: 'MSG2' });
    let state = initialDigiMessageState();
    state = applyDecoderEvent(state, makeEnvelope('ft8', msg1));
    state = applyDecoderEvent(state, makeEnvelope('ft8', msg2));
    expect(state.messages.length).toBe(2);
    expect(state.messages[0].text).toBe('MSG2'); // newest first
    expect(state.messages[1].text).toBe('MSG1');
    expect(state.messageCount).toBe(2);
  });

  it('caps the buffer at MAX_MESSAGES (ring buffer)', () => {
    let state = initialDigiMessageState();
    for (let i = 0; i < MAX_MESSAGES + 10; i++) {
      const msg = makeMessage({ ts: 1700000000 + i, text: `MSG${i}` });
      state = applyDecoderEvent(state, makeEnvelope('ft8', msg));
    }
    expect(state.messages.length).toBe(MAX_MESSAGES);
    expect(state.messageCount).toBe(MAX_MESSAGES + 10);
    // The newest message is at the front.
    expect(state.messages[0].text).toBe(`MSG${MAX_MESSAGES + 9}`);
  });

  it('handles a messages snapshot (server-side ring buffer)', () => {
    const msgs = [makeMessage({ ts: 1, text: 'A' }), makeMessage({ ts: 2, text: 'B' })];
    const env = makeEnvelope('ft8', { kind: 'messages', ts: 2, messages: msgs });
    const next = applyDecoderEvent(initialDigiMessageState(), env);
    expect(next.messages.length).toBe(2);
    expect(next.messages[0].text).toBe('A');
    expect(next.lastMessage).toBe('A');
  });

  it('handles decoder_state lifecycle events', () => {
    const env = makeEnvelope('ft8', {
      kind: 'decoder_state',
      state: 'failed',
      reason: 'demodulator_not_implemented',
    });
    const next = applyDecoderEvent(initialDigiMessageState(), env);
    expect(next.decoderState?.state).toBe('failed');
    expect(next.decoderState?.reason).toBe('demodulator_not_implemented');
  });

  it('preserves messageCount across snapshot events (max)', () => {
    // Pre-populate messageCount via per-message events.
    let state = initialDigiMessageState();
    for (let i = 0; i < 5; i++) {
      state = applyDecoderEvent(
        state,
        makeEnvelope('ft8', makeMessage({ ts: i, text: `MSG${i}` })),
      );
    }
    expect(state.messageCount).toBe(5);
    // Now a snapshot arrives with only 3 messages (server ring buffer
    // dropped some). messageCount stays at 5 (max — we don't lose history).
    const env = makeEnvelope('ft8', {
      kind: 'messages',
      ts: 10,
      messages: [makeMessage({ ts: 8, text: 'A' }), makeMessage({ ts: 9, text: 'B' }), makeMessage({ ts: 10, text: 'C' })],
    });
    state = applyDecoderEvent(state, env);
    expect(state.messageCount).toBe(5);
    expect(state.messages.length).toBe(3);
  });
});

describe('DigiMessageList formatters', () => {
  it('formatMessageSummary composes mode + SNR + offset + text', () => {
    const msg = makeMessage();
    const summary = formatMessageSummary(msg);
    expect(summary).toContain('FT8');
    expect(summary).toContain('-12 dB');
    expect(summary).toContain('1500 Hz');
    expect(summary).toContain('K1ABC KO51 -12');
  });

  it('formatMessageSummary omits missing optional fields', () => {
    const msg = makeMessage({ snr_db: undefined, audio_offset_hz: undefined });
    const summary = formatMessageSummary(msg);
    expect(summary).toContain('FT8');
    expect(summary).not.toContain('dB');
    expect(summary).not.toContain('Hz');
    expect(summary).toContain('K1ABC KO51 -12');
  });

  it('formatTime produces HH:MM:SSZ UTC', () => {
    // 1700000000 = 2023-11-14T22:13:20Z
    const out = formatTime(1700000000);
    expect(out).toMatch(/^\d{2}:\d{2}:\d{2}Z$/);
    expect(out).toBe('22:13:20Z');
  });

  it('formatAge produces human-friendly age strings', () => {
    const now = 1700000100;
    expect(formatAge(now, now)).toBe('0s');
    expect(formatAge(now, now - 5)).toBe('5s');
    expect(formatAge(now, now - 60)).toBe('1m');
    expect(formatAge(now, now - 3600)).toBe('1h');
  });

  it('formatAge clamps negative ages to 0s', () => {
    const now = 1700000100;
    expect(formatAge(now, now + 10)).toBe('0s');
  });
});
