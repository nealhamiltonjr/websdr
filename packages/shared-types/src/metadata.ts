/** Metadata events — server → client JSON messages over the same WS as FFT.
 *
 *  Distinct from FFT frames (binary) — these are JSON strings, one per line.
 */

import type { ReceiverMetadata } from './receiver.js';

export interface MetadataEvent {
  type: 'metadata';
  receiverId: string;
  data: ReceiverMetadata;
}

export interface OpenEvent {
  type: 'open';
  receiverId: string;
  timestamp: number;
}

export interface CloseEvent {
  type: 'close';
  receiverId: string;
  code: number;
  reason: string;
  timestamp: number;
}

export interface ErrorEvent {
  type: 'error';
  receiverId: string;
  message: string;
  /** Optional backend error code. */
  code?: string;
}

/** Decoder events emitted by RF-band / audio-band plugin runners.
 *  See ADR-003 for the per-decoder event schema. */
export interface DecoderEvent {
  type: 'decoder';
  decoder: 'adsb' | 'uat' | 'ais' | 'dab' | 'acars' | 'freedv' | 'ft8' | 'packet' | 'rtty' | 'cw' | 'sstv' | 'fax' | 'psk' | 'olivia';
  receiverId: string;
  /** Event payload — decoder-specific JSON. */
  data: unknown;
  timestamp: number;
}

export type ServerToClientEvent =
  | MetadataEvent
  | OpenEvent
  | CloseEvent
  | ErrorEvent
  | DecoderEvent;
