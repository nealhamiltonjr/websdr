// @vitest-environment node
import { describe, it, expect } from 'vitest';
import {
  initialTuningModel,
  applyMetadata,
  applyControl,
  setActive,
  dropReceiver,
  activeTuning,
  tabLabel,
  formatHzShort,
  CONTROL_DEFAULTS,
  type ReceiverTuningModel,
} from './receiverTuning';

const DEFAULTS = { frequency: 14_205_000, mode: 'USB' as const };

/** Full slice-4.7 control payload as the backend's metadata echo sends it. */
const FULL_META = {
  frequency: 14_100_000,
  mode: 'USB',
  gain: 6.0,
  dspMode: 'classic',
  gainRange: [-20.0, 20.0] as [number, number],
  supportsAgc: false,
};

function fresh(): ReceiverTuningModel {
  return initialTuningModel('rx-default', DEFAULTS);
}

describe('initialTuningModel', () => {
  it('starts with no per-receiver tunings and the default receiver active', () => {
    const m = fresh();
    expect(m.activeId).toBe('rx-default');
    expect(Object.keys(m.tunings)).toHaveLength(0);
    expect(activeTuning(m)).toEqual({ ...DEFAULTS, ...CONTROL_DEFAULTS });
  });

  it('slice-4.7 defaults: auto gain, classic DSP, unknown capability', () => {
    expect(CONTROL_DEFAULTS).toEqual({
      gain: null,
      dspMode: 'classic',
      gainRange: null,
      supportsAgc: false,
    });
    expect(activeTuning(fresh()).gain).toBeNull();
    expect(activeTuning(fresh()).dspMode).toBe('classic');
    expect(activeTuning(fresh()).gainRange).toBeNull();
  });
});

describe('applyMetadata', () => {
  it('creates a tuning entry for a new receiver', () => {
    const m = applyMetadata(fresh(), 'rx-abc', { frequency: 7_200_000, mode: 'LSB' });
    expect(m.tunings['rx-abc']).toEqual({
      frequency: 7_200_000,
      mode: 'LSB',
      ...CONTROL_DEFAULTS,
    });
    // Active receiver untouched — metadata for another receiver doesn't
    // disturb the TuningBar's state.
    expect(activeTuning(m)).toEqual({ ...DEFAULTS, ...CONTROL_DEFAULTS });
  });

  it('updates an existing entry in place (new record, same others)', () => {
    const m0 = applyMetadata(fresh(), 'rx-default', { frequency: 14_100_000, mode: 'USB' });
    const m1 = applyMetadata(m0, 'rx-default', { frequency: 14_150_000, mode: 'AM' });
    expect(m1.tunings['rx-default'].frequency).toBe(14_150_000);
    expect(m1.tunings['rx-default'].mode).toBe('AM');
    // Other receivers' entries survive the spread.
    const m2 = applyMetadata(m1, 'rx-other', { frequency: 1, mode: 'FM' });
    expect(m2.tunings['rx-default'].frequency).toBe(14_150_000);
  });

  it('is a no-op (referentially stable) when the metadata repeats', () => {
    const m0 = applyMetadata(fresh(), 'rx-default', FULL_META);
    const m1 = applyMetadata(m0, 'rx-default', FULL_META);
    expect(m1).toBe(m0); // stream-fps metadata must not thrash the signal
  });

  it('slice-4.7: carries gain, dspMode, and gain capability', () => {
    const m = applyMetadata(fresh(), 'rx-rtl', {
      frequency: 109_000_0000,
      mode: 'ADS-B',
      gain: 32.5,
      dspMode: 'raw',
      gainRange: [0, 49],
      supportsAgc: true,
    });
    const t = m.tunings['rx-rtl'];
    expect(t.gain).toBe(32.5);
    expect(t.dspMode).toBe('raw');
    expect(t.gainRange).toEqual([0, 49]);
    expect(t.supportsAgc).toBe(true);
  });

  it('slice-4.7: defaults when the echo omits the control fields', () => {
    const m = applyMetadata(fresh(), 'rx-abc', { frequency: 7_200_000, mode: 'LSB' });
    expect(m.tunings['rx-abc'].gain).toBeNull();
    expect(m.tunings['rx-abc'].dspMode).toBe('classic');
    expect(m.tunings['rx-abc'].gainRange).toBeNull();
    expect(m.tunings['rx-abc'].supportsAgc).toBe(false);
  });

  it('slice-4.7: a gain change alone breaks referential stability', () => {
    const m0 = applyMetadata(fresh(), 'rx-default', FULL_META);
    const m1 = applyMetadata(m0, 'rx-default', { ...FULL_META, gain: -3.5 });
    expect(m1).not.toBe(m0);
    expect(m1.tunings['rx-default'].gain).toBe(-3.5);
  });
});

