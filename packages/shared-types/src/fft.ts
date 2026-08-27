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
