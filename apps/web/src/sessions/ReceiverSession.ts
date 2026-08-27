/** ReceiverSession — the core abstraction of the OpenWebRX+ frontend.
 *
 *  One ReceiverSession represents ONE logical receiver (one frequency + mode +
 *  DSP chain). It holds:
 *    - An FFT stream (high-frequency, ~10–50 fps, Float32Array per frame)
 *    - A metadata stream (low-frequency, frequency/mode/source changes)
 *    - An audio node (post-demod audio, rendered via AudioWorklet)
 *
 *  Visualizations do NOT know about WebSocket, layout, or windows. They look
 *  up their ReceiverSession by id via `receiverRegistry` and subscribe to the
 *  streams they need.
 *
 *  The SharedWorker (src/workers/sdr.shared-worker.ts) holds the actual
 *  WebSocket per receiver; this class is the in-page proxy that fans out
 *  worker messages to subscribers.
 *
 *  See ADR-001 § Layer 1 for the contract.
 */

import { Subject } from '../lib/subject';
import {
  FFTFrame,
  FFT_HEADER_MAGIC,
  FFT_HEADER_SIZE_BYTES,
  FFT_HEADER_VERSION,
  FFT_OFFSET,
  ReceiverMetadata,
  AudioFrame,
  AUDIO_HEADER_MAGIC,
  AUDIO_HEADER_SIZE_BYTES,
  AUDIO_HEADER_VERSION,
  AUDIO_OFFSET,
  DecoderEventEnvelope,
} from '@openwebrx-plus/shared-types';

/** Crosshair state shared between the linked visualizations of one receiver
 *  (ADR-001 feature 11). `sourceVizId` discriminates the originating panel so
 *  a viz can skip re-applying its own echo. */
export interface CursorState {
  /** Frequency under the cursor, Hz. */
  hz: number;
  /** Id of the viz panel the cursor is over. */
  sourceVizId: string;
}

// Re-export so callers can do `import { FFTFrame, ReceiverMetadata } from '../sessions/ReceiverSession'`.
export type {
  FFTFrame,
  ReceiverMetadata,
  ReceiverMode,
  SourceInfo,
  SourceType,
  AudioFrame,
  DecoderEventEnvelope,
} from '@openwebrx-plus/shared-types';

// ---- Class ----------------------------------------------------------------

export class ReceiverSession {
  readonly id: string;
  readonly fftStream = new Subject<FFTFrame>();
  readonly audioStream = new Subject<AudioFrame>();
  readonly metadataStream = new Subject<ReceiverMetadata>();
  readonly decoderStream = new Subject<DecoderEventEnvelope>();
  /** Crosshair sync channel for this receiver's linked vizes (slice-4.6). */
  readonly cursorStream = new Subject<CursorState | null>();

  private metadata: ReceiverMetadata | null = null;
  private audioNode: AudioNode | null = null;
  private cursor: CursorState | null = null;
  /**
   * Optional forward sink for cross-window cursor broadcast (slice-6.3).
   *
   * When `setCursor` is called from a local viz (mouse over a canvas in
   * THIS page), the session both emits on `cursorStream` (local) AND calls
   * this sink so the SharedWorker can fan the cursor out to every other
   * page subscribed to this receiver (popouts, etc.). Set by the route
   * that owns the SharedWorker port (main.tsx / popout.tsx).
   *
   * `ingestRemoteCursor` (called when the worker hands us a cursor that
   * originated in ANOTHER page) updates the local state + emits on
   * `cursorStream` but does NOT re-forward (otherwise we'd echo forever).
   */
  private cursorForward: ((hz: number | null, sourceVizId: string) => void) | null = null;

  constructor(id: string) {
    this.id = id;
  }

  /** Called by the SharedWorker bridge when an FFT frame arrives. */
  ingestFFT(frame: FFTFrame): void {
    this.fftStream.emit(frame);
  }

  /** Called by the SharedWorker bridge when an audio frame arrives. */
  ingestAudio(frame: AudioFrame): void {
    this.audioStream.emit(frame);
  }

