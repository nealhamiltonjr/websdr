/** PacketListModel — pure-logic model for the PacketListViz component
 *  (slice-49).
 *
 *  Subscribes to PACKET_DECODERS family events (ax25) and maintains a
 *  ring buffer of recent packets + CRC errors. The model is testable
 *  without SolidJS — it's a pure function from (state, event) → state.
 */

import type {
  DecoderEventEnvelope,
  PacketEvent,
  PacketCrcErrorEvent,
} from '@openwebrx-plus/shared-types';
import { PACKET_DECODERS } from '@openwebrx-plus/shared-types';

const packetDecoderNames: readonly string[] = PACKET_DECODERS;

export const MAX_PACKETS = 100; // ring buffer size

export interface PacketRow {
  ts: number;
  source: string;
  destination: string;
  digipeaters: string[];
  control: number;
  frame_type: string;
  info_text: string;
  info_hex: string;
  packet_index: number;
  is_crc_error: boolean;
  error_reason?: string;
}

export interface PacketListState {
  packets: PacketRow[];
  total_decoded: number;
  total_crc_errors: number;
  last_update: number;
  decoder_name: string | null;
}

export function initialPacketListState(): PacketListState {
  return {
    packets: [],
    total_decoded: 0,
    total_crc_errors: 0,
    last_update: 0,
    decoder_name: null,
  };
}

/** Apply a decoder event to the packet list state.
 *
 *  - PacketEvent (kind='packet'): append a valid packet row.
 *  - PacketCrcErrorEvent (kind='crc_error'): append a CRC error row.
 *  - Other events: ignore.
 */
export function applyDecoderEvent(
  state: PacketListState,
  envelope: DecoderEventEnvelope,
): PacketListState {
  if (!packetDecoderNames.includes(envelope.decoder)) {
    return state;
  }
  const event = envelope.event;
  if (event.kind === 'packet') {
    const pkt = event as unknown as PacketEvent;
    const row: PacketRow = {
      ts: pkt.ts,
      source: pkt.source,
      destination: pkt.destination,
      digipeaters: pkt.digipeaters,
      control: pkt.control,
      frame_type: pkt.frame_type,
      info_text: pkt.info_text,
      info_hex: pkt.info_hex,
      packet_index: pkt.packet_index,
      is_crc_error: false,
    };
    const newPackets = [...state.packets, row];
    // Trim to MAX_PACKETS (keep most recent).
    if (newPackets.length > MAX_PACKETS) {
      newPackets.splice(0, newPackets.length - MAX_PACKETS);
    }
    return {
      packets: newPackets,
      total_decoded: state.total_decoded + 1,
      total_crc_errors: state.total_crc_errors,
      last_update: pkt.ts,
      decoder_name: envelope.decoder,
    };
  }
  if (event.kind === 'crc_error') {
    const err = event as unknown as PacketCrcErrorEvent;
    const row: PacketRow = {
      ts: err.ts,
      source: '???',
      destination: '???',
      digipeaters: [],
      control: 0,
      frame_type: 'ERR',
      info_text: err.reason,
      info_hex: err.raw_hex,
      packet_index: -1,
      is_crc_error: true,
      error_reason: err.reason,
    };
    const newPackets = [...state.packets, row];
    if (newPackets.length > MAX_PACKETS) {
      newPackets.splice(0, newPackets.length - MAX_PACKETS);
    }
    return {
      packets: newPackets,
      total_decoded: state.total_decoded,
      total_crc_errors: state.total_crc_errors + 1,
      last_update: err.ts,
      decoder_name: envelope.decoder,
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

/** Format a frame type for display. */
export function formatFrameType(frameType: string, control: number): string {
  if (frameType === 'ERR') return 'CRC ERR';
  return `${frameType} (0x${control.toString(16).padStart(2, '0')})`;
}

/** Format the digipeater list as a path string. */
export function formatDigipeaters(digis: string[]): string {
  if (digis.length === 0) return '';
  return ' via ' + digis.join(' > ');
}
