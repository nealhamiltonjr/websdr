/** GroupActions — dockview group header actions (ADR-001 § Layer 3).
 *
 *  Rendered in the RIGHT slot of every group's tab strip. Provides:
 *    - "+ viz" — a dropdown listing every registered visualization; picking
 *      one adds it as a new tab in THIS group (direction: 'within').
 *    - "⤢" — pops the active panel out into its own window
 *      (route contract: /popout/:vizType?receiverId=...).
 *
 *  Everything needed is available on the dockview header-actions props:
 *  the group's panels (their params carry receiverId + vizType) and the
 *  containerApi for addPanel. No external state required.
 */

import { For, createSignal, Show } from 'solid-js';
import type { Component } from 'solid-js';
import type { IDockviewHeaderActionsProps } from 'dockview-core';
import { listViz } from '../../visualizations/builtins';
import { panelIdFor, panelTitle, type VizPanelParams } from './layoutModel';

export const GroupActions: Component<IDockviewHeaderActionsProps> = (props) => {
  const [menuOpen, setMenuOpen] = createSignal(false);

  const receiverId = (): string | undefined => {
    const panel = props.activePanel ?? props.panels[0];
    const params = panel?.params as VizPanelParams | undefined;
    return params?.receiverId;
  };

  const activeParams = (): VizPanelParams | undefined =>
    (props.activePanel ?? props.panels[0])?.params as VizPanelParams | undefined;

  const popOutActive = () => {
    const params = activeParams();
    if (!params) return;
    const url = `/popout/${params.vizType}?receiverId=${encodeURIComponent(params.receiverId)}`;
    window.open(url, `${params.vizType}-${params.receiverId}-popout`, 'width=1024,height=768');
  };

  const addViz = (vizType: string, displayName: string) => {
    setMenuOpen(false);
    const rx = receiverId();
    if (!rx) return;
    const anchor = props.activePanel ?? props.panels[0];
    props.containerApi.addPanel({
      id: panelIdFor(rx, vizType),
      component: 'viz',
      params: { vizType, receiverId: rx },
      title: panelTitle(displayName, rx),
      ...(anchor ? { position: { referencePanel: anchor.id, direction: 'within' as const } } : {}),
    });
  };

  return (
    <div class="relative flex items-center gap-1 pr-1">
      {/* Add-viz dropdown */}
      <div class="relative">
        <button
          type="button"
          class="rounded bg-base-800 px-1.5 py-0.5 font-mono text-[10px] text-base-300 hover:bg-base-700 hover:text-base-100"
          onClick={() => setMenuOpen((v) => !v)}
          title="Add a visualization to this group"
        >
          + viz
        </button>
        <Show when={menuOpen()}>
          <div class="absolute right-0 top-full z-50 mt-1 w-48 rounded border border-base-700 bg-base-850 py-1 shadow-xl">
            <For each={listViz()}>
              {(m) => (
                <button
                  type="button"
                  class="block w-full px-3 py-1 text-left font-mono text-[11px] text-base-200 hover:bg-base-700 hover:text-base-100"
                  onClick={() => addViz(m.type, m.displayName)}
                >
                  {m.displayName}
                  <Show when={m.live}>
                    <span class="ml-1 text-cyan-450" title="live / high-frequency updates">
                      ●
                    </span>
                  </Show>
                </button>
              )}
            </For>
          </div>
        </Show>
      </div>

      {/* Pop out the active panel */}
      <button
        type="button"
        class="rounded bg-base-800 px-1.5 py-0.5 font-mono text-[10px] text-base-300 hover:bg-base-700 hover:text-base-100"
        onClick={() => popOutActive()}
        title="Pop the active panel out into its own window"
      >
        ⤢
      </button>
    </div>
  );
};
