/** layoutModel — pure logic for the Dockview workspace (ADR-001 § Layer 3).
 *
 *  Everything in this module is side-effect-free except the explicit
 *  localStorage load/save entry points (which are injectable for tests).
 *  The WorkspaceManager owns the DockviewApi; this module owns:
 *
 *    - the serialized-layout <-> localStorage round trip (+ shape validation)
 *    - stripping panels of dead receivers from a serialized layout BEFORE
 *      fromJSON (avoids mount/unmount churn of viz components for receivers
 *      that no longer exist)
 *    - the default panel recipe for a freshly-appeared receiver
 *    - unique panel-id generation and tab-title formatting
 *
 *  Layout version key: bump LAYOUT_VERSION when the params shape changes
 *  incompatibly — old saved layouts are then discarded (default rebuild).
 */

import type { SerializedDockview } from 'dockview-core';
import type { VizConfig } from '../../visualizations/registry';

export const LAYOUT_VERSION = 1;
export const LAYOUT_STORAGE_KEY = `openwebrx-plus.layout.v${LAYOUT_VERSION}`;

/** params carried by every dockview panel of component 'viz'. */
export interface VizPanelParams {
  vizType: string;
  receiverId: string;
  config?: VizConfig;
}

/** The default viz recipe for a new receiver, in add-order.
 *
 *  Positions are resolved by the WorkspaceManager (it owns the api); this
 *  list only fixes WHAT gets spawned and the relative anchoring:
 *    - waterfall first (root anchor)
 *    - spectrum below the waterfall
 *    - s-meter right of the waterfall, frequency below the s-meter
 *    - aircraft list spanning the bottom
 */
export interface DefaultPanelSpec {
  vizType: string;
  /** Anchor: which previously-added panel of the same receiver to position
   *  relative to. Undefined = the receiver's first panel (no position). */
  anchor?: string;
  direction: 'below' | 'right' | 'within';
}

export const DEFAULT_PANELS: readonly DefaultPanelSpec[] = [
  { vizType: 'waterfall', direction: 'below' /* unused for first panel */ },
  { vizType: 'spectrum', anchor: 'waterfall', direction: 'below' },
  { vizType: 'smeter', anchor: 'waterfall', direction: 'right' },
  { vizType: 'frequency-counter', anchor: 'smeter', direction: 'below' },
  { vizType: 'aircraft-list', anchor: 'spectrum', direction: 'below' },
];

/** Monotonic suffix so duplicate (receiver, vizType) pairs stay unique. */
let duplicateCounter = 0;

/** Stable, parseable panel id: `${receiverId}::${vizType}#n`. */
export function panelIdFor(receiverId: string, vizType: string): string {
  return `${receiverId}::${vizType}#${++duplicateCounter}`;
}

/** Extract the receiverId from a panel id produced by panelIdFor. */
export function receiverOfPanelId(id: string): string | undefined {
  const idx = id.indexOf('::');
  return idx === -1 ? undefined : id.slice(0, idx);
}

/** Tab title shown in the dockview tab strip. */
export function panelTitle(displayName: string, receiverId: string): string {
  return `${displayName} · ${receiverId.slice(0, 8)}`;
}

/* ------------------------------------------------------------------ */
/* localStorage persistence                                            */
/* ------------------------------------------------------------------ */

export interface LayoutStore {
  load(): SerializedDockview | null;
  save(layout: SerializedDockview): void;
  clear(): void;
}

export const browserLayoutStore: LayoutStore = {
  load() {
    try {
      const raw = globalThis.localStorage?.getItem(LAYOUT_STORAGE_KEY);
      if (!raw) return null;
      return validateLayout(JSON.parse(raw));
    } catch {
      return null;
    }
  },
  save(layout) {
    try {
      globalThis.localStorage?.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(layout));
    } catch {
      /* private mode / quota — layout persistence is best-effort */
    }
  },
  clear() {
    try {
      globalThis.localStorage?.removeItem(LAYOUT_STORAGE_KEY);
    } catch {
      /* ignore */
    }
  },
};

/** Structural validation — protects fromJSON from garbage. */
export function validateLayout(data: unknown): SerializedDockview | null {
  if (typeof data !== 'object' || data === null) return null;
  const d = data as Record<string, unknown>;
  const grid = d.grid;
  if (typeof grid !== 'object' || grid === null) return null;
  const g = grid as Record<string, unknown>;
  if (!g.root || typeof g.root !== 'object') return null;
  if (!isSerializedNode(g.root)) return null;
  if (typeof d.panels !== 'object' || d.panels === null) return null;
  const panels = d.panels as Record<string, unknown>;
  for (const id of Object.keys(panels)) {
    const p = panels[id];
    if (typeof p !== 'object' || p === null) return null;
    const rec = p as Record<string, unknown>;
    if (typeof rec.id !== 'string' || typeof rec.contentComponent !== 'string') return null;
  }
  return data as SerializedDockview;
}

