/** VisualizationRegistry — the second core abstraction (ADR-001 § Layer 2).
 *
 *  Every visualization is a SolidJS component with the contract:
 *    { receiverId, config, linkedVizIds? }
 *
 *  Visualizations register themselves here so the WorkspaceManager and the
 *  popout route can dynamically instantiate them by type name.
 *
 *  This module is a PURE LEAF (no imports of any viz component) so viz
 *  modules can call registerViz at module scope without a circular-import
 *  TDZ. The built-in registrations live in ./builtins.ts — import that for
 *  its side effects (see routes/popout.tsx).
 */

import type { JSX } from 'solid-js';

export interface VizConfig {
  /** Type-specific config; each viz defines its own shape. */
  [key: string]: unknown;
}

export interface VizProps {
  receiverId: string;
  config?: VizConfig;
  /** Other viz ids this one should sync with (e.g., waterfall ↔ spectrum crosshair). */
  linkedVizIds?: string[];
}

export type VizComponent = (props: VizProps) => JSX.Element;

export interface VizManifest {
  type: string;
  displayName: string;
  /** Icon name in the lucide-solid icon set (or null for a default). */
  icon: string | null;
  /** Default width when added to a Dockview panel (in px). */
  defaultWidth: number;
  /** Default height when added to a Dockview panel (in px). */
  defaultHeight: number;
  /** Whether this viz is "live" (high-frequency updates) or "static". */
  live: boolean;
  component: VizComponent;
}

const registry = new Map<string, VizManifest>();

export function registerViz(manifest: VizManifest): void {
  if (registry.has(manifest.type)) {
    console.warn(`[VisualizationRegistry] overwriting existing viz type "${manifest.type}"`);
  }
  registry.set(manifest.type, manifest);
}

export function getViz(type: string): VizManifest | undefined {
  return registry.get(type);
}

export function listViz(): VizManifest[] {
  return Array.from(registry.values());
}
