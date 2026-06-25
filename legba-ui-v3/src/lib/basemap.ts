/**
 * Self-hosted basemap resolution (v4) — FROZEN surface (UI_V4_PLAN §2.4, D6).
 *
 * The world basemap (coastlines / land / water / admin borders) is served by
 * OUR Caddy as a single PMTiles archive at `/tiles/basemap.pmtiles`, read by the
 * browser via HTTP range requests — clients never download the whole file, only
 * the byte-ranges of tiles in view. No external tile server, no API key.
 *
 * Data: OpenStreetMap via Protomaps' free reduced-maxzoom build (ODbL — keep the
 * attribution line). Until the operator drops the file in (see RUNBOOK), we fall
 * back to the external demotiles style behind a flag so the map still renders.
 *
 * `resolveBasemapStyle()` HEAD-probes the file and returns a self-hosted dark
 * style if present, else the fallback. The minimal style here is land/water/
 * borders only (no label glyphs → no external font dependency); the World map
 * agent layers labels (self-hosted glyphs) on top in Wave 1.
 */
import type { StyleSpecification } from 'maplibre-gl'

export const PMTILES_PATH = '/tiles/basemap.pmtiles'
export const WORLD_GEOJSON_PATH = '/world.geojson'
/** Last-ditch only — kept for reference; the default fallback is self-hosted. */
export const DEMOTILES_FALLBACK = 'https://demotiles.maplibre.org/style.json'

let pmtilesRegistered = false

/** Register the `pmtiles://` protocol with MapLibre once (idempotent). */
export async function registerPmtiles(): Promise<void> {
  if (pmtilesRegistered) return
  const maplibregl = (await import('maplibre-gl')).default
  const { Protocol } = await import('pmtiles')
  const protocol = new Protocol()
  maplibregl.addProtocol('pmtiles', protocol.tile)
  pmtilesRegistered = true
}

/** Minimal dark Protomaps style — land/water/boundaries, no labels (no glyphs). */
function darkPmtilesStyle(): StyleSpecification {
  const src = `pmtiles://${window.location.origin}${PMTILES_PATH}`
  return {
    version: 8,
    sources: {
      protomaps: {
        type: 'vector',
        url: src,
        attribution: '© OpenStreetMap contributors',
      },
    },
    layers: [
      { id: 'bg', type: 'background', paint: { 'background-color': '#0a0c10' } },
      {
        id: 'earth',
        type: 'fill',
        source: 'protomaps',
        'source-layer': 'earth',
        paint: { 'fill-color': '#15171c' },
      },
      {
        id: 'water',
        type: 'fill',
        source: 'protomaps',
        'source-layer': 'water',
        paint: { 'fill-color': '#0d1b2a' },
      },
      {
        id: 'boundaries',
        type: 'line',
        source: 'protomaps',
        'source-layer': 'boundaries',
        paint: { 'line-color': '#2a2f3a', 'line-width': 0.6 },
      },
    ],
  }
}

/**
 * Fully self-hosted dark base — a bundled simplified world-countries GeoJSON
 * (Natural Earth 110m, served same-origin at /world.geojson) drawn as dark land
 * on a darker void. No external tile server, no key, no big download — always
 * renders. The PMTiles archive, when present, is the higher-detail upgrade.
 */
export function defaultBasemapStyle(): StyleSpecification {
  return {
    version: 8,
    sources: {
      world: { type: 'geojson', data: WORLD_GEOJSON_PATH, attribution: 'Natural Earth' },
    },
    layers: [
      { id: 'bg', type: 'background', paint: { 'background-color': '#0a0c10' } },
      { id: 'land', type: 'fill', source: 'world', paint: { 'fill-color': '#1b2433' } },
      {
        id: 'borders',
        type: 'line',
        source: 'world',
        paint: { 'line-color': '#3a4965', 'line-width': 0.6 },
      },
    ],
  }
}

/** PMTiles is an opt-in upgrade — enable with `localStorage.legba_pmtiles='1'`. */
function pmtilesEnabled(): boolean {
  try {
    return typeof localStorage !== 'undefined' && localStorage.getItem('legba_pmtiles') === '1'
  } catch {
    return false
  }
}

/**
 * Returns a self-hosted dark style: the bundled GeoJSON base by default, or the
 * higher-detail PMTiles archive when it's been dropped in AND opt-in is enabled
 * (so we never 404-probe for an archive that isn't there). Self-hosted either way.
 */
export async function resolveBasemapStyle(): Promise<string | StyleSpecification> {
  if (pmtilesEnabled()) {
    try {
      const head = await fetch(PMTILES_PATH, { method: 'HEAD' })
      const ctype = head.headers.get('content-type') ?? ''
      // A 200 can be the SPA catch-all serving index.html for a missing file —
      // that HTML would crash the PMTiles parser. Require a non-HTML body.
      if (head.ok && !ctype.includes('text/html')) {
        await registerPmtiles()
        return darkPmtilesStyle()
      }
    } catch {
      /* not present / not reachable → fall back */
    }
  }
  return defaultBasemapStyle()
}
