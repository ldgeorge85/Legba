/**
 * The globe's hand-written dark style (U-4; layout design §1 colour tokens,
 * §3 "Projection and basemap").
 *
 * MapLibre paints from literals, not CSS variables, so the dark tokens are
 * restated here once (`T`) — the same values as `globals.css` `:root`.
 *
 * LABELS AND GLYPHS. The style declares NO `glyphs` URL. MapLibre 5 then
 * draws every glyph on the client with TinySDF, in the CSS font the layer's
 * `text-font` names — here the self-hosted IBM Plex Sans / Plex Mono the shell
 * already bundles (`src/assets/fonts`, loaded through `@font-face`). So the
 * glyph set is built in the browser from the bundled woff2 files at first use
 * (only the characters the labels contain), with no glyph files to ship and
 * no glyph or tile host at runtime. The map waits for the two faces to load
 * before it is created (`ensureMapFonts`), so a label is never rasterised in a
 * fallback face.
 *
 * Basemap: the bundled Natural Earth 110 m outline (`/world.geojson`, served
 * same-origin, read by MapLibre's own worker): land in `--land` on a `--sea`
 * sphere, one hairline outline in `--muted` at 40 %, a 15° graticule at 6 %
 * ink, country names in 12 px Plex Sans capitals in `--muted`, ocean names
 * wider-spaced and fainter. No imagery, no terrain, no roads.
 */
import type { StyleSpecification } from 'maplibre-gl'
import { WORLD_GEOJSON_PATH } from '@/lib/basemap'
import { RING_STEPS } from './prep'

/** The dark tokens, as literals (globals.css `:root`; layout design §1). */
export const T = {
  ground: '#11161A',
  sea: '#0B1013',
  surface: '#171D22',
  land: '#1D252B',
  ink: '#E6E2D8',
  muted: '#8F9AA2',
  rule: '#2A343B',
  accent: '#EC6FA9',
  good: '#5CBF8E',
  warn: '#E2A43C',
  critical: '#F2694D',
  wash: ['#2C3A45', '#3D5160', '#587286'] as const,
  sev: { critical: '#F2694D', high: '#E8843F', medium: '#E2A43C' } as Record<string, string>,
}

export const FONT_SANS = 'IBM Plex Sans'
export const FONT_MONO = 'IBM Plex Mono'

/** Load the two faces the labels use before the first glyph is drawn. */
export async function ensureMapFonts(): Promise<void> {
  try {
    if (typeof document === 'undefined' || !document.fonts) return
    await Promise.race([
      Promise.all([document.fonts.load(`400 24px "${FONT_SANS}"`), document.fonts.load(`500 24px "${FONT_MONO}"`)]),
      new Promise((r) => setTimeout(r, 2500)),
    ])
  } catch {
    // a refused font load leaves the browser's own face: labels still draw
  }
}

/** A 15° graticule (meridians to ±75°, parallels −60…75°). */
export function graticule(): GeoJSON.FeatureCollection<GeoJSON.LineString> {
  const features: GeoJSON.Feature<GeoJSON.LineString>[] = []
  for (let lon = -180; lon < 180; lon += 15) {
    const coords: [number, number][] = []
    for (let lat = -75; lat <= 75; lat += 5) coords.push([lon, lat])
    features.push({ type: 'Feature', geometry: { type: 'LineString', coordinates: coords }, properties: {} })
  }
  for (let lat = -60; lat <= 75; lat += 15) {
    const coords: [number, number][] = []
    for (let lon = -180; lon <= 180; lon += 5) coords.push([lon, lat])
    features.push({ type: 'Feature', geometry: { type: 'LineString', coordinates: coords }, properties: {} })
  }
  return { type: 'FeatureCollection', features }
}

