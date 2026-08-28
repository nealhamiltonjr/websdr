/** attachCrosshair — wires one FFT canvas into the receiver's crosshair sync.
 *
 *  Slice-4.6 (ADR-001 feature 11): all visualizations of the SAME receiver
 *  share one hover crosshair. Moving the cursor over any of them publishes
 *  {hz, sourceVizId} on the session's cursorStream; every linked panel draws
 *  the crosshair at that frequency (the originating panel draws it locally on
 *  mousemove already — it skips its own echo).
 *
 *  Each panel also draws the persistent tuned-frequency marker + passband
 *  band derived from the receiver's metadata (frequency + mode → per-mode
 *  passband table), and supports click-to-tune via the tune bus.
 *
 *  This module is DOM-facing but renderer-agnostic: it only needs
 *  getAxis()/setOverlays() from the host renderer.
 */

import type { ReceiverSession } from '../sessions/ReceiverSession';
import { requestTune } from '../sessions/tuneBus';
import type { VizOverlays } from '../lib/webgl2/overlay';
import { freqAtFraction, passbandAround, type FreqAxis } from './freqAxis';

/** Minimal renderer surface the crosshair needs. */
export interface CrosshairRenderer {
  getAxis(): FreqAxis | null;
  setOverlays(ov: VizOverlays): void;
}

export interface CrosshairOptions {
  canvas: HTMLCanvasElement;
  /** Absolutely-positioned chip element (pointer-events: none) for the Hz readout. */
  chip: HTMLElement;
  renderer: CrosshairRenderer;
  session: ReceiverSession;
  /** Unique id of this mounted viz instance. */
  vizId: string;
  /** Optional level formatter (e.g. dB readout from the last FFT frame). */
  formatLevel?: (hz: number) => string | null;
  /** Format the Hz value for the chip. */
  formatHz?: (hz: number) => string;
}

const fmtHzDefault = (hz: number): string => {
  if (hz >= 1_000_000_000) return `${(hz / 1e9).toFixed(6)} GHz`;
  if (hz >= 1_000_000) return `${(hz / 1e6).toFixed(4)} MHz`;
  if (hz >= 1_000) return `${(hz / 1e3).toFixed(2)} kHz`;
  return `${Math.round(hz)} Hz`;
};

export function attachCrosshair(opts: CrosshairOptions): () => void {
  const { canvas, chip, renderer, session, vizId } = opts;
  const fmtHz = opts.formatHz ?? fmtHzDefault;

  let meta = session.getCurrentMetadata();
  let remoteCursorHz: number | null = null;
  let disposed = false;

  const apply = (localHz: number | null) => {
    if (disposed) return;
    const cursorHz = localHz ?? remoteCursorHz;
    renderer.setOverlays({
      cursorHz,
      tunedHz: meta ? meta.frequency : null,
      passbandHz: meta ? passbandAround(meta.mode, meta.frequency) : null,
    });
  };

  const showChip = (ev: MouseEvent, hz: number) => {
    const axis = renderer.getAxis();
    const level = axis && opts.formatLevel ? opts.formatLevel(hz) : null;
    chip.textContent = level ? `${fmtHz(hz)} · ${level}` : fmtHz(hz);
    chip.style.display = 'block';
    // Position relative to the canvas's offsetParent (the viz wrapper).
    let x = canvas.offsetLeft + ev.offsetX + 10;
    const y = canvas.offsetTop + ev.offsetY + 14;
    const maxRight = canvas.offsetLeft + canvas.clientWidth;
    // Flip near the right edge so the chip stays inside the panel.
    const chipWidth = chip.offsetWidth || 120;
    if (x + chipWidth > maxRight + canvas.offsetLeft) {
      x = Math.max(canvas.offsetLeft, maxRight + canvas.offsetLeft - chipWidth - 4);
    }
    chip.style.left = `${x}px`;
    chip.style.top = `${y}px`;
  };

  const onMouseMove = (ev: MouseEvent) => {
    const axis = renderer.getAxis();
    if (!axis) return;
    const frac = canvas.clientWidth > 0 ? ev.offsetX / canvas.clientWidth : 0.5;
    const hz = freqAtFraction(axis, Math.max(0, Math.min(1, frac)));
    session.setCursor(hz, vizId);
    showChip(ev, hz);
    apply(hz);
  };

  const onMouseLeave = () => {
    session.setCursor(null, vizId);
    chip.style.display = 'none';
    apply(null);
  };

  const onClick = (ev: MouseEvent) => {
    const axis = renderer.getAxis();
    if (!axis) return;
    const frac = canvas.clientWidth > 0 ? ev.offsetX / canvas.clientWidth : 0.5;
    const hz = freqAtFraction(axis, Math.max(0, Math.min(1, frac)));
    requestTune(session.id, hz);
  };

  canvas.addEventListener('mousemove', onMouseMove);
  canvas.addEventListener('mouseleave', onMouseLeave);
  canvas.addEventListener('click', onClick);

  // Remote cursor updates (another panel of the same receiver).
  const unsubCursor = session.cursorStream.subscribe((c) => {
    if (disposed) return;
    if (c && c.sourceVizId === vizId) return; // own echo
    remoteCursorHz = c ? c.hz : null;
    apply(null);
  });

  // Metadata → tuned marker + passband.
  const unsubMeta = session.metadataStream.subscribe((m) => {
    if (disposed) return;
    meta = m;
    apply(null);
  });

  // Initial paint from whatever the session already knows.
  const c0 = session.getCursor();
  remoteCursorHz = c0 && c0.sourceVizId !== vizId ? c0.hz : null;
  apply(null);

  return () => {
    disposed = true;
    canvas.removeEventListener('mousemove', onMouseMove);
    canvas.removeEventListener('mouseleave', onMouseLeave);
    canvas.removeEventListener('click', onClick);
    unsubCursor();
    unsubMeta();
    chip.style.display = 'none';
  };
}