function isSerializedNode(node: unknown): boolean {
  if (typeof node !== 'object' || node === null) return false;
  const n = node as Record<string, unknown>;
  if (n.type !== 'leaf' && n.type !== 'branch') return false;
  if (n.type === 'branch') {
    return Array.isArray(n.data) && (n.data as unknown[]).every(isSerializedNode);
  }
  // leaf: data = { views: string[], id: string, ... }
  const data = n.data;
  if (typeof data !== 'object' || data === null) return false;
  const leaf = data as Record<string, unknown>;
  return Array.isArray(leaf.views) && leaf.views.every((v) => typeof v === 'string');
}

/* ------------------------------------------------------------------ */
/* dead-receiver stripping (pre-fromJSON surgery)                      */
/* ------------------------------------------------------------------ */

interface SerializedNodeShape {
  type: 'leaf' | 'branch';
  data: unknown;
  size?: number;
  visible?: boolean;
}

interface GroupViewStateShape {
  views: string[];
  activeView?: string;
  id: string;
}

/** Result of stripReceivers: a deep-cloned layout without dead receivers'
 *  panels, plus the set of live receivers that kept at least one panel.
 *  `layout` is null when NOTHING survived (entire grid collapsed) — callers
 *  must then build the default layout from scratch. */
export interface StripResult {
  layout: SerializedDockview | null;
  receiversWithPanels: Set<string>;
}

/** Clone the layout, removing every panel whose params.receiverId is not in
 *  `liveReceiverIds`. Empty groups (and empty floating/popout groups) are
 *  dropped; empty branches collapse. Mutates nothing. */
export function stripReceivers(
  layout: SerializedDockview,
  liveReceiverIds: ReadonlySet<string>,
): StripResult {
  const receiversWithPanels = new Set<string>();
  const deadPanels = new Set<string>();

  // 1. classify panels
  const panelsIn: Record<string, any> = (layout as any).panels ?? {};
  const panelsOut: Record<string, any> = {};
  for (const [id, panel] of Object.entries(panelsIn)) {
    const rx = (panel as any)?.params?.receiverId;
    if (typeof rx === 'string' && liveReceiverIds.has(rx)) {
      panelsOut[id] = panel;
      receiversWithPanels.add(rx);
    } else {
      deadPanels.add(id);
    }
  }

  const keepView = (id: string) => !deadPanels.has(id);

  // 2. walk the grid
  const rootIn = (layout.grid.root as unknown) as SerializedNodeShape;
  const rootOut = walkNode(rootIn, keepView);
  if (!rootOut) {
    // Nothing survived — signal "unusable" to the caller.
    return { layout: null, receiversWithPanels };
  }
  const out: any = { ...layout, grid: { ...layout.grid, root: rootOut as any } };

  // 3. floating / popout groups
  if (Array.isArray((layout as any).floatingGroups)) {
    out.floatingGroups = (layout as any).floatingGroups.filter(
      (fg: any) => Array.isArray(fg?.data?.views) && fg.data.views.some(keepView),
    );
  }
  if (Array.isArray((layout as any).popoutGroups)) {
    out.popoutGroups = (layout as any).popoutGroups.filter(
      (pg: any) => Array.isArray(pg?.data?.views) && pg.data.views.some(keepView),
    );
  }

  // 4. activeGroup sanity: drop if the group disappeared
  if (typeof out.activeGroup === 'string' && !groupExists(rootOut, out.activeGroup)) {
    delete out.activeGroup;
  }

  out.panels = panelsOut;
  return { layout: out, receiversWithPanels };
}

function walkNode(node: SerializedNodeShape, keepView: (id: string) => boolean): SerializedNodeShape | null {
  if (node.type === 'leaf') {
    const data = node.data as GroupViewStateShape;
    const views = (data.views ?? []).filter(keepView);
    if (views.length === 0) return null;
    const activeView = data.activeView && views.includes(data.activeView) ? data.activeView : views[0];
    return { ...node, data: { ...data, views, activeView } };
  }
  // branch
  const children = Array.isArray(node.data) ? (node.data as SerializedNodeShape[]) : [];
  const kept = children
    .map((child) => walkNode(child, keepView))
    .filter((child): child is SerializedNodeShape => child !== null);
  if (kept.length === 0) return null;
  // A branch with a single child is still valid for fromJSON (dockview
  // normalizes); keep it to preserve sizes.
  return { ...node, data: kept };
}

function groupExists(node: SerializedNodeShape | null, groupId: string): boolean {
  if (!node) return false;
  if (node.type === 'leaf') {
    return (node.data as GroupViewStateShape).id === groupId;
  }
  const children = Array.isArray(node.data) ? (node.data as SerializedNodeShape[]) : [];
  return children.some((child) => groupExists(child, groupId));
}
