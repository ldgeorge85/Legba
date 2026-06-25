/**
 * v4 panel — The World map (geotemporal), rendered with Leaflet (DOM/SVG/Canvas,
 * not WebGL) so it composites and renders inside a Dockview tile. Docks like any
 * other panel; clicking a cluster/point drives the shared selection store, which
 * the Why / Casework panels react to.
 *
 * `[&_.leaflet-container]:z-0` gives Leaflet's container a stacking context so its
 * internal panes (z 200-700) stay contained and the LayerPanel/Drawer overlays
 * render on top.
 */
import { PanelBoundary } from './PanelBoundary'
import LeafletWorldMap from '@/v4/world/LeafletWorldMap'
import LayerPanel from '@/v4/world/LayerPanel'
import Drawer from '@/v4/world/Drawer'
import TimeScrubber from '@/v4/world/TimeScrubber'
import KpiStrip from '@/v4/world/KpiStrip'

export default function MapPanel() {
  return (
    <PanelBoundary>
      <div className="flex h-full w-full flex-col bg-surface-300">
        <KpiStrip />
        <div className="relative min-h-[320px] flex-1 [&_.leaflet-container]:z-0">
          <LeafletWorldMap />
          <LayerPanel />
          <Drawer />
        </div>
        <TimeScrubber />
      </div>
    </PanelBoundary>
  )
}
