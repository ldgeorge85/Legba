/**
 * World-map renderer selection (S7-T5).
 *
 * The World map ships on the maplibre-gl WebGL renderer, routed out of the
 * Dockview tile transform by TileWebGLOverlay. Leaflet (DOM/SVG) is retained as
 * an instant, always-composites fallback: if an integrator finds the WebGL
 * overlay black in their browser (the transform-compositing risk the S7-T2 spike
 * flagged), flip the engine with NO rebuild —
 *
 *   localStorage.legba_map_engine = 'leaflet'   // or 'maplibre'
 *   ?map=leaflet                                 // URL override (wins)
 *
 * Default is 'maplibre'. This is why the Leaflet map + its `leaflet` deps are
 * NOT deleted in this task: the fallback (and the ReadGeoLens mini-map) still
 * use them.
 */
export type MapEngine = 'maplibre' | 'leaflet'

export function mapEngine(): MapEngine {
  try {
    const url = new URLSearchParams(window.location.search).get('map')
    if (url === 'leaflet' || url === 'maplibre') return url
    const ls = localStorage.getItem('legba_map_engine')
    if (ls === 'leaflet' || ls === 'maplibre') return ls
  } catch {
    /* SSR / private mode → default */
  }
  return 'maplibre'
}
