/**
 * The walk's "a place selected" pick (U-8): which pixel of the Board map to
 * click so the click selects a changed area.
 *
 * Roll 80's live walk failed "map: a place selected — no accent outline after
 * the click" because the old pick asked the renderer only: the first pixel,
 * scanning from the top-left, where the `land` layer carried a wash. At 1680
 * the map's own layer card (open by default at that size) sits over the
 * top-left of the map, and Canada was under it — the click landed on the
 * card, nothing was selected, and Consult still read "everything in view".
 * The map was never asked.
 *
 * A pixel is clickable only when what a hand would hit there is the map's
 * canvas (not the layer card, the legend, a control or a series glyph), AND
 * the TOPMOST pickable feature there is a washed `land` area, or a blank's
 * hatch over that same place (a forecast ring or an event dot on top would
 * make the click select that instead — exactly what `GlobeMap.tsx::onClick`
 * does; the hatch it treats as the land).
 *
 * `pickPlace` runs in the browser (`page.evaluate`, by its source), so it is
 * self-contained: no imports, no closures over module state.
 */

/** The map's pickable layers, top first — `GlobeMap.tsx::onClick`'s list. */
export const PICKABLE = ['forecast-rings', 'series-dots', 'incidents', 'point-dots', 'point-clusters', 'hatch-dense', 'hatch-sparse', 'land']

/**
 * @param {Document} doc
 * @param {string[]} pickable
 * @returns {{ x: number, y: number, iso: string } | null}
 */
export function pickPlace(doc, pickable) {
  const c = doc.querySelector('[data-testid="globe-map"] .maplibregl-map')
  const mm = c && c.__legbaMap
  if (!mm) return null
  const canvas = c.querySelector('canvas.maplibregl-canvas') || c.querySelector('canvas')
  const r = c.getBoundingClientRect()
  const layers = pickable.filter((l) => mm.getLayer(l) && mm.getLayoutProperty(l, 'visibility') !== 'none')
  const isoOf = (p) => p.ISO_A2_EH || p.ISO_A2 || p.iso || ''
  const HATCH = new Set(['hatch-dense', 'hatch-sparse'])
  for (let y = 40; y < r.height - 40; y += 16) {
    for (let x = 40; x < r.width - 40; x += 16) {
      const all = mm.queryRenderedFeatures([x, y], { layers })
      const top = all[0]
      if (!top) continue
      const land = top.layer.id === 'land' ? top : all.find((f) => f.layer.id === 'land')
      if (!land || !(land.state && land.state.wash > 0)) continue
      const iso = isoOf(land.properties || {})
      // Roll 86: a blank's hatch is drawn over its land and `onClick` treats
      // it as the land (the default branch selects the place's subject by
      // `iso`), so a hatched changed area is as clickable as a bare one. Any
      // other feature on top (a ring, a series dot) selects that instead.
      if (top !== land && !(HATCH.has(top.layer.id) && isoOf(top.properties || {}) === iso)) continue
      // what a hand clicking here would hit: the map's canvas, nothing over it
      const hit = doc.elementFromPoint(r.left + x, r.top + y)
      if (!hit || hit !== canvas) continue
      return { x: r.left + x, y: r.top + y, iso }
    }
  }
  return null
}