/** Ocean and sea names (geography; drawn fainter and wider than countries). */
export const OCEAN_LABELS: ReadonlyArray<[string, number, number, number]> = [
  ['PACIFIC OCEAN', -150, 5, 0],
  ['PACIFIC OCEAN', 165, 25, 1],
  ['ATLANTIC OCEAN', -38, 28, 0],
  ['ATLANTIC OCEAN', -18, -20, 1],
  ['INDIAN OCEAN', 78, -20, 0],
  ['SOUTHERN OCEAN', 40, -58, 1],
  ['ARCTIC OCEAN', 0, 82, 1],
  ['ARABIAN SEA', 63, 15, 2],
  ['RED SEA', 38.5, 20.5, 3],
  ['MEDITERRANEAN SEA', 18, 34.5, 2],
  ['SOUTH CHINA SEA', 114, 13, 2],
  ['CARIBBEAN SEA', -75, 15, 2],
  ['BLACK SEA', 34.5, 43.3, 3],
  ['GULF OF GUINEA', 3, 1.5, 3],
  ['BAY OF BENGAL', 89, 14, 2],
]

function oceanLabels(): GeoJSON.FeatureCollection<GeoJSON.Point> {
  return {
    type: 'FeatureCollection',
    features: OCEAN_LABELS.map(([name, lon, lat, min]) => ({
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [lon, lat] },
      properties: { name, min },
    })),
  }
}

/** The projection: a globe at world zoom that becomes Mercator by zoom 5, or flat. */
export function projectionSpec(mode: 'globe' | 'flat'): StyleSpecification['projection'] {
  if (mode === 'flat') return { type: 'mercator' }
  return { type: ['interpolate', ['linear'], ['zoom'], 3.2, 'vertical-perspective', 5, 'mercator'] } as unknown as StyleSpecification['projection']
}

export function globeStyle(mode: 'globe' | 'flat'): StyleSpecification {
  return {
    version: 8,
    projection: projectionSpec(mode),
    // No atmosphere glow; the sphere is told from the page by its sea tone.
    sky: { 'atmosphere-blend': 0 },
    sources: {
      // `promoteId`: the change wash and the point's dimming are feature
      // states on the land layer, keyed by Natural Earth's unique ADM0_A3.
      world: { type: 'geojson', data: WORLD_GEOJSON_PATH, attribution: 'Natural Earth', promoteId: 'ADM0_A3' },
      graticule: { type: 'geojson', data: graticule() },
      oceans: { type: 'geojson', data: oceanLabels() },
    },
    layers: [
      { id: 'sea', type: 'background', paint: { 'background-color': T.sea } },
      { id: 'graticule', type: 'line', source: 'graticule', paint: { 'line-color': 'rgba(230,226,216,0.06)', 'line-width': 0.6 } },
      // The coast and borders are the land fill's own 1 px outline: one layer
      // over the sphere rather than two (each globe layer costs a full pass).
      {
        id: 'land',
        type: 'fill',
        source: 'world',
        paint: {
          // the three-step ink wash by changes (a feature state), dimmed to the
          // plain land colour when another place is the point
          'fill-color': [
            'case',
            ['boolean', ['feature-state', 'dim'], false],
            T.land,
            ['match', ['coalesce', ['feature-state', 'wash'], 0], 1, T.wash[0], 2, T.wash[1], 3, T.wash[2], T.land],
          ],
          // a changed area's outline is ink; every other coast a 40 % hairline
          'fill-outline-color': ['case', ['>=', ['coalesce', ['feature-state', 'wash'], 0], 1], 'rgba(230,226,216,0.55)', 'rgba(143,154,162,0.45)'],
        },
      },
    ],
  }
}

/**
 * Raster marks the symbol layers use, drawn once into ImageData:
 *   - `hatch-dense` / `hatch-sparse`: 45° hatch, 1 px `--muted`, 6 px and 12 px pitch;
 *   - `ring-<n>` / `ring-<n>-h`: an open forecast's ring with n/12 of its arc filled
 *     (`-h`: due within 7 days, the heavier stroke);
 *   - `incident`: a small open diamond.
 */
