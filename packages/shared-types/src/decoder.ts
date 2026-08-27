/** Decoder events — ADR-003 plugin output streamed over the receiver WS.
 *
 *  Wire format (text frame, JSON — see apps/server ws.py):
 *    {
 *      "type": "decoder",
 *      "decoder": "adsb",
 *      "receiverId": "rx-…",
 *      "event": { "kind": "frame" | "aircraft", … }
 *    }
 *
 *  Binary frames (FFT/audio) keep their own wire formats; decoder output
 *  is JSON because it's low-rate structured data, not sample streams.
 *  The SharedWorker routes text frames by their `type` field —
 *  "metadata" keeps its legacy path, "decoder" fans out to
 *  ReceiverSession.decoderStream.
 */

/** Decoder names that emit ADS-B-family wire events.
 *
 *  Both the in-process "adsb" plugin and the subprocess "dump1090"
 *  plugin emit the same frame/aircraft event schema — the aircraft table
 *  viz consumes either. The subprocess plugin additionally emits
 *  decoder_state lifecycle events and rows carrying position fields.
 */
export const ADSB_DECODERS = ['adsb', 'dump1090'] as const;
export type AdsbDecoderName = (typeof ADSB_DECODERS)[number];

/** Envelope for every decoder event arriving over the WebSocket. */
export interface DecoderEventEnvelope {
  type: 'decoder';
  /** Decoder plugin name (e.g. "adsb", "dump1090"). */
  decoder: string;
  receiverId: string;
  /** Plugin-specific payload; `kind` discriminates the event family. */
  event: {
    kind: string;
    [key: string]: unknown;
  };
}

/** ADS-B "frame" event — one CRC-verified Mode S frame (see plugins/adsb.py). */
export interface AdsbFrameEvent {
  kind: 'frame';
  ts: number;
  df: number;
  icao: string | null;
  callsign?: string;
  altitude_ft?: number;
  raw: string;
  parity: 'data' | 'address';
  rssi_dbfs: number;
}

/** One aircraft row in an ADS-B "aircraft" snapshot event. */
export interface AdsbAircraftRow {
  icao: string;
  callsign: string | null;
  altitude_ft: number | null;
  frames: number;
  last_seen: number;
  rssi_dbfs: number;
  /** Position + velocity — present when the decoder provides them (the
   *  dump1090 subprocess decodes CPR; the in-process plugin does not). */
  lat?: number;
  lon?: number;
  groundspeed_kt?: number;
  vertical_rate_fpm?: number;
  /** "synthetic" for the test fake, "cpr" for real decoders. */
  position_source?: string;
}

/** ADS-B "aircraft" event — full table snapshot, newest first. */
export interface AdsbAircraftEvent {
  kind: 'aircraft';
  ts: number;
  aircraft: AdsbAircraftRow[];
}

/** Subprocess-decoder lifecycle event (PluginRunner restarts/failures). */
export interface DecoderStateEvent {
  kind: 'decoder_state';
  state: 'restarting' | 'failed';
  reason?: string;
  restarts?: number;
  attempt?: number;
  delay?: number;
}

const adsbDecoderNames: readonly string[] = ADSB_DECODERS;

/** Type guard: this decoder event is an ADS-B aircraft snapshot (any
 *  ADSB_DECODERS family member). */
export function isAdsbAircraftEvent(
  envelope: DecoderEventEnvelope,
): envelope is DecoderEventEnvelope & { event: AdsbAircraftEvent } {
  return (
    adsbDecoderNames.includes(envelope.decoder) && envelope.event.kind === 'aircraft'
  );
}

/** Type guard: this decoder event is an ADS-B frame (any family member). */
export function isAdsbFrameEvent(
  envelope: DecoderEventEnvelope,
): envelope is DecoderEventEnvelope & { event: AdsbFrameEvent } {
  return (
    adsbDecoderNames.includes(envelope.decoder) && envelope.event.kind === 'frame'
  );
}
