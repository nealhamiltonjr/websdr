// @vitest-environment node
/** Unit tests for the Dockview workspace layout model (pure logic). */

import { describe, expect, it } from 'vitest';
import type { SerializedDockview } from 'dockview-core';
import {
  DEFAULT_PANELS,
  LAYOUT_STORAGE_KEY,
  panelIdFor,
  panelTitle,
  receiverOfPanelId,
  stripReceivers,
  validateLayout,
} from './layoutModel';

/** Build a realistic serialized dockview layout:
 *
 *   root (branch, vertical)
 *   ├── branch (horizontal)
 *   │   ├── leaf G1: [wf-rx-default, sp-rx-default]     ← rx-default
 *   │   └── leaf G2: [sm-rx-default, fc-rx-default]     ← rx-default
 *   └── leaf G3: [ac-rx-default, wf-rx-b]               ← mixed
 *
 *   plus a floating group [wf-rx-c] and a popout group [sm-rx-c].
 */
function fixtureLayout(): SerializedDockview {
  const panel = (id: string, receiverId: string) => ({
    id,
    contentComponent: 'viz',
    tabComponent: undefined,
    params: { vizType: id.split('-')[0], receiverId },
    title: `${id} · ${receiverId.slice(0, 8)}`,
  });
  return {
    grid: {
      root: {
        type: 'branch',
        data: [
          {
            type: 'branch',
            data: [
              { type: 'leaf', data: { views: ['wf-rx-default', 'sp-rx-default'], activeView: 'wf-rx-default', id: 'G1' }, size: 400 },
              { type: 'leaf', data: { views: ['sm-rx-default', 'fc-rx-default'], activeView: 'sm-rx-default', id: 'G2' }, size: 220 },
            ],
            size: 500,
          },
          { type: 'leaf', data: { views: ['ac-rx-default', 'wf-rx-b'], activeView: 'ac-rx-default', id: 'G3' }, size: 150 },
        ],
        size: 800,
      },
      height: 800,
      width: 1200,
      orientation: 'VERTICAL',
    },
    panels: {
      'wf-rx-default': panel('wf-rx-default', 'rx-default'),
      'sp-rx-default': panel('sp-rx-default', 'rx-default'),
      'sm-rx-default': panel('sm-rx-default', 'rx-default'),
      'fc-rx-default': panel('fc-rx-default', 'rx-default'),
      'ac-rx-default': panel('ac-rx-default', 'rx-default'),
      'wf-rx-b': panel('wf-rx-b', 'rx-b'),
      'wf-rx-c': panel('wf-rx-c', 'rx-c'),
      'sm-rx-c': panel('sm-rx-c', 'rx-c'),
    },
    activeGroup: 'G1',
    floatingGroups: [
      { data: { views: ['wf-rx-c'], activeView: 'wf-rx-c', id: 'G4' }, position: { left: 0, top: 0, height: 200, width: 200 } },
    ],
    popoutGroups: [
      { data: { views: ['sm-rx-c'], activeView: 'sm-rx-c', id: 'G5' }, position: { left: 0, top: 0, height: 200, width: 200 } },
    ],
  } as unknown as SerializedDockview;
}

describe('validateLayout', () => {
  it('accepts a well-formed layout', () => {
    expect(validateLayout(fixtureLayout())).not.toBeNull();
  });

  it('rejects non-objects, missing grid/root/panels', () => {
    expect(validateLayout(null)).toBeNull();
    expect(validateLayout('nope')).toBeNull();
    expect(validateLayout({})).toBeNull();
    expect(validateLayout({ grid: {} })).toBeNull();
    expect(validateLayout({ grid: { root: { type: 'leaf', data: { views: ['a'] } } } })).toBeNull();
  });

  it('rejects malformed nodes and panels', () => {
    const bad = fixtureLayout() as any;
    bad.grid.root.data[0].data[0].type = 'weird';
    expect(validateLayout(bad)).toBeNull();

    const badPanel = fixtureLayout() as any;
    badPanel.panels['wf-rx-default'] = { id: 42 };
    expect(validateLayout(badPanel)).toBeNull();
  });
});

