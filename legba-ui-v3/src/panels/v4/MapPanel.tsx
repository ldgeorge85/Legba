/**
 * v4 panel — The World map (geotemporal).
 *
 * S7-T5 ships TWO renderers behind `@/lib/mapEngine` (both wired to the shared
 * world store; LayerPanel / Drawer / TimeScrubber chrome is shared):
 *
 *  - MAPLIBRE (default) — WebGL, rendered through the TileWebGLOverlay harness:
 *    a `position: fixed` canvas portalled to document.body and position-synced to
 *    this tile's rect, which routes the GPU canvas OUT of the Dockview tile
 *    transform — the thing that made a plain in-tile maplibre/sigma canvas paint
 *    BLACK (S7-T2 spike, dockview v4+v7). maplibre adds the banded-verdict
 *    choropleth + a signal-density heatmap over the signal/finding/situation
 *    layers.
 *  - LEAFLET (fallback, `?map=leaflet`) — DOM/SVG, always composites in a
 *    Dockview tile.
 *
 * MAPLIBRE is the DEFAULT (S7-T5 integration, 2026-07-02): the overlay's
 * not-black rendering was confirmed via the containerized render harness, and a
 * WebGL-availability guard downgrades to Leaflet on any browser that cannot
 * create a WebGL context, so a black panel can never ship. `?map=leaflet` (or
 * `localStorage.legba_map_engine`) forces the fallback. See mapEngine.ts.
 *
 * The KPI stat tiles were already lifted OUT of the map into the standalone
 * `v4.kpi` strip (UI direction §"map panel").
 */
import type { ReactNode } from 'react'
import { PanelBoundary } from './PanelBoundary'
import { TileWebGLOverlay } from '@/components/TileWebGLOverlay'
import LeafletWorldMap from '@/v4/world/LeafletWorldMap'
import MapLibreWorldMap from '@/v4/world/MapLibreWorldMap'
import LayerPanel from '@/v4/world/LayerPanel'
import Drawer from '@/v4/world/Drawer'
import TimeScrubber from '@/v4/world/TimeScrubber'
import { mapEngine } from '@/lib/mapEngine'

/** The shared map body (map renderer + reused chrome). `renderer` swaps only the
 *  base map surface; LayerPanel/Drawer/TimeScrubber read the world store either
 *  way. `[&_.leaflet-container]:z-0` only matters for the Leaflet renderer. */
function MapBody({ renderer }: { renderer: ReactNode }) {
  return (
    <div className="flex h-full w-full flex-col bg-surface-300">
      <div className="relative min-h-[320px] flex-1 [&_.leaflet-container]:z-0">
        {renderer}
        <LayerPanel />
        <Drawer />
      </div>
      <TimeScrubber />
    </div>
  )
}

export default function MapPanel() {
  const engine = mapEngine()
  if (engine === 'leaflet') {
    return (
      <PanelBoundary>
        <MapBody renderer={<LeafletWorldMap />} />
      </PanelBoundary>
    )
  }
  // maplibre (default) — rendered through the position-sync overlay so the WebGL
  // canvas escapes the tile transform.
  return (
    <PanelBoundary>
      <TileWebGLOverlay className="bg-surface-300">
        <MapBody renderer={<MapLibreWorldMap />} />
      </TileWebGLOverlay>
    </PanelBoundary>
  )
}
