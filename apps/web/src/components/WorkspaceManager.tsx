/** WorkspaceManager — the main window's layout root (ADR-001 § Layer 3).
 *
 *  Slice-4: the CSS-grid stub is replaced by a real Dockview workspace
 *  (dockview-solid). Responsibilities:
 *
 *    - ONE DockviewSolid instance hosting every panel of every receiver
 *      (panel = one visualization of one receiver, dispatched through the
 *      VisualizationRegistry — see workspace/VizPanel.tsx)
 *    - default panel recipe per freshly-appeared receiver
 *      (workspace/layoutModel.ts DEFAULT_PANELS)
 *    - reconciliation: receiver destroyed → its panels are removed;
 *      receiver spawned → default panels are docked right of the last group
 *    - layout persistence: api.toJSON() → localStorage (debounced),
 *      stripReceivers() → api.fromJSON() on boot
 *    - group header actions: "+ viz" dropdown + "⤢ pop out"
 *      (workspace/GroupActions.tsx)
 *
 *  Receivers themselves (spawn/destroy/tuning) stay owned by routes/main.tsx
 *  — the receiver tab bar above remains the source of truth for WHAT is
 *  alive; this component only decides WHERE each receiver's vizes render.
 */

import { createEffect, createSignal, onCleanup, onMount, Show } from 'solid-js';
import type { Component } from 'solid-js';
import { DockviewDefaultTab, DockviewSolid } from 'dockview-solid';
import type {
  AddPanelOptions,
  DockviewApi,
  DockviewReadyEvent,
  IWatermarkPanelProps,
} from 'dockview-core';
import { getViz } from '../visualizations/builtins';
import { VizPanel } from './workspace/VizPanel';
import { GroupActions } from './workspace/GroupActions';
import {
  DEFAULT_PANELS,
  browserLayoutStore,
  panelIdFor,
  panelTitle,
  stripReceivers,
  type VizPanelParams,
} from './workspace/layoutModel';

/** Module-scoped context the watermark reads. Dockview instantiates the
 *  watermark component itself with only containerApi props, so live state
 *  (receiver ids / restore action) flows through this object instead. */
const workspaceContext = {
  receiverIds: (): string[] => [],
  restoreDefaults: (): void => {},
};

const WorkspaceWatermark: Component<IWatermarkPanelProps> = () => (
  <div class="flex h-full w-full flex-col items-center justify-center gap-3 bg-base-950 font-mono text-sm text-base-400">
    <Show
      when={workspaceContext.receiverIds().length > 0}
      fallback={
        <span>
          No receivers. Click <span class="text-amber-450">+ receiver</span> in the top bar to spawn one.
        </span>
      }
    >
      <span>All panels were closed.</span>
      <button
        type="button"
        class="rounded bg-base-800 px-3 py-1 font-mono text-xs text-base-200 hover:bg-base-700 hover:text-base-100"
        onClick={() => workspaceContext.restoreDefaults()}
      >
        restore default panels
      </button>
    </Show>
  </div>
);

export interface WorkspaceManagerProps {
  /** List of receiver ids to render. The top bar spawns more via REST. */
  receiverIds: () => string[];
  /** Called when the user removes a receiver from the workspace (NOT when
   *  the backend destroys it — that's handled via the registry). */
  onRemoveReceiver?: (id: string) => void;
}

