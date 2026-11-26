/**
 * Are the flat World map's signal points drawn at the signal's own
 * point, not at a country centroid?
 *
 * The map places a signal at `data.geo.{lat, lon}` and falls back to its first
 * country's centroid only when the row carries no point. Before 0b8b0484 the
 * light `/signals` row dropped `data.geo`, so every point silently fell back,
 * and a check of only the API rows missed it. This reads what the map DREW.
 *
 * `signalPlacement` runs in the browser (`page.evaluate`, by its source), so
 * it is self-contained: no imports, no closures over module state.
 *   rows — the `/signals` rows the walk served the page (data.geo + clocks)
 *   layer — the flat map's signal layer id
 *   windowMs — the map's default window (rows older than it are not drawn)
 * Returns the expected points (rows with a point, in the window, in view, one
 * per 0.01° cell — the map's own cluster key) and which of them have a drawn
 * feature within `tol` degrees.
 */
export function signalPlacement(doc, rows, layer, windowMs, tol) {
  const c = doc.querySelector('[data-testid="maplibre-world"] .maplibregl-map')
  const mm = c && c.__legbaMap
  if (!mm || !mm.getLayer(layer)) return null
  const now = Date.now()
  const bounds = mm.getBounds()
  const want = new Map()
  for (const r of rows) {
    const g = r && r.data && r.data.geo
    if (!g || typeof g.lat !== 'number' || typeof g.lon !== 'number') continue
    const t = Date.parse((r.data && r.data.published_at) || '')
    if (!Number.isFinite(t) || t < now - windowMs || t > now) continue
    if (!bounds.contains([g.lon, g.lat])) continue
    want.set(`${g.lat.toFixed(2)},${g.lon.toFixed(2)}`, { id: r.id, lat: g.lat, lon: g.lon })
  }
  const drawn = mm.queryRenderedFeatures({ layers: [layer] }).map((f) => f.geometry.coordinates)
  const missing = []
  for (const w of want.values()) {
    if (!drawn.some(([lon, lat]) => Math.abs(lon - w.lon) <= tol && Math.abs(lat - w.lat) <= tol)) missing.push(w)
  }
  return { expected: want.size, drawn: drawn.length, missing: missing.slice(0, 5), missingCount: missing.length, sample: [...want.values()][0] ?? null }
}
