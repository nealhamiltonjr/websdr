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
 *  The in-process "adsb" plugin, the subprocess "dump1090" plugin, AND
 *  the in-process "dump978" UAT plugin all emit the same frame/aircraft
 *  event schema — the aircraft table viz consumes any of them. The
 *  subprocess plugin additionally emits decoder_state lifecycle events
 *  and rows carrying position fields (CPR); the dump978 plugin v1
 *  emits the same fields when the message carries them.
 */
export const ADSB_DECODERS = ['adsb', 'dump1090', 'dump978'] as const;
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

// ============================================================================
// AIS (Marine) — slice-6.4 (in-process AIS decoder plugin)
//
// The AIS plugin emits the same envelope schema as ADS-B but with
// `decoder: "ais"` and `kind: "vessel"` (instead of "aircraft"). The
// events pair exactly with the AIS-side plugin (apps/server/
// openwebrx_plus/plugins/ais.py) which decodes ITU-R M.1371-5 messages.
// ============================================================================

/** Decoder names that emit AIS-family wire events. */
export const AIS_DECODERS = ['ais'] as const;
export type AisDecoderName = (typeof AIS_DECODERS)[number];

/** AIS "frame" event — one CRC-verified AIS message (see plugins/ais.py). */
export interface AisFrameEvent {
  kind: 'frame';
  ts: number;
  /** ITU-R M.1371-5 message type (1-3 = Class A position, 4 = base station,
   * 5 = static & voyage, 18 = Class B position, 21 = aid-to-nav, etc.). */
  type: number;
  /** Maritime Mobile Service Identity — 9-digit unique ship ID. */
  mmsi: string;
  /** Full message payload hex (UNSTUFFED, pre-CRC) — for raw display. */
  raw: string;
  rssi_dbfs: number;
  /** Optional fields populated per message type. */
  speed_kn?: number;
  longitude?: number;
  latitude?: number;
  course_deg?: number;
  heading_deg?: number;
  timestamp_sec?: number;
  vessel_name?: string;
  callsign?: string;
  imo?: number;
  destination?: string;
  nav_status?: number;
  ship_type?: number;
}

/** One vessel row in an AIS "vessel" snapshot event. */
export interface AisVesselRow {
  mmsi: string;
  /** Last message type received for this vessel (1-3, 4, 5, 18, 21). */
  type: number;
  vessel_name: string | null;
  callsign: string | null;
  imo: number | null;
  ship_type: number | null;
  speed_kn: number | null;
  longitude: number | null;
  latitude: number | null;
  course_deg: number | null;
  heading_deg: number | null;
  nav_status: number | null;
  destination: string | null;
  frames: number;
  last_seen: number;
  rssi_dbfs: number;
}

/** AIS "vessel" event — full table snapshot, newest first. */
export interface AisVesselEvent {
  kind: 'vessel';
  ts: number;
  vessels: AisVesselRow[];
}

const aisDecoderNames: readonly string[] = AIS_DECODERS;

/** Type guard: this decoder event is an AIS vessel snapshot. */
export function isAisVesselEvent(
  envelope: DecoderEventEnvelope,
): envelope is DecoderEventEnvelope & { event: AisVesselEvent } {
  return (
    aisDecoderNames.includes(envelope.decoder) && envelope.event.kind === 'vessel'
  );
}

/** Type guard: this decoder event is an AIS frame. */
export function isAisFrameEvent(
  envelope: DecoderEventEnvelope,
): envelope is DecoderEventEnvelope & { event: AisFrameEvent } {
  return (
    aisDecoderNames.includes(envelope.decoder) && envelope.event.kind === 'frame'
  );
}

// ============================================================================
// FT8 (and similar audio-band digital modes) — slice-21
//
// FT8 is a weak-signal digital mode used on HF (amateur radio):
//   - 8-FSK at 6.25 baud, 12.5 Hz tone spacing, 15-second slots.
//   - Each message: 77 bits + LDPC + CRC, decoded free-form as
//     "CALLSIGN GRIDLOC SIGNAL_REPORT" (e.g. "K1ABC KO51 -12").
//   - The reference impl is WSJT-X; this slice ships the *wire format*
//     for the OpenWebRX+ plugin output, not the demodulator itself.
//
// The wire schema is intentionally generic: it covers any audio-band
// digital mode (FT8, FT4, WSPR, JT65, JT9, PSK31, RTTY) — each emits
// `kind: 'message'` events carrying a free-form `text` field plus
// optional structured fields. The DigiMessageListViz renders any of
// them in a single table.
// ============================================================================

/** Decoder names that emit audio-band digital-mode wire events.
 *
 *  Slice-21 ships the FT8 stub plugin (manifest + status() only — the
 *  full demodulator is a future slice). The wire format here lets the
 *  viz component + tests land first, mirroring slice-20's SDRangel
 *  manifest pattern. */
