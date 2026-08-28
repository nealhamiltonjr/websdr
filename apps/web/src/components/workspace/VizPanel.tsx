/** VizPanel — the single dockview content component used by every panel.
 *
 *  Dockview instantiates components by NAME from the `components` map; we
 *  register exactly one ('viz') and dispatch on `params.vizType` through the
 *  VisualizationRegistry (ADR-001 § Layer 2). This keeps panel identity
 *  (serialized id + params) stable across layout persistence, while the set
 *  of available visualizations stays open-ended.
 *
 *  The dockview-solid bridge mounts each panel in its own Solid root with a
 *  store-backed props object, so reactivity inside the viz components works
 *  unchanged (they already subscribe to ReceiverSession subjects).
 */

import { Show } from 'solid-js';
import { Dynamic } from 'solid-js/web';
import type { Component } from 'solid-js';
import type { IDockviewPanelProps } from 'dockview-core';
import { getViz } from '../../visualizations/builtins';
import type { VizPanelParams } from './layoutModel';

export const VizPanel: Component<IDockviewPanelProps<VizPanelParams>> = (props) => {
  const manifest = () => getViz(props.params?.vizType ?? '');

  return (
    <div
      class="h-full w-full overflow-hidden bg-base-950"
      data-viz={props.params?.vizType ?? ''}
      data-receiver={props.params?.receiverId ?? ''}
    >
      <Show
        when={manifest()}
        keyed
        fallback={
          <div class="flex h-full w-full items-center justify-center font-mono text-xs text-base-400">
            Unknown visualization: {props.params?.vizType}
          </div>
        }
      >
        {(m) => <Dynamic component={m.component} receiverId={props.params!.receiverId} config={props.params!.config} />}
      </Show>
    </div>
  );
};
