/**
 * World-map renderer selection (S7-T5; U-4 retired Leaflet).
 *
 * Two renderers ship:
 *   - 'drawn'    — the SVG map (`v4/globe/SvgWorld`), always composites in a
 *                  Dockview tile. FALLBACK — served only when the browser
 *                  cannot create a WebGL2 context (see DEFAULT note below).
 *                  It replaced Leaflet (U-4).
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
 * to the drawn map on any browser that cannot create a WebGL2 context, so a
 * black default can never ship. Overrides still win, with NO rebuild —
 *
 *   ?map=drawn                                    // URL override (wins, either way)
 *   localStorage.legba_map_engine = 'drawn'       // or 'maplibre'
 *
 * (`leaflet`, the retired engine's name, still reads as `drawn`.) This choice
 * applies with the globe flag OFF; with it on, `lib/globeFlag.ts` decides.
 */
export type MapEngine = 'maplibre' | 'drawn'

/**
 * True if the browser can create a WebGL2 context. maplibre-gl 5 dropped
 * WebGL1 (U-4: the upgrade's one hard break), so a WebGL1-only browser is
 * routed to the drawn fallback rather than handed an engine that throws.
 */
export function hasWebGL2(): boolean {
  try {
    const c = document.createElement('canvas')
    return !!c.getContext('webgl2')
  } catch {
    return false
  }
}

export function mapEngine(): MapEngine {
  try {
    // An explicit override is honored unconditionally (debugging / preference).
    const url = new URLSearchParams(window.location.search).get('map')
    if (url === 'maplibre') return url
    if (url === 'drawn' || url === 'leaflet') return 'drawn'
    const ls = localStorage.getItem('legba_map_engine')
    if (ls === 'maplibre') return ls
    if (ls === 'drawn' || ls === 'leaflet') return 'drawn'
    // Default to the richer maplibre choropleth, but never hand back a renderer
    // the browser can't composite — fall back to the always-safe drawn map.
    return hasWebGL2() ? 'maplibre' : 'drawn'
  } catch {
    /* SSR / private mode → the safe SVG renderer */
    return 'drawn'
  }
}