/** Decoder names that emit text-character wire events (slice-42).
 *
 *  CW (slice-13), RTTY (slice-38), PSK31 (slice-39), and Olivia
 *  (slice-41) all emit the same frame/text event schema — a "frame"
 *  event per decoded character and a "text" snapshot periodically.
 *  The DigiMessageListViz + a future TextStreamViz consume any of them.
 */
export const TEXT_DECODERS = ['cw', 'rtty', 'psk31', 'olivia'] as const;
export type TextDecoderName = (typeof TEXT_DECODERS)[number];

/** Decoder names that emit image wire events (slice-42 + slice-58).
 *
 *  SSTV (slice-40) emits "image" events with base64-encoded RGB pixel
 *  data, plus "scanline" progress and "mode" detection events.
 *  FAX (slice-51) emits "image" events with base64-encoded grayscale
 *  pixel data, plus "scanline" and "start"/"stop" events. */
export const IMAGE_DECODERS = ['sstv', 'fax'] as const;
export type ImageDecoderName = (typeof IMAGE_DECODERS)[number];

export const DIGI_MESSAGE_DECODERS = ['ft8', 'wspr', 'jt65', 'jt9'] as const;
export type DigiMessageDecoderName = (typeof DIGI_MESSAGE_DECODERS)[number];

/** Decoder names that emit packet wire events (slice-48).
 *
 *  AX.25 (slice-45) emits "packet" events with source/destination
 *  callsigns + digipeaters + control + info payload, plus "crc_error"
 *  events for corrupted frames. */
export const PACKET_DECODERS = ['ax25'] as const;
export type PacketDecoderName = (typeof PACKET_DECODERS)[number];

/** Decoder names that emit ACARS aircraft messages (slice-57).
 *  ACARS (slice-52) emits "message" events with aircraft address + label + text. */
export const ACARS_DECODERS = ['acars'] as const;
export type AcarsDecoderName = (typeof ACARS_DECODERS)[number];

/** Decoder names that emit DAB service events (slice-57).
 *  DAB (slice-53) emits "service" + "ensemble" events with station labels. */
export const DAB_DECODERS = ['dab'] as const;
export type DabDecoderName = (typeof DAB_DECODERS)[number];

/** Decoder names that emit ATC voice activity events (slice-57).
 *  ATC (slice-55) emits "voice_start" / "voice_end" / "rssi" events. */
export const ATC_DECODERS = ['atc'] as const;
export type AtcDecoderName = (typeof ATC_DECODERS)[number];

/** FT8 (or similar audio-band digi-mode) "message" event — one
 *  decoded free-form text message (callsign, grid, signal report,
 *  etc.). The decoder plugin emits these as IQ is fed in. */
export interface DigiMessageEvent {
  kind: 'message';
  ts: number;
  /** The mode name (FT8 / FT4 / WSPR / JT65 / JT9 / PSK31 / RTTY). */
  mode: string;
  /** The decoded free-form text (typically "CALLSIGN GRIDLOC SNR" for
   *  FT8). Plain ASCII; the viz renders it as-is. */
  text: string;
  /** Optional structured fields the decoder may populate. */
  callsign?: string;
  grid_locator?: string;
  /** SNR in dB (FT8 reports -20 to +10 typical). */
  snr_db?: number;
  /** Audio frequency offset within the decoder's passband (Hz). */
  audio_offset_hz?: number;
  /** UTC timestamp of the slot (FT8 slots are aligned to 15s). */
  slot_utc?: number;
}

/** Snapshot of recent messages (last N=50 by default; the viz keeps a
 *  ring buffer). New messages emit a fresh snapshot event. */
export interface DigiMessageListEvent {
  kind: 'messages';
  ts: number;
  messages: DigiMessageEvent[];
}

const digiMessageDecoderNames: readonly string[] = DIGI_MESSAGE_DECODERS;

/** Type guard: this decoder event is a digi-mode message. */
export function isDigiMessageEvent(
  envelope: DecoderEventEnvelope,
): envelope is DecoderEventEnvelope & { event: DigiMessageEvent } {
  return (
    digiMessageDecoderNames.includes(envelope.decoder) &&
    envelope.event.kind === 'message'
  );
}

/** Type guard: this decoder event is a digi-mode message list snapshot. */
export function isDigiMessageListEvent(
  envelope: DecoderEventEnvelope,
): envelope is DecoderEventEnvelope & { event: DigiMessageListEvent } {
  return (
    digiMessageDecoderNames.includes(envelope.decoder) &&
    envelope.event.kind === 'messages'
  );
}

// ---------------------------------------------------------------------------
// Text-character decoder events (slice-42) — CW / RTTY / PSK31 / Olivia
// ---------------------------------------------------------------------------

