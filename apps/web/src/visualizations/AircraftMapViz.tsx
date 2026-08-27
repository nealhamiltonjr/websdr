/** AircraftMapViz — ADS-B aircraft on a MapLibre map (slice-8).
 *
 *  Thin wrapper over MapViz: pins the family to 'aircraft' and the
 *  default decoder to the in-process 'adsb' plugin (the dump1090
 *  subprocess plugin is also accepted via the ADSB_DECODERS family).
 *  Markers come from positions the dump1090 plugin decodes via CPR —
 *  the in-process plugin doesn't decode positions, so the map works
 *  best with dump1090 attached (one-click CTA still defaults to the
 *  in-process plugin because it has no external binary dep).
 */

import { ADSB_DECODERS } from '@openwebrx-plus/shared-types';
import { registerViz, type VizProps } from './registry';
import MapViz, { type MapVizConfig } from './MapViz';

const CONFIG: MapVizConfig = {
  family: 'aircraft',
  decoderNames: ADSB_DECODERS,
  defaultDecoder: 'adsb',
};

function AircraftMapViz(props: VizProps): import('solid-js').JSX.Element {
  return <MapViz receiverId={props.receiverId} config={CONFIG} />;
}

registerViz({
  type: 'aircraft-map',
  displayName: 'ADS-B Map',
  icon: 'map',
  defaultWidth: 540,
  defaultHeight: 360,
  live: true,
  component: AircraftMapViz,
});

export default AircraftMapViz;
