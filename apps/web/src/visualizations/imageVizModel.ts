/** ImageVizModel — pure-logic model for the ImageViz component (slice-46).
 *
 *  Subscribes to IMAGE_DECODERS family events (sstv) and maintains
 *  the latest decoded image + progress state. The model is testable
 *  without SolidJS — it's a pure function from (state, event) → state.
 */

import type {
  DecoderEventEnvelope,
  ImageEvent,
  ImageModeEvent,
  ImageScanlineEvent,
} from '@openwebrx-plus/shared-types';
import { IMAGE_DECODERS } from '@openwebrx-plus/shared-types';

const imageDecoderNames: readonly string[] = IMAGE_DECODERS;

export interface ImageVizState {
  /** The most recent decoded image (null before the first complete decode). */
  currentImage: ImageEvent | null;
  /** Total images decoded. */
  imageCount: number;
  /** Current scanline progress (0 = no frame in progress). */
  scanlineProgress: number;
  /** The SSTV mode being decoded (null before VIS detection). */
  mode: string | null;
  /** The raw VIS code byte (0-127). */
  visCode: number | null;
  /** Timestamp of the last update. */
  lastUpdate: number;
  /** The decoder name (always 'sstv' for now). */
  decoderName: string | null;
}

export function initialImageVizState(): ImageVizState {
  return {
    currentImage: null,
    imageCount: 0,
    scanlineProgress: 0,
    mode: null,
    visCode: null,
    lastUpdate: 0,
    decoderName: null,
  };
}

/** Apply a decoder event to the image viz state.
 *
 *  - ImageEvent (kind='image'): store as currentImage, increment count.
 *  - ImageScanlineEvent (kind='scanline'): update progress.
 *  - ImageModeEvent (kind='mode'): store mode + VIS code.
 *  - Other events: ignore.
 */
export function applyDecoderEvent(
  state: ImageVizState,
  envelope: DecoderEventEnvelope,
): ImageVizState {
  if (!imageDecoderNames.includes(envelope.decoder)) {
    return state;
  }
  const event = envelope.event;
  if (event.kind === 'image') {
    const imgEvent = event as unknown as ImageEvent;
    return {
      ...state,
      currentImage: imgEvent,
      imageCount: state.imageCount + 1,
      scanlineProgress: 0, // frame complete
      lastUpdate: imgEvent.ts,
      decoderName: envelope.decoder,
    };
  }
  if (event.kind === 'scanline') {
    const scanEvent = event as unknown as ImageScanlineEvent;
    return {
      ...state,
      scanlineProgress: scanEvent.scanline,
      lastUpdate: scanEvent.ts,
      decoderName: envelope.decoder,
    };
  }
  if (event.kind === 'mode') {
    const modeEvent = event as unknown as ImageModeEvent;
    return {
      ...state,
      mode: modeEvent.mode,
      visCode: modeEvent.vis_code,
      scanlineProgress: 0, // new frame starting
      lastUpdate: modeEvent.ts,
      decoderName: envelope.decoder,
    };
  }
  return state;
}

/** Decode a base64 string into a Uint8Array (for ImageData reconstruction). */
export function decodeBase64ToBytes(b64: string): Uint8Array {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
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

/** Compute the progress percentage (0-100) based on scanline count and mode. */
export function progressPercent(state: ImageVizState): number {
  if (state.scanlineProgress === 0 || !state.mode) return 0;
  // Known mode heights (from the backend SstvMode enum).
  const heights: Record<string, number> = {
    SCOTTIE_1: 256,
    SCOTTIE_2: 256,
    MARTIN_1: 256,
    MARTIN_2: 256,
    ROBOT_36: 240,
  };
  const height = heights[state.mode] ?? 256;
  return Math.min(100, Math.round((state.scanlineProgress / height) * 100));
}