const textDecoderNames: readonly string[] = TEXT_DECODERS;

/** A single decoded character from a text-band decoder (CW/RTTY/PSK31/Olivia).
 *  Emitted one per character as the decoder processes audio. */
export interface TextCharEvent {
  kind: 'frame';
  ts: number;
  /** The decoded character (may be a control char like \n, \r, space). */
  char: string;
}

/** A text snapshot from a text-band decoder — the full accumulated text
 *  up to this point. Emitted periodically (every ~0.5 s) or on word gaps. */
export interface TextSnapshotEvent {
  kind: 'text';
  ts: number;
  /** The full accumulated decoded text. */
  text: string;
}

/** Type guard: this decoder event is a text-character frame event. */
export function isTextCharEvent(
  envelope: DecoderEventEnvelope,
): envelope is DecoderEventEnvelope & { event: TextCharEvent } {
  return (
    textDecoderNames.includes(envelope.decoder) &&
    envelope.event.kind === 'frame'
  );
}

/** Type guard: this decoder event is a text snapshot. */
export function isTextSnapshotEvent(
  envelope: DecoderEventEnvelope,
): envelope is DecoderEventEnvelope & { event: TextSnapshotEvent } {
  return (
    textDecoderNames.includes(envelope.decoder) &&
    envelope.event.kind === 'text'
  );
}

// ---------------------------------------------------------------------------
// Image decoder events (slice-42) — SSTV
// ---------------------------------------------------------------------------

const imageDecoderNames: readonly string[] = IMAGE_DECODERS;

/** A complete decoded image from an image-band decoder (SSTV).
 *  The `data` field is base64-encoded raw RGB bytes (height × width × 3),
 *  row-major. The frontend reconstructs via Uint8Array → ImageData →
 *  putImageData. No PNG compression (keeps the encoder pure-numpy, no
 *  PIL dependency in the live path). */
export interface ImageEvent {
  kind: 'image';
  ts: number;
  /** The SSTV mode name (SCOTTIE_1, MARTIN_1, etc.). */
  mode: string;
  width: number;
  height: number;
  /** Base64-encoded raw RGB bytes (no header). */
  data: string;
  /** Sequential index of this image (0, 1, 2, ...). */
  image_index: number;
}

/** Scanline progress event — emitted every N scanlines during decoding. */
export interface ImageScanlineEvent {
  kind: 'scanline';
  ts: number;
  /** Number of scanlines decoded so far. */
  scanline: number;
}

/** Mode detection event — emitted when the VIS code is identified. */
export interface ImageModeEvent {
  kind: 'mode';
  ts: number;
  /** The SSTV mode name (SCOTTIE_1, MARTIN_1, etc.). */
  mode: string;
  /** The raw VIS code byte (0-127). */
  vis_code: number;
}

/** Type guard: this decoder event is an image event. */
export function isImageEvent(
  envelope: DecoderEventEnvelope,
): envelope is DecoderEventEnvelope & { event: ImageEvent } {
  return (
    imageDecoderNames.includes(envelope.decoder) &&
    envelope.event.kind === 'image'
  );
}

/** Type guard: this decoder event is an image scanline progress event. */
export function isImageScanlineEvent(
  envelope: DecoderEventEnvelope,
): envelope is DecoderEventEnvelope & { event: ImageScanlineEvent } {
  return (
    imageDecoderNames.includes(envelope.decoder) &&
    envelope.event.kind === 'scanline'
  );
}

/** Type guard: this decoder event is an image mode detection event. */
export function isImageModeEvent(
  envelope: DecoderEventEnvelope,
): envelope is DecoderEventEnvelope & { event: ImageModeEvent } {
  return (
    imageDecoderNames.includes(envelope.decoder) &&
    envelope.event.kind === 'mode'
  );
}

// ---------------------------------------------------------------------------
// Packet decoder events (slice-48) — AX.25
// ---------------------------------------------------------------------------

const packetDecoderNames: readonly string[] = PACKET_DECODERS;

/** A decoded AX.25 packet — source/destination callsigns + info payload. */
export interface PacketEvent {
  kind: 'packet';
  ts: number;
  /** Source callsign (with SSID, e.g., "K1ABC-1"). */
  source: string;
  /** Destination callsign (with SSID). */
  destination: string;
  /** Digipeater callsigns (with SSIDs). */
  digipeaters: string[];
  /** Control byte (determines frame type: I/S/U). */
  control: number;
  /** Frame type: "I" (information), "S" (supervisory), "U" (unnumbered). */
  frame_type: string;
  /** Info payload as hex string. */
  info_hex: string;
  /** Info payload as ASCII text (lossy — non-ASCII replaced). */
  info_text: string;
  /** Sequential index of this packet. */
  packet_index: number;
}

