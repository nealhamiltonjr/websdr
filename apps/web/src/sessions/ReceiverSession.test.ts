// @vitest-environment node
/** Tests for slice-6.3 — cross-window cursor broadcast plumbing.
 *
 * The cross-window pattern: a viz in window A calls `session.setCursor()`,
 * which (1) updates local state, (2) emits on `cursorStream` (local vizes
 * redraw), and (3) forwards to the SharedWorker via a sink. The worker
 * fans out to every other page subscribed to this receiver; those pages
 * call `session.ingestRemoteCursor()` which updates their local state
 * and emits on their `cursorStream` but does NOT re-forward (no echo).
 */
import { describe, it, expect, vi } from 'vitest';
import { ReceiverSession } from './ReceiverSession';

describe('ReceiverSession cursor forward + remote ingest (slice-6.3)', () => {
  it('setCursor updates local state + emits on cursorStream', () => {
    const s = new ReceiverSession('rx-a');
    const seen: Array<{ hz: number | null; sourceVizId: string } | null> = [];
    const unsub = s.cursorStream.subscribe((c) => seen.push(c));

    s.setCursor(14_150_000, 'viz-1');
    expect(s.getCursor()).toEqual({ hz: 14_150_000, sourceVizId: 'viz-1' });
    expect(seen).toEqual([{ hz: 14_150_000, sourceVizId: 'viz-1' }]);

    unsub();
  });

  it('setCursor(null) clears local state + emits null', () => {
    const s = new ReceiverSession('rx-a');
    const seen: Array<{ hz: number | null; sourceVizId: string } | null> = [];
    s.setCursor(14_150_000, 'viz-1');
    const unsub = s.cursorStream.subscribe((c) => seen.push(c));

    s.setCursor(null, 'viz-1');
    expect(s.getCursor()).toBeNull();
    // Note: the subscriber set up AFTER the first setCursor only sees the
    // second call — Subjects don't replay by default. That's intentional:
    // a viz that mounts later reads getCursor() directly for initial state.
    expect(seen).toEqual([null]);

    unsub();
  });

  it('setCursor forwards to the cursor sink when one is set', () => {
    const s = new ReceiverSession('rx-a');
    const forwards: Array<{ hz: number | null; sourceVizId: string }> = [];
    s.setCursorForward((hz, sourceVizId) => {
      forwards.push({ hz, sourceVizId });
    });

    s.setCursor(7_200_000, 'viz-waterfall');
    s.setCursor(null, 'viz-waterfall');

    expect(forwards).toEqual([
      { hz: 7_200_000, sourceVizId: 'viz-waterfall' },
      { hz: null, sourceVizId: 'viz-waterfall' },
    ]);

    s.setCursorForward(null);
  });

  it('setCursor is a no-op forward when no sink is attached', () => {
    const s = new ReceiverSession('rx-a');
    expect(() => s.setCursor(14_150_000, 'viz-1')).not.toThrow();
    expect(s.getCursor()).toEqual({ hz: 14_150_000, sourceVizId: 'viz-1' });
  });

  it('ingestRemoteCursor updates local state + emits on cursorStream', () => {
    const s = new ReceiverSession('rx-a');
    const seen: Array<{ hz: number | null; sourceVizId: string } | null> = [];
    const unsub = s.cursorStream.subscribe((c) => seen.push(c));

    // Cursor originated in another window — simulating the SharedWorker
    // handing us a `cursor` message.
    s.ingestRemoteCursor(14_205_000, 'viz-popout-1');
    expect(s.getCursor()).toEqual({ hz: 14_205_000, sourceVizId: 'viz-popout-1' });
    expect(seen).toEqual([{ hz: 14_205_000, sourceVizId: 'viz-popout-1' }]);

    unsub();
  });

  it('ingestRemoteCursor does NOT re-forward to the sink (no echo loop)', () => {
    const s = new ReceiverSession('rx-a');
    const forwards: Array<{ hz: number | null; sourceVizId: string }> = [];
    s.setCursorForward((hz, sourceVizId) => {
      forwards.push({ hz, sourceVizId });
    });

    // A remote cursor arrives (from the worker) — should be ingested but
    // NOT re-forwarded, otherwise we'd loop forever: A→worker→B→worker→A→…
    s.ingestRemoteCursor(14_205_000, 'viz-popout-1');
    s.ingestRemoteCursor(null, 'viz-popout-1');

    expect(forwards).toEqual([]);
    expect(s.getCursor()).toBeNull();

    s.setCursorForward(null);
  });

  it('a locally-set cursor forwards; a remote follow-up overrides without re-forwarding', () => {
    const s = new ReceiverSession('rx-a');
    const forwards: Array<{ hz: number | null; sourceVizId: string }> = [];
    s.setCursorForward((hz, sourceVizId) => {
      forwards.push({ hz, sourceVizId });
    });

    // Local user hovers → forwards to worker.
    s.setCursor(14_150_000, 'viz-1');
    // Meanwhile another window publishes a different cursor → ingest only.
    s.ingestRemoteCursor(14_205_000, 'viz-2');

    expect(s.getCursor()).toEqual({ hz: 14_205_000, sourceVizId: 'viz-2' });
    expect(forwards).toEqual([{ hz: 14_150_000, sourceVizId: 'viz-1' }]);

    s.setCursorForward(null);
  });

  it('setCursorForward(null) detaches the sink cleanly', () => {
    const s = new ReceiverSession('rx-a');
    const fn = vi.fn();
    s.setCursorForward(fn);
    s.setCursorForward(null);

    s.setCursor(14_150_000, 'viz-1');
    expect(fn).not.toHaveBeenCalled();
  });
});