describe('applyControl', () => {
  it('optimistically updates frequency for a receiver with no metadata yet', () => {
    const m = applyControl(fresh(), 'rx-abc', { frequency: 3_570_000 });
    expect(m.tunings['rx-abc'].frequency).toBe(3_570_000);
    expect(m.tunings['rx-abc'].mode).toBe('USB');
  });

  it('updates only the given field, keeping the other', () => {
    const m0 = applyMetadata(fresh(), 'rx-abc', { frequency: 3_570_000, mode: 'LSB' });
    const m1 = applyControl(m0, 'rx-abc', { mode: 'CW' });
    expect(m1.tunings['rx-abc'].frequency).toBe(3_570_000);
    expect(m1.tunings['rx-abc'].mode).toBe('CW');
  });

  it('is a no-op when the value did not change', () => {
    const m0 = applyMetadata(fresh(), 'rx-abc', { frequency: 3_570_000, mode: 'LSB' });
    const m1 = applyControl(m0, 'rx-abc', { frequency: 3_570_000 });
    expect(m1).toBe(m0);
  });

  it('slice-4.7: optimistic gain + dspMode (incl. null = auto)', () => {
    const m0 = applyMetadata(fresh(), 'rx-rtl', {
      frequency: 109_000_0000,
      mode: 'ADS-B',
      gain: 32.5,
      dspMode: 'classic',
      gainRange: [0, 49],
      supportsAgc: true,
    });
    const m1 = applyControl(m0, 'rx-rtl', { gain: 40 });
    expect(m1.tunings['rx-rtl'].gain).toBe(40);
    expect(m1.tunings['rx-rtl'].dspMode).toBe('classic');
    const m2 = applyControl(m1, 'rx-rtl', { gain: null });
    expect(m2.tunings['rx-rtl'].gain).toBeNull();
    const m3 = applyControl(m2, 'rx-rtl', { dspMode: 'raw' });
    expect(m3.tunings['rx-rtl'].dspMode).toBe('raw');
    // Capability fields survive control updates.
    expect(m3.tunings['rx-rtl'].gainRange).toEqual([0, 49]);
    expect(m3.tunings['rx-rtl'].supportsAgc).toBe(true);
  });

  it('slice-4.7: setting gain to the same value is a no-op', () => {
    const m0 = applyMetadata(fresh(), 'rx-default', FULL_META);
    const m1 = applyControl(m0, 'rx-default', { gain: 6.0 });
    expect(m1).toBe(m0);
  });
});

describe('setActive', () => {
  it('switches the active receiver', () => {
    const m = setActive(fresh(), 'rx-abc');
    expect(m.activeId).toBe('rx-abc');
    // Active tuning falls back to defaults until that receiver's metadata
    // lands (or its control send populated it).
    expect(activeTuning(m)).toEqual({ ...DEFAULTS, ...CONTROL_DEFAULTS });
  });

  it('is a no-op when already active', () => {
    const m0 = setActive(fresh(), 'rx-abc');
    const m1 = setActive(m0, 'rx-abc');
    expect(m1).toBe(m0);
  });
});

describe('dropReceiver', () => {
  it('forgets the tuning and falls back when the ACTIVE receiver is removed', () => {
    const m0 = setActive(fresh(), 'rx-abc');
    const m1 = applyMetadata(m0, 'rx-abc', { frequency: 109_000_0000, mode: 'ADS-B' });
    const m2 = dropReceiver(m1, 'rx-abc', 'rx-default');
    expect(m2.activeId).toBe('rx-default');
    expect(m2.tunings['rx-abc']).toBeUndefined();
    expect(activeTuning(m2)).toEqual({ ...DEFAULTS, ...CONTROL_DEFAULTS });
  });

  it('keeps the active receiver when a DIFFERENT receiver is removed', () => {
    const m0 = setActive(fresh(), 'rx-abc');
    const m1 = applyMetadata(m0, 'rx-other', { frequency: 1, mode: 'FM' });
    const m2 = dropReceiver(m1, 'rx-other', 'rx-default');
    expect(m2.activeId).toBe('rx-abc');
    expect(m2.tunings['rx-other']).toBeUndefined();
  });

  it('is a no-op for an unknown receiver', () => {
    const m0 = fresh();
    expect(dropReceiver(m0, 'rx-ghost', 'rx-default')).toBe(m0);
  });
});

describe('activeTuning', () => {
  it('reflects metadata + control for the active receiver', () => {
    const m = applyControl(
      applyMetadata(fresh(), 'rx-abc', { frequency: 50_150_000, mode: 'USB' }),
      'rx-abc',
      { frequency: 50_200_000 },
    );
    const t = activeTuning(setActive(m, 'rx-abc'));
    expect(t.frequency).toBe(50_200_000);
    expect(t.mode).toBe('USB');
  });
});

describe('tabLabel', () => {
  it('shows id + frequency once known', () => {
    expect(
      tabLabel(
        'rx-abcdef123456',
        { frequency: 14_205_000, mode: 'USB', ...CONTROL_DEFAULTS },
        { ...DEFAULTS, ...CONTROL_DEFAULTS },
      ),
    ).toBe('rx-abcdef123 · 14.2050M');
  });

  it('omits the frequency part when unknown (0)', () => {
    expect(
      tabLabel('rx-abc', undefined, { ...DEFAULTS, ...CONTROL_DEFAULTS, frequency: 0 }),
    ).toBe('rx-abc');
  });

  it('falls back to defaults tuning when the receiver has no entry', () => {
    expect(tabLabel('rx-abc', undefined, { ...DEFAULTS, ...CONTROL_DEFAULTS })).toBe(
      'rx-abc · 14.2050M',
    );
  });
});

describe('formatHzShort', () => {
  it('formats GHz / MHz / kHz / Hz bands compactly', () => {
    expect(formatHzShort(1_090_000_000)).toBe('1.090G');
    expect(formatHzShort(14_205_000)).toBe('14.2050M');
    expect(formatHzShort(462_562_500)).toBe('462.5625M');
    expect(formatHzShort(7_040_000)).toBe('7.0400M');
    expect(formatHzShort(455_000)).toBe('455.00k');
    expect(formatHzShort(1_000)).toBe('1.00k');
    expect(formatHzShort(750)).toBe('750');
  });
});