  /** Called by the SharedWorker bridge when metadata changes. */
  ingestMetadata(meta: ReceiverMetadata): void {
    this.metadata = meta;
    this.metadataStream.emit(meta);
  }

  /** Called by the SharedWorker bridge when a decoder event arrives (ADR-003). */
  ingestDecoder(envelope: DecoderEventEnvelope): void {
    this.decoderStream.emit(envelope);
  }

  /** Wire a forward sink for cross-window cursor broadcast (slice-6.3).
   *  Set by the route that owns the SharedWorker port. Pass null to detach. */
  setCursorForward(fn: ((hz: number | null, sourceVizId: string) => void) | null): void {
    this.cursorForward = fn;
  }

  /** Publish the hover crosshair to this receiver's linked vizes (slice-4.6).
   *  null = cursor left the canvas. The value is retained so panels that
   *  mount later (a freshly added "+ viz" tab) can pick it up on first draw.
   *
   *  Slice-6.3: also forwards to the SharedWorker (via `cursorForward`) so
   *  popouts and other subscribed windows can mirror the cursor. The
   *  originator skips its own echo via `sourceVizId === vizId`. */
  setCursor(hz: number | null, sourceVizId: string): void {
    this.cursor = hz == null ? null : { hz, sourceVizId };
    this.cursorStream.emit(this.cursor);
    if (this.cursorForward) {
      this.cursorForward(hz, sourceVizId);
    }
  }

  /** Ingest a cursor update that came from the SharedWorker (originated in
   *  ANOTHER window — slice-6.3). Updates local state + emits on
   *  `cursorStream` so local vizes draw the crosshair, but does NOT
   *  re-forward (otherwise we'd echo forever). */
  ingestRemoteCursor(hz: number | null, sourceVizId: string): void {
    this.cursor = hz == null ? null : { hz, sourceVizId };
    this.cursorStream.emit(this.cursor);
  }

  /** Latest crosshair state (null when the cursor is outside every canvas). */
  getCursor(): CursorState | null {
    return this.cursor;
  }

  /** Attach a demodulated audio node (from AudioWorklet). */
  attachAudio(node: AudioNode): void {
    this.audioNode = node;
  }

  getAudio(): AudioNode | null {
    return this.audioNode;
  }

  getCurrentMetadata(): ReceiverMetadata | null {
    return this.metadata;
  }
}

// ---- Singleton registry ---------------------------------------------------

/** Visualizations look up their ReceiverSession by id via this registry.
 *  If a viz mounts before the session has been created (e.g., a popout
 *  opened before the worker announced the receiver), the registry creates
 *  a placeholder and the SharedWorker will fill it in.
 */
class ReceiverSessionRegistry {
  private sessions = new Map<string, ReceiverSession>();

  getOrCreate(id: string): ReceiverSession {
    let s = this.sessions.get(id);
    if (!s) {
      s = new ReceiverSession(id);
      this.sessions.set(id, s);
    }
    return s;
  }

  get(id: string): ReceiverSession | undefined {
    return this.sessions.get(id);
  }

  list(): ReceiverSession[] {
    return Array.from(this.sessions.values());
  }

  destroy(id: string): void {
    const s = this.sessions.get(id);
    if (!s) return;
    s.fftStream.dispose();
    s.metadataStream.dispose();
    s.decoderStream.dispose();
    s.audioStream.dispose();
    s.cursorStream.dispose();
    this.sessions.delete(id);
  }
}

export const receiverRegistry = new ReceiverSessionRegistry();

// ---- Binary wire-format parser --------------------------------------------

/** Parse the binary FFT wire format defined in
 *  `packages/shared-types/src/fft.ts` into an FFTFrame.
 *
 *  Zero-copy: the returned `bins` Float32Array shares the underlying buffer
 *  with the input ArrayBuffer (offset 32, length binCount).
 *
 *  @param data  The raw bytes received from the SharedWorker (msg.data)
 *  @param receiverId  The receiver id (passed separately — not in the wire format)
 */
