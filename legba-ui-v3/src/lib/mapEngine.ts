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
 * DEFAULT = 'maplibre' (S7-T5 integration, 2026-07-02): the overlay's "not
 * black" rendering was confirmed via the containerized render harness — the same
 * harness the S7-T2 spike used to call maplibre black in-tile now shows the
 * banded-verdict choropleth painting correctly, because the TileWebGLOverlay
 * portals the GPU canvas to <body>, OUT of every Dockview CSS transform (an
 * architectural fix, GPU-agnostic). A WebGL-availability guard below downgrades
 * to Leaflet on any browser that genuinely cannot create a WebGL context, so a
 * black default can never ship. Overrides still win, with NO rebuild —
 *
 *   ?map=leaflet                                  // URL override (wins, either way)
 *   localStorage.legba_map_engine = 'leaflet'     // or 'maplibre'
 *
 * Leaflet + its `leaflet` deps stay regardless — the ReadGeoLens mini-map and
 * the fallback both use them.
 */
export type MapEngine = 'maplibre' | 'leaflet'

/** True if the browser can create a WebGL context (maplibre needs one). */
function hasWebGL(): boolean {
  try {
    const c = document.createElement('canvas')
    return !!(c.getContext('webgl2') || c.getContext('webgl') || c.getContext('experimental-webgl'))
  } catch {
    return false
  }
}

export function mapEngine(): MapEngine {
  try {
    // An explicit override is honored unconditionally (debugging / preference).
    const url = new URLSearchParams(window.location.search).get('map')
    if (url === 'leaflet' || url === 'maplibre') return url
    const ls = localStorage.getItem('legba_map_engine')
    if (ls === 'leaflet' || ls === 'maplibre') return ls
    // Default to the richer maplibre choropleth, but never hand back a renderer
    // the browser can't composite — fall back to the always-safe Leaflet.
    return hasWebGL() ? 'maplibre' : 'leaflet'
  } catch {
    /* SSR / private mode → the safe DOM/SVG renderer */
    return 'leaflet'
  }
}
