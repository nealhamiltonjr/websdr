/** Audio frame — emitted alongside FFT frames over the same WebSocket.
 *
 *  Wire format (binary, little-endian):
 *    Header (16 bytes):
 *      u32 magic = 0x41554449 ("AUDI")
 *      u32 version = 1
 *      u32 sampleRate   (Hz, e.g. 8000)
 *      u32 frameCount   (number of s16 samples in the body)
 *    Body: frameCount * 2 bytes of int16 PCM, mono, little-endian.
 *
 *  Notes:
 *    - Mono only for slice-1.5. Stereo comes in slice-2 with proper
 *      demodulation of FM stereo / DAB.
 *    - Sample rate is fixed per session (8000 Hz for slice-1.5). Variable
 *      rate support comes with the real DSP chain (slice-2).
 *    - Timestamp is NOT in the wire format; the AudioWorklet stamps on
 *      arrival via `performance.now()` and uses its own clock for scheduling.
 */

export interface AudioFrame {
  /** Sample rate of the PCM data, Hz. */
  sampleRate: number;
  /** Raw Int16 PCM samples, mono. Length = frameCount. */
  samples: Int16Array;
}

export const AUDIO_HEADER_MAGIC = 0x41554449; // "AUDI"
export const AUDIO_HEADER_VERSION = 1;
export const AUDIO_HEADER_SIZE_BYTES = 16;

export const AUDIO_OFFSET = {
  magic: 0,
  version: 4,
  sampleRate: 8,
  frameCount: 12,
} as const;
