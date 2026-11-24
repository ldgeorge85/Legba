/**
 * A small gazetteer for series subjects that are PLACES the provider names by
 * its own key (U-4). The observation rows carry no coordinates — a PortWatch
 * row's subject is `chokepoint4` and its provenance names it "Bab el-Mandeb
 * Strait" — so the map places a series subject only where this table names
 * the key. An unplaced subject is listed in the legend as unplaced, never
 * dropped at (0, 0).
 *
 * The positions are the passages' approximate mid-points, to a tenth of a
 * degree; they are geography, not data from any reader's record.
 */
export interface PlacedSeriesSubject {
  key: string
  name: string
  lat: number
  lon: number
}

/** IMF PortWatch's 28 maritime chokepoints, by the provider's `portid`. */
export const PORTWATCH_CHOKEPOINTS: readonly PlacedSeriesSubject[] = [
  { key: 'chokepoint1', name: 'Suez Canal', lat: 30.6, lon: 32.3 },
  { key: 'chokepoint2', name: 'Panama Canal', lat: 9.1, lon: -79.7 },
  { key: 'chokepoint3', name: 'Bosporus Strait', lat: 41.1, lon: 29.1 },
  { key: 'chokepoint4', name: 'Bab el-Mandeb Strait', lat: 12.6, lon: 43.4 },
  { key: 'chokepoint5', name: 'Malacca Strait', lat: 2.5, lon: 101.3 },
  { key: 'chokepoint6', name: 'Strait of Hormuz', lat: 26.6, lon: 56.3 },
  { key: 'chokepoint7', name: 'Cape of Good Hope', lat: -34.6, lon: 18.5 },
  { key: 'chokepoint8', name: 'Gibraltar Strait', lat: 36.0, lon: -5.6 },
  { key: 'chokepoint9', name: 'Dover Strait', lat: 51.0, lon: 1.5 },
  { key: 'chokepoint10', name: 'Oresund Strait', lat: 55.9, lon: 12.7 },
  { key: 'chokepoint11', name: 'Taiwan Strait', lat: 24.2, lon: 119.5 },
  { key: 'chokepoint12', name: 'Korea Strait', lat: 34.6, lon: 129.1 },
  { key: 'chokepoint13', name: 'Tsugaru Strait', lat: 41.5, lon: 140.6 },
  { key: 'chokepoint14', name: 'Luzon Strait', lat: 20.5, lon: 121.0 },
  { key: 'chokepoint15', name: 'Lombok Strait', lat: -8.8, lon: 115.8 },
  { key: 'chokepoint16', name: 'Ombai Strait', lat: -8.4, lon: 125.0 },
  { key: 'chokepoint17', name: 'Bohai Strait', lat: 38.3, lon: 121.0 },
  { key: 'chokepoint18', name: 'Torres Strait', lat: -10.5, lon: 142.3 },
  { key: 'chokepoint19', name: 'Sunda Strait', lat: -6.0, lon: 105.8 },
  { key: 'chokepoint20', name: 'Makassar Strait', lat: -2.5, lon: 118.0 },
  { key: 'chokepoint21', name: 'Magellan Strait', lat: -53.2, lon: -70.6 },
  { key: 'chokepoint22', name: 'Yucatan Channel', lat: 21.6, lon: -85.8 },
  { key: 'chokepoint23', name: 'Windward Passage', lat: 20.0, lon: -73.8 },
  { key: 'chokepoint24', name: 'Mona Passage', lat: 18.2, lon: -67.9 },
  { key: 'chokepoint25', name: 'Balabac Strait', lat: 7.6, lon: 117.0 },
  { key: 'chokepoint26', name: 'Bering Strait', lat: 65.8, lon: -168.9 },
  { key: 'chokepoint27', name: 'Mindoro Strait', lat: 12.3, lon: 120.4 },
  { key: 'chokepoint28', name: 'Kerch Strait', lat: 45.3, lon: 36.6 },
]

const BY_KEY = new Map(PORTWATCH_CHOKEPOINTS.map((p) => [p.key, p]))

export function placeSeriesSubject(key: string): PlacedSeriesSubject | null {
  return BY_KEY.get(key) ?? null
}

/**
 * THE SHARED POINT for a series place (U-7): the selection id of a placed
 * series subject, `portwatch:<key>`, and back. A place is not a record, so
 * the selection kind is `place` and the id names the gazetteer it is in.
 */
export const PLACE_GAZETTEER = 'portwatch'

export function placeId(key: string): string {
  return `${PLACE_GAZETTEER}:${key}`
}

export function placeOfId(id: string): PlacedSeriesSubject | null {
  const i = id.indexOf(':')
  if (i < 0 || id.slice(0, i) !== PLACE_GAZETTEER) return null
  return placeSeriesSubject(id.slice(i + 1))
}

/**
 * The live series the map draws at their place: one row per declared series.
 * A profile adds a row when it adds a live place-keyed source; with none the
 * layer says so instead of drawing.
 */
export interface SeriesLayerSource {
  collectionId: string
  seriesId: string
  /** What one value counts, for the legend. */
  label: string
  unit: string
}

export const SERIES_SOURCES: readonly SeriesLayerSource[] = [
  {
    collectionId: 'live:source.imf.portwatch_chokepoints',
    seriesId: 'imf_portwatch.chokepoint.n_total',
    label: 'daily transits',
    unit: 'vessels / day',
  },
]