describe('stripReceivers', () => {
  it('keeps everything when all receivers are live', () => {
    const res = stripReceivers(fixtureLayout(), new Set(['rx-default', 'rx-b', 'rx-c']));
    expect(res.layout).not.toBeNull();
    expect(Object.keys((res.layout as any).panels)).toHaveLength(8);
    expect(res.receiversWithPanels).toEqual(new Set(['rx-default', 'rx-b', 'rx-c']));
  });

  it('removes every panel of a dead receiver and drops emptied groups', () => {
    const res = stripReceivers(fixtureLayout(), new Set(['rx-default', 'rx-b']));
    const layout = res.layout as any;
    // rx-c panels gone (grid leaf G3 keeps ac-rx-default, but floating G4 and
    // popout G5 — both pure rx-c — must be dropped entirely)
    expect(layout.panels['wf-rx-c']).toBeUndefined();
    expect(layout.panels['sm-rx-c']).toBeUndefined();
    expect(layout.floatingGroups).toHaveLength(0);
    expect(layout.popoutGroups).toHaveLength(0);
    expect(res.receiversWithPanels.has('rx-c')).toBe(false);
    // leaf G3 survives with both live panels (rx-default + rx-b)
    const g3 = findLeaf(layout.grid.root, 'G3');
    expect(g3.data.views).toEqual(['ac-rx-default', 'wf-rx-b']);
  });

  it('drops an emptied grid leaf and collapses the tree', () => {
    const res = stripReceivers(fixtureLayout(), new Set(['rx-default']));
    const layout = res.layout as any;
    // G3 had ac-rx-default (rx-default) + wf-rx-b (rx-b dead) → G3 keeps 1 view
    const g3 = findLeaf(layout.grid.root, 'G3');
    expect(g3.data.views).toEqual(['ac-rx-default']);
    // G1/G2 untouched
    expect(findLeaf(layout.grid.root, 'G1').data.views).toHaveLength(2);
    expect(findLeaf(layout.grid.root, 'G2').data.views).toHaveLength(2);
    expect(layout.activeGroup).toBe('G1');
  });

  it('returns null layout when nothing survives', () => {
    const res = stripReceivers(fixtureLayout(), new Set(['rx-zzz']));
    expect(res.layout).toBeNull();
    expect(res.receiversWithPanels.size).toBe(0);
  });

  it('re-points activeView when the active panel is removed', () => {
    const res = stripReceivers(fixtureLayout(), new Set(['rx-b', 'rx-c']));
    const layout = res.layout as any;
    const g1 = findLeaf(layout.grid.root, 'G1');
    // both G1 views were rx-default → G1 dropped entirely
    expect(g1).toBeNull();
    // G2 also rx-default → dropped; only G3 (wf-rx-b) remains
    expect(findLeaf(layout.grid.root, 'G3').data.views).toEqual(['wf-rx-b']);
    // activeGroup pointed at the dropped G1 → removed
    expect(layout.activeGroup).toBeUndefined();
  });

  it('does not mutate the input layout', () => {
    const input = fixtureLayout();
    const snapshot = JSON.stringify(input);
    stripReceivers(input, new Set(['rx-default']));
    expect(JSON.stringify(input)).toBe(snapshot);
  });
});

describe('panel ids and titles', () => {
  it('panelIdFor produces unique, parseable ids', () => {
    const a = panelIdFor('rx-default', 'waterfall');
    const b = panelIdFor('rx-default', 'waterfall');
    expect(a).not.toBe(b);
    expect(receiverOfPanelId(a)).toBe('rx-default');
    expect(receiverOfPanelId(b)).toBe('rx-default');
    expect(receiverOfPanelId('no-separator')).toBeUndefined();
  });

  it('panelTitle shows the short receiver id', () => {
    expect(panelTitle('Waterfall', 'rx-default')).toBe('Waterfall · rx-defau');
    expect(panelTitle('S-Meter', '12345678-90ab-')).toBe('S-Meter · 12345678');
  });
});

describe('DEFAULT_PANELS recipe', () => {
  it('starts with the waterfall as anchor and covers the five built-ins', () => {
    expect(DEFAULT_PANELS[0].vizType).toBe('waterfall');
    expect(DEFAULT_PANELS[0].anchor).toBeUndefined();
    expect(DEFAULT_PANELS.map((p) => p.vizType)).toEqual([
      'waterfall',
      'spectrum',
      'smeter',
      'frequency-counter',
      'aircraft-list',
    ]);
    // every non-first panel must anchor to an earlier spec
    const seen = new Set([DEFAULT_PANELS[0].vizType]);
    for (const spec of DEFAULT_PANELS.slice(1)) {
      expect(spec.anchor, `${spec.vizType} must anchor`).toBeDefined();
      expect(seen.has(spec.anchor as string)).toBe(true);
      seen.add(spec.vizType);
    }
  });
});

function findLeaf(node: any, groupId: string): any {
  if (!node) return null;
  if (node.type === 'leaf') return node.data.id === groupId ? node : null;
  for (const child of node.data ?? []) {
    const hit = findLeaf(child, groupId);
    if (hit) return hit;
  }
  return null;
}

describe('storage key', () => {
  it('is versioned', () => {
    expect(LAYOUT_STORAGE_KEY).toBe('openwebrx-plus.layout.v1');
  });
});
