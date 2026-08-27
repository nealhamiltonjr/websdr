/** AircraftListViz model — pure state reducer over ADS-B decoder events.
 *
 *  Extracted from the component so vitest can exercise the event → state
 *  fold without a DOM (same pattern as sourceFormModel.ts).
 *
 *  Accepts every ADSB_DECODERS family member (in-process "adsb" and the
 *  subprocess "dump1090" — identical event schemas); tracks subprocess
 *  lifecycle (decoder_state) and position-bearing rows.
 */

import {
  ADSB_DECODERS,
  type AdsbAircraftRow,
  type AdsbFrameEvent,
  type DecoderEventEnvelope,
} from '@openwebrx-plus/shared-types';

const adsbDecoderNames: readonly string[] = ADSB_DECODERS;

export interface AircraftFeedState {
  /** Latest aircraft table (newest first — server sorts). */
  rows: AdsbAircraftRow[];
  /** Human-readable summary of the most recent frame event. */
  lastFrame: string | null;
  /** Total frame events seen (for the header counter). */
  frameCount: number;
  /** Rows carrying lat/lon in the latest snapshot (subprocess decoder). */
  positionCount: number;
  /** Last subprocess lifecycle transition (null while healthy/unknown). */
  decoderState: { state: string; reason?: string } | null;
}

export function initialAircraftState(): AircraftFeedState {
  return { rows: [], lastFrame: null, frameCount: 0, positionCount: 0, decoderState: null };
}

/** Fold one decoder envelope into the feed state (ignores non-ADS-B). */
export function applyDecoderEvent(
  state: AircraftFeedState,
  envelope: DecoderEventEnvelope,
): AircraftFeedState {
  if (!adsbDecoderNames.includes(envelope.decoder)) return state;
  const event = envelope.event;

  if (event.kind === 'aircraft') {
    const snapshot = event as unknown as { aircraft?: AdsbAircraftRow[] };
    const rows = snapshot.aircraft ?? [];
    return {
      ...state,
      rows,
      positionCount: rows.filter(
        (r) => typeof r.lat === 'number' && typeof r.lon === 'number',
      ).length,
    };
  }

  if (event.kind === 'frame') {
    const frame = event as unknown as AdsbFrameEvent;
    return {
      ...state,
      frameCount: state.frameCount + 1,
      lastFrame: formatFrameSummary(frame),
    };
  }

  if (event.kind === 'decoder_state') {
    const ev = event as unknown as { state: string; reason?: string };
    return { ...state, decoderState: { state: ev.state, reason: ev.reason } };
  }

  return state;
}

/** "DF17 4D22AA · OWRX001 · 12500 ft" — the message-feed line. */
export function formatFrameSummary(frame: AdsbFrameEvent): string {
  const parts: string[] = [`DF${frame.df}`];
  if (frame.icao) parts.push(frame.icao);
  if (frame.callsign) parts.push(frame.callsign);
  if (frame.altitude_ft !== undefined) parts.push(`${frame.altitude_ft.toLocaleString()} ft`);
  return parts.join(' · ');
}

/** Age column text: "2s", "45s", "3m", "1h". */
export function formatAge(now: number, then: number): string {
  const s = Math.max(0, Math.round(now - then));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h`;
}

/** Altitude display: "12,500 ft" or "—" when not yet reported. */
export function formatAltitude(ft: number | null): string {
  return ft === null ? '—' : `${ft.toLocaleString()} ft`;
}

/** Position display: "37.42, -121.63" or "—" when the row has none. */
export function formatPosition(row: AdsbAircraftRow): string {
  if (typeof row.lat !== 'number' || typeof row.lon !== 'number') return '—';
  return `${row.lat.toFixed(2)}, ${row.lon.toFixed(2)}`;
}