export function parseFFTFrame(data: ArrayBuffer, receiverId: string): FFTFrame {
  if (data.byteLength < FFT_HEADER_SIZE_BYTES) {
    throw new Error(
      `[parseFFTFrame] frame too small: ${data.byteLength} bytes (need ≥ ${FFT_HEADER_SIZE_BYTES})`,
    );
  }

  const view = new DataView(data);

  const magic = view.getUint32(FFT_OFFSET.magic, true);
  if (magic !== FFT_HEADER_MAGIC) {
    throw new Error(
      `[parseFFTFrame] bad magic 0x${magic.toString(16)} (expected 0x${FFT_HEADER_MAGIC.toString(16)})`,
    );
  }

  const version = view.getUint32(FFT_OFFSET.version, true);
  if (version !== FFT_HEADER_VERSION) {
    throw new Error(`[parseFFTFrame] unsupported version ${version}`);
  }

  // receiverIdHash at offset 8 is intentionally ignored — the caller has the
  // full receiverId from the separate metadata channel. We could assert they
  // match, but for slice-1 we just trust the source.

  const centerFreq = view.getFloat32(FFT_OFFSET.centerFreq, true);
  const sampleRate = view.getFloat32(FFT_OFFSET.sampleRate, true);
  const minDb = view.getFloat32(FFT_OFFSET.minDb, true);
  const maxDb = view.getFloat32(FFT_OFFSET.maxDb, true);
  const binCount = view.getUint32(FFT_OFFSET.binCount, true);

  const expectedSize = FFT_HEADER_SIZE_BYTES + binCount * 4;
  if (data.byteLength < expectedSize) {
    throw new Error(
      `[parseFFTFrame] frame truncated: ${data.byteLength} bytes (need ${expectedSize} for ${binCount} bins)`,
    );
  }

  // Zero-copy slice into the underlying buffer. The Float32Array shares
  // memory with `data` — no allocation, no copy. Note: this means the buffer
  // must not be transferred back to the worker before this view is consumed.
  const bins = new Float32Array(data, FFT_HEADER_SIZE_BYTES, binCount);

  return {
    receiverId,
    // High-resolution timestamp aligned to the page's performance clock.
    timestamp: typeof performance !== 'undefined' ? performance.now() : Date.now(),
    centerFreq,
    sampleRate,
    bins,
    minDb,
    maxDb,
  };
}

// ---- Audio frame parser ---------------------------------------------------

/** Parse the binary audio wire format defined in
 *  `packages/shared-types/src/audio.ts` into an AudioFrame.
 *
 *  Zero-copy: the returned `samples` Int16Array shares the underlying
 *  buffer with the input ArrayBuffer (offset 16, length frameCount).
 */
export function parseAudioFrame(data: ArrayBuffer): AudioFrame {
  if (data.byteLength < AUDIO_HEADER_SIZE_BYTES) {
    throw new Error(
      `[parseAudioFrame] frame too small: ${data.byteLength} bytes (need ≥ ${AUDIO_HEADER_SIZE_BYTES})`,
    );
  }

  const view = new DataView(data);

  const magic = view.getUint32(AUDIO_OFFSET.magic, true);
  if (magic !== AUDIO_HEADER_MAGIC) {
    throw new Error(
      `[parseAudioFrame] bad magic 0x${magic.toString(16)} (expected 0x${AUDIO_HEADER_MAGIC.toString(16)})`,
    );
  }

  const version = view.getUint32(AUDIO_OFFSET.version, true);
  if (version !== AUDIO_HEADER_VERSION) {
    throw new Error(`[parseAudioFrame] unsupported version ${version}`);
  }

  const sampleRate = view.getUint32(AUDIO_OFFSET.sampleRate, true);
  const frameCount = view.getUint32(AUDIO_OFFSET.frameCount, true);

  const expectedSize = AUDIO_HEADER_SIZE_BYTES + frameCount * 2;
  if (data.byteLength < expectedSize) {
    throw new Error(
      `[parseAudioFrame] frame truncated: ${data.byteLength} bytes (need ${expectedSize} for ${frameCount} samples)`,
    );
  }

  // Zero-copy slice into the underlying buffer.
  const samples = new Int16Array(data, AUDIO_HEADER_SIZE_BYTES, frameCount);

  return { sampleRate, samples };
}
