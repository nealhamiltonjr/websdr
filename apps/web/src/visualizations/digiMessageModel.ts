/** DigiMessageList model — pure state reducer over FT8 / audio-band decoder events.
 *
 *  Extracted from the component so vitest can exercise the event → state
 *  fold without a DOM (same pattern as aircraftModel.ts).
 *
 *  Accepts FT8-family decoder events (any DIGI_MESSAGE_DECODERS member).
 *  Tracks:
 *    - The most recent messages (ring buffer, default 50)
 *    - The total message count (for the header counter)
 *    - The most recent message text (for the "latest" banner)
 *    - The active mode (FT8 / FT4 / WSPR / etc.)
 *    - Subprocess lifecycle state (when the decoder is a subprocess)
 */

import {
  DIGI_MESSAGE_DECODERS,
  type DecoderEventEnvelope,
  type DigiMessageEvent,
  type DigiMessageListEvent,
} from '@openwebrx-plus/shared-types';

const digiDecoderNames: readonly string[] = DIGI_MESSAGE_DECODERS;

/** Maximum messages to retain (ring buffer). */
export const MAX_MESSAGES = 50;

export interface DigiMessageFeedState {
  /** Recent messages, newest first (the server sorts; we cap at MAX_MESSAGES). */
  messages: DigiMessageEvent[];
  /** Total messages seen (for the header counter — may exceed MAX_MESSAGES). */
  messageCount: number;
  /** The most recent message text — for the "latest decode" banner. */
  lastMessage: string | null;
  /** The active mode (e.g. "FT8", "FT4", "WSPR"). */
  mode: string | null;
  /** Subprocess lifecycle (null while healthy/unknown). */
  decoderState: { state: string; reason?: string } | null;
}

export function initialDigiMessageState(): DigiMessageFeedState {
  return {
    messages: [],
    messageCount: 0,
    lastMessage: null,
    mode: null,
    decoderState: null,
  };
}

/** Fold one decoder envelope into the feed state (ignores non-digi-mode). */
export function applyDecoderEvent(
  state: DigiMessageFeedState,
  envelope: DecoderEventEnvelope,
): DigiMessageFeedState {
  if (!digiDecoderNames.includes(envelope.decoder)) return state;
  const event = envelope.event;

  // "messages" snapshot — server sends a fresh snapshot with the new
  // message appended (server-side ring buffer). Just adopt it.
  if (event.kind === 'messages') {
    const snapshot = event as unknown as DigiMessageListEvent;
    const messages = snapshot.messages ?? [];
    return {
      ...state,
      messages,
      messageCount: Math.max(state.messageCount, messages.length),
      lastMessage: messages.length > 0 ? messages[0].text : state.lastMessage,
      mode: messages.length > 0 ? messages[0].mode : state.mode,
    };
  }

  // "message" — single new message. Append to our local ring buffer.
  if (event.kind === 'message') {
    const msg = event as unknown as DigiMessageEvent;
    const newMessages = [msg, ...state.messages].slice(0, MAX_MESSAGES);
    return {
      ...state,
      messages: newMessages,
      messageCount: state.messageCount + 1,
      lastMessage: msg.text,
      mode: msg.mode,
    };
  }

  // Subprocess lifecycle.
  if (event.kind === 'decoder_state') {
    const ev = event as unknown as { state: string; reason?: string };
    return { ...state, decoderState: { state: ev.state, reason: ev.reason } };
  }

  return state;
}

/** "FT8 · K1ABC KO51 -12 · -12 dB · 1500 Hz" — one-line summary for the
 *  "latest decode" banner. */
export function formatMessageSummary(msg: DigiMessageEvent): string {
  const parts: string[] = [msg.mode];
  if (msg.snr_db !== undefined) parts.push(`${msg.snr_db} dB`);
  if (msg.audio_offset_hz !== undefined) {
    parts.push(`${Math.round(msg.audio_offset_hz)} Hz`);
  }
  parts.push('·');
  parts.push(msg.text);
  return parts.join(' ');
}

/** Format a Unix timestamp as HH:MM:SS UTC. */
export function formatTime(ts: number): string {
  const d = new Date(ts * 1000);
  return d.toISOString().slice(11, 19) + 'Z';
}

/** Age column: "2s", "45s", "3m" (mirrors aircraftModel.formatAge). */
export function formatAge(now: number, then: number): string {
  const s = Math.max(0, Math.round(now - then));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h`;
}
