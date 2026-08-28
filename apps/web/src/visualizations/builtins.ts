/** Built-in visualization registration — import for side effects.
 *
 *  The four built-in vizes register themselves with the VisualizationRegistry
 *  at module scope. This module exists SEPARATELY from registry.ts (which is
 *  a pure leaf) so the registry↔viz circular import can never produce a
 *  temporal-dead-zone error:
 *
 *    registry.ts  (pure store — imports nothing)
 *        ↑ registerViz
 *    WaterfallViz.tsx / SpectrumViz.tsx / …
 *        ↑ side-effect imports
 *    builtins.ts  (this file — the only place that pulls them all in)
 *
 *  Entry points that need the registry populated (the popout route) import
 *  this module; the main workspace gets registration for free because
 *  WorkspaceManager imports the viz components directly.
 */

import './WaterfallViz';
import './SpectrumViz';
import './SMeterViz';
import './FrequencyCounterViz';
import './AircraftListViz';
import './VesselListViz';
import './AircraftMapViz';
import './VesselMapViz';
import './DigiMessageListViz';
import './TextStreamViz';
import './ImageViz';

export { getViz, listViz } from './registry';
