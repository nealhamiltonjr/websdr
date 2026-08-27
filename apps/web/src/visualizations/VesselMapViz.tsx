/** VesselMapViz — AIS vessels on a MapLibre map (slice-8).
 *
 *  Thin wrapper over MapViz: pins the family to 'vessel' and the default
 *  decoder to the in-process 'ais' plugin. Vessel positions come from
 *  Type 1/2/3/18 messages — every panel snapshot refreshes the marker set.
 */

import { AIS_DECODERS } from '@openwebrx-plus/shared-types';
import { registerViz, type VizProps } from './registry';
import MapViz, { type MapVizConfig } from './MapViz';

const CONFIG: MapVizConfig = {
  family: 'vessel',
  decoderNames: AIS_DECODERS,
  defaultDecoder: 'ais',
};

function VesselMapViz(props: VizProps): import('solid-js').JSX.Element {
  return <MapViz receiverId={props.receiverId} config={CONFIG} />;
}

registerViz({
  type: 'vessel-map',
  displayName: 'AIS Map',
  icon: 'map',
  defaultWidth: 540,
  defaultHeight: 360,
  live: true,
  component: VesselMapViz,
});

export default VesselMapViz;