/** A CRC error event — frame was received but the CRC check failed. */
export interface PacketCrcErrorEvent {
  kind: 'crc_error';
  ts: number;
  /** Error reason (e.g., "CRC mismatch", "parse error"). */
  reason: string;
  /** First 64 bytes of the raw frame as hex (for diagnostics). */
  raw_hex: string;
  /** Total frame length in bytes. */
  length: number;
}

/** Type guard: this decoder event is an AX.25 packet. */
export function isPacketEvent(
  envelope: DecoderEventEnvelope,
): envelope is DecoderEventEnvelope & { event: PacketEvent } {
  return (
    packetDecoderNames.includes(envelope.decoder) &&
    envelope.event.kind === 'packet'
  );
}

/** Type guard: this decoder event is an AX.25 CRC error. */
export function isPacketCrcErrorEvent(
  envelope: DecoderEventEnvelope,
): envelope is DecoderEventEnvelope & { event: PacketCrcErrorEvent } {
  return (
    packetDecoderNames.includes(envelope.decoder) &&
    envelope.event.kind === 'crc_error'
  );
}

// ---------------------------------------------------------------------------
// ACARS decoder events (slice-57)
// ---------------------------------------------------------------------------

const acarsDecoderNames: readonly string[] = ACARS_DECODERS;

export interface AcarsMessageEvent {
  kind: 'message';
  ts: number;
  address: string;
  mode: string;
  ack: string;
  label: string;
  block_id: string;
  text: string;
  raw_hex: string;
  message_index: number;
}

export interface AcarsCrcErrorEvent {
  kind: 'crc_error';
  ts: number;
  reason: string;
  raw_hex: string;
  length: number;
}

export function isAcarsMessageEvent(
  envelope: DecoderEventEnvelope,
): envelope is DecoderEventEnvelope & { event: AcarsMessageEvent } {
  return acarsDecoderNames.includes(envelope.decoder) && envelope.event.kind === 'message';
}

export function isAcarsCrcErrorEvent(
  envelope: DecoderEventEnvelope,
): envelope is DecoderEventEnvelope & { event: AcarsCrcErrorEvent } {
  return acarsDecoderNames.includes(envelope.decoder) && envelope.event.kind === 'crc_error';
}

// ---------------------------------------------------------------------------
// DAB decoder events (slice-57)
// ---------------------------------------------------------------------------

const dabDecoderNames: readonly string[] = DAB_DECODERS;

export interface DabServiceEvent {
  kind: 'service';
  ts: number;
  service_id: number;
  label: string;
  program_type: number;
  subchannel_id: number | null;
}

export interface DabEnsembleEvent {
  kind: 'ensemble';
  ts: number;
  services: Array<{ service_id: number; label: string; program_type: number; subchannel_id: number | null }>;
  ensemble_index: number;
}

export function isDabServiceEvent(
  envelope: DecoderEventEnvelope,
): envelope is DecoderEventEnvelope & { event: DabServiceEvent } {
  return dabDecoderNames.includes(envelope.decoder) && envelope.event.kind === 'service';
}

export function isDabEnsembleEvent(
  envelope: DecoderEventEnvelope,
): envelope is DecoderEventEnvelope & { event: DabEnsembleEvent } {
  return dabDecoderNames.includes(envelope.decoder) && envelope.event.kind === 'ensemble';
}

// ---------------------------------------------------------------------------
// ATC voice activity events (slice-57)
// ---------------------------------------------------------------------------

const atcDecoderNames: readonly string[] = ATC_DECODERS;

export interface AtcVoiceStartEvent {
  kind: 'voice_start';
  ts: number;
  rssi_dbfs: number;
  frequency_hz: number;
}

export interface AtcVoiceEndEvent {
  kind: 'voice_end';
  ts: number;
  rssi_dbfs: number;
  frequency_hz: number;
}

export interface AtcRssiEvent {
  kind: 'rssi';
  ts: number;
  rssi_dbfs: number;
  frequency_hz: number;
}

export function isAtcVoiceStartEvent(
  envelope: DecoderEventEnvelope,
): envelope is DecoderEventEnvelope & { event: AtcVoiceStartEvent } {
  return atcDecoderNames.includes(envelope.decoder) && envelope.event.kind === 'voice_start';
}

export function isAtcVoiceEndEvent(
  envelope: DecoderEventEnvelope,
): envelope is DecoderEventEnvelope & { event: AtcVoiceEndEvent } {
  return atcDecoderNames.includes(envelope.decoder) && envelope.event.kind === 'voice_end';
}

export function isAtcRssiEvent(
  envelope: DecoderEventEnvelope,
): envelope is DecoderEventEnvelope & { event: AtcRssiEvent } {
  return atcDecoderNames.includes(envelope.decoder) && envelope.event.kind === 'rssi';
}
