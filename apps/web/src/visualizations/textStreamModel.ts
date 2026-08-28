/** TextStreamModel — pure-logic model for the TextStreamViz component
 *  (slice-44).
 *
 *  Subscribes to TEXT_DECODERS family events (cw / rtty / psk31 / olivia)
 *  and maintains a scrolling text buffer. The model is testable without
 *  SolidJS — it's a pure function from (state, event) → state.
 */

import type { DecoderEventEnvelope, TextCharEvent, TextSnapshotEvent } from '@openwebrx-plus/shared-types';
import { TEXT_DECODERS } from '@openwebrx-plus/shared-types';

const textDecoderNames: readonly string[] = TEXT_DECODERS;

export interface TextStreamState {
  /** The full accumulated decoded text. */
  text: string;
  /** Number of characters decoded (including non-printable). */
  charCount: number;
  /** Timestamp of the last update. */
  lastUpdate: number;
  /** The decoder name (cw / rtty / psk31 / olivia). */
  decoderName: string | null;
}

export function initialTextStreamState(): TextStreamState {
  return {
    text: '',
    charCount: 0,
    lastUpdate: 0,
    decoderName: null,
  };
}

/** Apply a decoder event to the text stream state.
 *
 *  - TextCharEvent (kind='frame'): append the character to the buffer.
 *  - TextSnapshotEvent (kind='text'): replace the buffer with the snapshot.
 *  - Other events: ignore.
 */
export function applyDecoderEvent(
  state: TextStreamState,
  envelope: DecoderEventEnvelope,
): TextStreamState {
  if (!textDecoderNames.includes(envelope.decoder)) {
    return state;
  }
  const event = envelope.event;
  if (event.kind === 'frame') {
    const charEvent = event as unknown as TextCharEvent;
    const char = charEvent.char;
    return {
      text: state.text + char,
      charCount: state.charCount + 1,
      lastUpdate: charEvent.ts,
      decoderName: envelope.decoder,
    };
  }
  if (event.kind === 'text') {
    const snapEvent = event as unknown as TextSnapshotEvent;
    return {
      text: snapEvent.text,
      charCount: snapEvent.text.length,
      lastUpdate: snapEvent.ts,
      decoderName: envelope.decoder,
    };
  }
  return state;
}

/** Format a timestamp as HH:MM:SS UTC. */
export function formatTime(ts: number): string {
  if (!ts) return '--:--:--';
  const d = new Date(ts * 1000);
  return d.toISOString().slice(11, 19) + 'Z';
}

/** Format the age of the last update in seconds. */
export function formatAge(ts: number, now: number): string {
  if (!ts) return '--';
  const age = Math.max(0, Math.floor(now - ts));
  if (age < 60) return `${age}s`;
  if (age < 3600) return `${Math.floor(age / 60)}m`;
  return `${Math.floor(age / 3600)}h`;
}

/** Truncate text to a max length, keeping the tail (most recent). */
export function truncateText(text: string, maxLength: number): string {
  if (text.length <= maxLength) return text;
  return '…' + text.slice(-maxLength + 1);
}
