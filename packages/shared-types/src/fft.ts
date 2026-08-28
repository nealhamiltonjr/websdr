/** FFT frame — emitted at ~10–50 fps per receiver over WebSocket.
 *
 *  Wire format (binary, little-endian):
 *    Header (32 bytes):
 *      u32 magic = 0x4f465257 ("WRFO")
 *      u32 version = 1
 *      u32 receiverIdHash       (low 32 bits of a hash; full id in metadata)
 *      f32 centerFreq (Hz)
 *      f32 sampleRate (Hz)
 *      f32 minDb
 *      f32 maxDb
 *      u32 binCount
 *    Body: binCount * sizeof(f32) — power in dBFS, monotonic non-increasing
 *    in index (leftmost = lowest freq, rightmost = highest freq, already fftshifted).
 *
 *  Notes:
 *    - Timestamp is NOT in the wire format. The frontend stamps it with
 *      `performance.now()` on arrival (high-resolution, already aligned to
 *      the page's clock).
 *    - The full `receiverId` string is also NOT in the binary frame. Clients
 *      match it to the metadata they received earlier via the hash.
 *
 *  TypeScript view (zero-copy): wrap the ArrayBuffer in a DataView, read
 *  the header, slice the bins as `new Float32Array(data, 32, binCount)`.
 *  See `apps/web/src/sessions/ReceiverSession.ts` → `parseFFTFrame`.
 */

export interface FFTFrame {
  receiverId: string;
  timestamp: number;       // ms (performance.now-aligned, stamped on arrival)
  centerFreq: number;      // Hz
  sampleRate: number;      // Hz
  bins: Float32Array;      // power in dBFS, length = binCount
  minDb: number;
  maxDb: number;
}

/** Header layout for the binary wire format. */
export const FFT_HEADER_MAGIC = 0x4f465257;
export const FFT_HEADER_VERSION = 1;
export const FFT_HEADER_SIZE_BYTES = 32;

/** Field offsets within the 32-byte header (little-endian). */
export const FFT_OFFSET = {
  magic: 0,
  version: 4,
  receiverIdHash: 8,
  centerFreq: 12,
  sampleRate: 16,
  minDb: 20,
  maxDb: 24,
  binCount: 28,
} as const;

// ============================================================================
// Secondary FFT — slice-22 (federation polish)
//
// The OpenWebRX federation protocol carries TWO FFT streams per receiver:
//   - Type 0x01: primary (wideband) FFT — the main waterfall.
//   - Type 0x03: secondary (narrowband, demod-channel) FFT — the
//     "channel scope" view, showing the FSK tones of the active
//     digital-mode channel in a tight zoom.
//
// Slice-14 STATUS.md listed "secondary-demod forwarding for
// openwebrx_remote (Type 0x03 secondary FFT frames still skipped in
// the federation client)" as an open item. This slice closes it: the
// federation client decodes the 0x03 frame, the session repacks it
// as a "WRSF" wire frame, and the frontend can render a secondary
// waterfall alongside the primary.
//
// The wire format mirrors the primary FFT (32-byte header + bin body)
// with a distinct magic so the client's WS demux can route it. The
// center_freq / sample_rate fields describe the SECONDARY channel
// (the demod channel — typically much narrower than the wideband
// span; e.g. for FT8 at 14.074 MHz the secondary span is ~250 Hz
// around the dial frequency, while the primary span is whatever the
// remote SDR covers, e.g. 2.4 MHz around 14.150 MHz).
// ============================================================================

export const SECONDARY_FFT_HEADER_MAGIC = 0x46535257;  // "WRSF"
export const SECONDARY_FFT_HEADER_VERSION = 1;
export const SECONDARY_FFT_HEADER_SIZE_BYTES = 32;

/** Secondary FFT frame — emitted at the secondary FFT rate (typically
 *  matches the primary FFT rate, but on a much narrower span).
 *
 *  Same wire format as FFTFrame (32-byte header + bin body), distinct
 *  magic so the frontend WS demux routes it to a separate stream. */
export interface SecondaryFFTFrame {
  receiverId: string;
  timestamp: number;
  /** The center frequency of the demod channel (Hz). */
  centerFreq: number;
  /** The sample rate of the demod channel (Hz) — typically much
   *  narrower than the primary FFT's sample_rate. */
  sampleRate: number;
  bins: Float32Array;
  minDb: number;
  maxDb: number;
}

/** Field offsets within the 32-byte secondary header (same layout
 *  as FFT_OFFSET — repeated here so the shared-types module is the
 *  single source of truth for the wire format). */
export const SECONDARY_FFT_OFFSET = {
  magic: 0,
  version: 4,
  receiverIdHash: 8,
  centerFreq: 12,
  sampleRate: 16,
  minDb: 20,
  maxDb: 24,
  binCount: 28,
} as const;