export function WorkspaceManager(props: WorkspaceManagerProps) {
  let api: DockviewApi | undefined;
  const [panelCount, setPanelCount] = createSignal(0);
  const [groupCount, setGroupCount] = createSignal(0);
  let saveTimer: ReturnType<typeof setTimeout> | undefined;

  const persist = () => {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      if (api) {
        try {
          browserLayoutStore.save(api.toJSON());
        } catch (e) {
          console.warn('[workspace] failed to serialize layout', e);
        }
      }
    }, 500);
  };

  const refreshCounts = () => {
    if (!api) return;
    setPanelCount(api.panels.length);
    setGroupCount(api.groups.length);
  };

  /** Dock the default panel set for one receiver. The receiver's first
   *  panel anchors right of the current last group (or becomes the root
   *  panel when the workspace is empty); the rest anchor relative to it,
   *  reproducing the classic block: waterfall+spectrum | smeter+freq /
   *  aircraft list spanning the bottom. */
  const addDefaultPanels = (receiverId: string) => {
    if (!api) return;
    const idsByViz = new Map<string, string>();
    const lastGroup = api.groups.length > 0 ? api.groups[api.groups.length - 1] : undefined;

    for (const spec of DEFAULT_PANELS) {
      const manifest = getViz(spec.vizType);
      if (!manifest) continue;
      const id = panelIdFor(receiverId, spec.vizType);
      idsByViz.set(spec.vizType, id);
      const anchorId = spec.anchor ? idsByViz.get(spec.anchor) : undefined;

      const options = {
        id,
        component: 'viz',
        params: { vizType: spec.vizType, receiverId },
        title: panelTitle(manifest.displayName, receiverId),
      } as AddPanelOptions<VizPanelParams>;
      if (anchorId) {
        options.position = { referencePanel: anchorId, direction: spec.direction };
      } else if (lastGroup) {
        options.position = { referenceGroup: lastGroup.id, direction: 'right' };
      }
      api.addPanel(options);
    }
  };

  /** Dead receivers lose their panels; panel-less receivers gain defaults. */
  const reconcile = () => {
    if (!api) return;
    const live = new Set(props.receiverIds());

    const dead = api.panels.filter((panel) => {
      const rx = (panel.params as VizPanelParams | undefined)?.receiverId;
      return !rx || !live.has(rx);
    });
    for (const panel of dead) {
      api.removePanel(panel); // empty groups auto-drop
    }

    const present = new Set<string>();
    for (const panel of api.panels) {
      const rx = (panel.params as VizPanelParams | undefined)?.receiverId;
      if (rx) present.add(rx);
    }
    for (const rx of props.receiverIds()) {
      if (!present.has(rx)) addDefaultPanels(rx);
    }
    refreshCounts();
    persist();
  };

  const onReady = (event: DockviewReadyEvent) => {
    api = event.api;

    // Restore the persisted layout (if any), pruned of dead receivers.
    const saved = browserLayoutStore.load();
    if (saved) {
      const { layout } = stripReceivers(saved, new Set(props.receiverIds()));
      if (layout) {
        try {
          api.fromJSON(layout);
        } catch (e) {
          console.warn('[workspace] saved layout failed to restore — rebuilding defaults', e);
          api.clear();
        }
      }
    }

    reconcile();

    api.onDidLayoutChange(() => persist());
    api.onDidAddPanel(refreshCounts);
    api.onDidRemovePanel(refreshCounts);
    api.onDidAddGroup(refreshCounts);
    api.onDidRemoveGroup(refreshCounts);
    persist();
  };

  // Receivers spawn/destroy (from main.tsx) → reconcile the panel set.
  createEffect(() => {
    void props.receiverIds(); // track
    reconcile();
  });

  // Feed the watermark context.
  onMount(() => {
    workspaceContext.receiverIds = () => props.receiverIds();
    workspaceContext.restoreDefaults = () => reconcile();
  });

  onCleanup(() => {
    if (saveTimer) clearTimeout(saveTimer);
    // NOTE: the DockviewApi itself is disposed by DockviewSolid's own
    // onCleanup — do not double-dispose here.
  });

  return (
    <div class="flex h-full w-full flex-col bg-base-900">
      {/* The dockable workspace */}
      <main class="min-h-0 flex-1">
        <div class="dockview-theme-abyss owrx-dockview h-full w-full">
          <DockviewSolid
            components={{ viz: VizPanel }}
            defaultTabComponent={DockviewDefaultTab}
            rightHeaderActionsComponent={GroupActions}
            watermarkComponent={WorkspaceWatermark}
            onReady={onReady}
          />
        </div>
      </main>

      {/* Status bar */}
      <div class="flex h-7 shrink-0 items-center justify-between border-t border-base-800 bg-base-900 px-3 font-mono text-[11px] text-base-400">
        <span>
          {props.receiverIds().length} receiver(s) · {panelCount()} panel(s) · {groupCount()} group(s)
        </span>
        <span>drag tabs / splitters to rearrange · layout persists</span>
      </div>
    </div>
  );
}