export function markImages(dpr = 2): Array<{ id: string; data: ImageData; pixelRatio: number }> {
  const out: Array<{ id: string; data: ImageData; pixelRatio: number }> = []
  const canvas = (w: number, h: number) => {
    const c = document.createElement('canvas')
    c.width = w * dpr
    c.height = h * dpr
    const ctx = c.getContext('2d')
    if (!ctx) return null
    ctx.scale(dpr, dpr)
    return { c, ctx }
  }
  for (const [id, pitch, alpha] of [['hatch-dense', 6, 0.55], ['hatch-sparse', 12, 0.35]] as const) {
    const k = canvas(pitch, pitch)
    if (!k) continue
    k.ctx.strokeStyle = `rgba(143,154,162,${alpha})`
    k.ctx.lineWidth = 1
    k.ctx.beginPath()
    // three diagonals so the tile seams join into continuous 45° lines
    for (const o of [-pitch, 0, pitch]) {
      k.ctx.moveTo(o, pitch)
      k.ctx.lineTo(o + pitch, 0)
    }
    k.ctx.stroke()
    out.push({ id, data: k.ctx.getImageData(0, 0, pitch * dpr, pitch * dpr), pixelRatio: dpr })
  }
  const S = 26
  for (let step = 0; step <= RING_STEPS; step++) {
    for (const heavy of [false, true]) {
      const k = canvas(S, S)
      if (!k) continue
      const cx = S / 2
      const r = S / 2 - 3
      k.ctx.lineCap = 'butt'
      // the whole ring, faint
      k.ctx.strokeStyle = 'rgba(230,226,216,0.35)'
      k.ctx.lineWidth = heavy ? 2 : 1.25
      k.ctx.beginPath()
      k.ctx.arc(cx, cx, r, 0, Math.PI * 2)
      k.ctx.stroke()
      // the elapsed share, in ink, clockwise from twelve o'clock
      if (step > 0) {
        k.ctx.strokeStyle = T.ink
        k.ctx.lineWidth = heavy ? 3 : 2
        k.ctx.beginPath()
        k.ctx.arc(cx, cx, r, -Math.PI / 2, -Math.PI / 2 + (Math.PI * 2 * step) / RING_STEPS)
        k.ctx.stroke()
      }
      // a dot at the centre marks the place
      k.ctx.fillStyle = T.ink
      k.ctx.beginPath()
      k.ctx.arc(cx, cx, 1.5, 0, Math.PI * 2)
      k.ctx.fill()
      out.push({ id: `ring-${step}${heavy ? '-h' : ''}`, data: k.ctx.getImageData(0, 0, S * dpr, S * dpr), pixelRatio: dpr })
    }
  }
  {
    const D = 14
    const k = canvas(D, D)
    if (k) {
      k.ctx.strokeStyle = T.warn
      k.ctx.fillStyle = 'rgba(17,22,26,0.85)'
      k.ctx.lineWidth = 1.5
      k.ctx.beginPath()
      k.ctx.moveTo(D / 2, 2)
      k.ctx.lineTo(D - 2, D / 2)
      k.ctx.lineTo(D / 2, D - 2)
      k.ctx.lineTo(2, D / 2)
      k.ctx.closePath()
      k.ctx.fill()
      k.ctx.stroke()
      out.push({ id: 'incident', data: k.ctx.getImageData(0, 0, D * dpr, D * dpr), pixelRatio: dpr })
    }
  }
  return out
}

/** The label layers (added after the fonts load, above the marks' fills). */
export function labelLayers(): import('maplibre-gl').LayerSpecification[] {
  return [
    {
      id: 'ocean-labels',
      type: 'symbol',
      source: 'oceans',
      filter: ['<=', ['get', 'min'], ['+', ['zoom'], 0.6]],
      layout: {
        'text-field': ['get', 'name'],
        'text-font': [FONT_SANS],
        'text-size': 12,
        'text-letter-spacing': 0.3,
        'text-max-width': 8,
      },
      paint: { 'text-color': 'rgba(143,154,162,0.55)' },
    },
    {
      id: 'country-labels',
      type: 'symbol',
      source: 'g-labels',
      filter: ['<=', ['get', 'min'], ['+', ['zoom'], 1.9]],
      layout: {
        'text-field': ['get', 'name'],
        'text-font': [FONT_SANS],
        'text-size': 12,
        'text-letter-spacing': 0.06,
        'text-max-width': 7,
        'text-padding': 4,
      },
      paint: { 'text-color': T.muted, 'text-halo-color': 'rgba(17,22,26,0.8)', 'text-halo-width': 1 },
    },
  ]
}
