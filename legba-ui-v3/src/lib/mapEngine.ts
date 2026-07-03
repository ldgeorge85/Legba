/**
 * World-map renderer selection (S7-T5).
 *
 * Two renderers ship, both fully wired to the shared world store:
 *   - 'leaflet'  — DOM/SVG, always composites in a Dockview tile (the current,
 *                  known-good map). DEFAULT.
 *   - 'maplibre' — WebGL, rendered through the TileWebGLOverlay harness that
 *                  routes the GPU canvas OUT of the tile transform (the S7-T5
 *                  unlock), adding the banded-verdict choropleth + signal-density
 *                  heatmap.
 *
 * WHY LEAFLET IS THE DEFAULT: the overlay's "not black" rendering can only be
 * confirmed with an in-browser screenshot, which the build environment cannot
 * produce — and a black default map must never ship (hard rule). So maplibre is
 * OPT-IN pending that visual check; flip it with NO rebuild —
 *
 *   ?map=maplibre                                 // URL override (wins)
 *   localStorage.legba_map_engine = 'maplibre'    // or 'leaflet'
 *
 * INTEGRATOR: once you confirm `?map=maplibre` renders (not black) and behaves,
 * change the default below to 'maplibre'. Leaflet + its `leaflet` deps stay
 * regardless — the ReadGeoLens mini-map and this fallback both use them.
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
  return 'leaflet'
}
