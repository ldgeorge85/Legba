/**
 * v4 panel — the map (one panel kind, `v4.map`, every surface).
 *
 * WHICH MAP (U-4):
 *  - GLOBE (the flag `lib/globeFlag.ts`, on by default): `v4/globe/GlobeMap`
 *    on maplibre-gl 5 — a globe on the Board flattening on zoom, a flat inset
 *    on Subject, layers built from the platform's objects. Without WebGL2, or
 *    when the context is lost, the same layers are drawn in SVG
 *    (`v4/globe/SvgWorld`).
 *  - FLAG OFF: the earlier flat World map (`v4/world/MapLibreWorldMap`), now
 *    on maplibre-gl 5 in Mercator; without WebGL2 the drawn map stands in.
 *
 * The WebGL canvas renders through the TileWebGLOverlay harness: a
 * `position: fixed` layer portalled to document.body and synced to this
 * tile's rect, so the GPU canvas escapes the Dockview tile transform (the
 * S7-T2 "black tile"). The drawn map composites in the tile and needs none.
 *
 * Leaflet is retired: the drawn map replaced it as the safe renderer.
 */
import { useState, type ReactNode } from 'react'
import { PanelBoundary } from './PanelBoundary'
import { TileWebGLOverlay } from '@/components/TileWebGLOverlay'
import { useTileVisible } from '@/components/useTileVisible'
import MapLibreWorldMap from '@/v4/world/MapLibreWorldMap'
import LayerPanel from '@/v4/world/LayerPanel'
import Drawer from '@/v4/world/Drawer'
import GlobeMap from '@/v4/globe/GlobeMap'
import SvgWorld from '@/v4/globe/SvgWorld'
import SubjectField from '@/v4/globe/SubjectField'
import { hasWebGL2, mapEngine } from '@/lib/mapEngine'
import { globeEnabled } from '@/lib/globeFlag'
import { mapSurfaceDeclared, useSubjectCamera } from '@/lib/subjectGeometry'
import { usePinScope } from '@/lib/consultContext'
import { useActiveSurface } from '@/state/activeSurface'

/**
 * THE PIN HOOK on the map (UI-B6): the subject the camera is fitted to — the
 * renderers' own `useSubjectCamera` — is what the map is showing, so Consult's
 * ambient pin follows it. A subject the map cannot place (no declared
 * geometry, so no camera move) is not being shown, and pins nothing.
 */
export function MapSubjectPin() {
  const { subject, camera } = useSubjectCamera()
  usePinScope(subject && camera ? { kind: 'target', id: subject.id } : null, 'map')
  return null
}

/** The earlier flat map's body (flag off): renderer + its reused chrome. */
function MapBody({ renderer }: { renderer: ReactNode }) {
  return (
    <div className="flex h-full w-full flex-col bg-surface-300">
      <MapSubjectPin />
      <div className="relative min-h-[320px] flex-1">
        {renderer}
        <LayerPanel />
        <Drawer />
      </div>
    </div>
  )
}

function GlobePanel() {
  const surface = useActiveSurface((s) => s.surface)
  const preset = surface === 'subject' ? 'inset' : 'board'
  const visible = useTileVisible()
  const [fallback, setFallback] = useState<string | null>(() =>
    hasWebGL2() ? null : 'this browser has no WebGL2, which the globe needs',
  )
  if (fallback) {
    return (
      <PanelBoundary>
        <MapSubjectPin />
        <SvgWorld preset={preset} active={visible} reason={fallback} />
      </PanelBoundary>
    )
  }
  return (
    <PanelBoundary>
      <MapSubjectPin />
      <TileWebGLOverlay className="bg-surf-base">
        <GlobeMap key={preset} preset={preset} active={visible} onFallback={setFallback} />
      </TileWebGLOverlay>
    </PanelBoundary>
  )
}

export default function MapPanel() {
  // A profile that declares no map geometry: the subject field takes the
  // map's place, with the same marks (layout design §3).
  if (!mapSurfaceDeclared()) {
    return (
      <PanelBoundary>
        <SubjectField />
      </PanelBoundary>
    )
  }
  if (globeEnabled()) return <GlobePanel />
  if (mapEngine() === 'drawn') {
    return (
      <PanelBoundary>
        <MapSubjectPin />
        <SvgWorld preset="board" active reason="the flat map needs WebGL2" />
      </PanelBoundary>
    )
  }
  return (
    <PanelBoundary>
      <TileWebGLOverlay className="bg-surface-300">
        <MapBody renderer={<MapLibreWorldMap />} />
      </TileWebGLOverlay>
    </PanelBoundary>
  )
}
