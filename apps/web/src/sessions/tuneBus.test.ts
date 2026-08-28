// @vitest-environment node
import { describe, it, expect, vi, afterEach } from 'vitest';
import { registerTuneHandler, requestTune, hasTuneHandler } from './tuneBus';
import { ReceiverSession } from './ReceiverSession';

afterEach(() => {
  // Drain the tune bus between tests — registerTuneHandler returns an
  // unregister that tests must call (or we leak handlers across tests).
});

describe('tuneBus', () => {
  it('delivers tune requests to the registered handler', () => {
    const seen: Array<[string, number]> = [];
    const un = registerTuneHandler((rx, hz) => seen.push([rx, hz]));
    expect(hasTuneHandler()).toBe(true);
    requestTune('rx-a', 7_200_000);
    expect(seen).toEqual([['rx-a', 7_200_000]]);
    un();
  });

  it('unregister stops delivery', () => {
    const fn = vi.fn();
    const un = registerTuneHandler(fn);
    un();
    expect(hasTuneHandler()).toBe(false);
    requestTune('rx-a', 14_150_000);
    expect(fn).not.toHaveBeenCalled();
  });

  it('a stale unregister does not clobber a newer handler', () => {
    const first = vi.fn();
    const second = vi.fn();
    const unFirst = registerTuneHandler(first);
    const unSecond = registerTuneHandler(second);
    unFirst(); // must NOT unregister `second`
    requestTune('rx-b', 1_000_000);
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledWith('rx-b', 1_000_000);
    unSecond();
  });

  it('requestTune without a handler degrades gracefully (popout windows)', () => {
    const err = vi.spyOn(console, 'info').mockImplementation(() => {});
    // No handler registered in this test — but previous tests unregistered.
    requestTune('rx-popout', 14_070_000);
    expect(err).toHaveBeenCalled();
    err.mockRestore();
  });
});

describe('ReceiverSession cursor channel (crosshair sync)', () => {
  it('setCursor emits state to subscribers', () => {
    const s = new ReceiverSession('rx-1');
    const events: Array<{ hz: number; sourceVizId: string } | null> = [];
    const unsub = s.cursorStream.subscribe((c) => events.push(c ? { ...c } : null));

    s.setCursor(14_150_000, 'waterfall-1');
    s.setCursor(14_200_000, 'spectrum-2');
    s.setCursor(null, 'waterfall-1');

    expect(events).toEqual([
      { hz: 14_150_000, sourceVizId: 'waterfall-1' },
      { hz: 14_200_000, sourceVizId: 'spectrum-2' },
      null,
    ]);
    unsub();
  });

  it('getCursor retains the last value for late-mounting panels', () => {
    const s = new ReceiverSession('rx-2');
    expect(s.getCursor()).toBeNull();
    s.setCursor(7_200_000, 'waterfall-9');
    // No subscription — the value is still queryable.
    expect(s.getCursor()).toEqual({ hz: 7_200_000, sourceVizId: 'waterfall-9' });
    s.setCursor(null, 'waterfall-9');
    expect(s.getCursor()).toBeNull();
  });

  it('cursor state is per-session (receivers do not share crosshairs)', () => {
    const a = new ReceiverSession('rx-a');
    const b = new ReceiverSession('rx-b');
    a.setCursor(1_000_000, 'waterfall-1');
    b.setCursor(2_000_000, 'waterfall-2');
    expect(a.getCursor()?.hz).toBe(1_000_000);
    expect(b.getCursor()?.hz).toBe(2_000_000);
  });
});
