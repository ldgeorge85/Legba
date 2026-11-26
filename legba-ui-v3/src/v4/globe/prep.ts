/**
 * The globe's data preparation (U-4) — PURE, so it runs in the prep worker
 * (`prep.worker.ts`) and, where no worker exists (a unit test), on the
 * caller's thread with the same result.
 *
 * In: the platform's own rows as the routes serve them (already projected to
 * small shapes by `data.ts`), the shared window, and the world outline. Out:
 * one GeoJSON collection per drawn layer, each CAPPED (`GLOBE_CAPS`) with the
 * count it was cut from, the per-country hover facts, each subject's centre
 * (for the camera and the selection ring), and the home view.
 *
 * Nothing here invents a place: a row is drawn at its own coordinates, at its
 * subject's DECLARED geometry, or at a gazetteer position for a provider key
 * — and a row with none of the three is counted as unplaced, never drawn.
 */
import { GLOBE_CAPS } from './catalog'
import { placeSeriesSubject } from './chokepoints'

type Pos = [number, number]

export interface PrepSubject {
  id: string
  name: string
  /** Declared ISO-3166 alpha-2 codes (`scope.geo`). */
  geo: string[]
  /**
   * The descriptor declares at least one source; null when the roster row
   * does not say (a registry older than the summary's `declared_sources`).
   * Only a known false hatches a place "not covered".
   */
  hasSources: boolean | null
}

export interface PrepChange {
  id: string
  targetId: string | null
  atMs: number
}

export interface PrepRead {
  targetId: string
  atMs: number
}

export interface PrepPoint {
  id: string
  kind: 'event' | 'situation'
  lat: number
  lon: number
  ts: number
  title: string
  severity: string | null
  lifecycle: string
}

export interface PrepIncident {
  id: string
  lat: number
  lon: number
  /** Valid time (the publisher's instant), NaN when none. */
  ts: number
  /** Record time (fetched). */
  recordedMs: number
  title: string
  place: string | null
  coasts: string[]
  sourceId: string | null
}

export interface PrepForecast {
  id: string
  question: string
  startMs: number
  dueMs: number
  subjectKeys: string[]
}

export interface PrepSeries {
  subject: string
  name: string | null
  latest: number
  pct: number | null
  revised: boolean
  day: string
  values: number[]
}

export interface PrepInput {
  window: { startMs: number; endMs: number }
  nowMs: number
  subjects: PrepSubject[]
  changes: PrepChange[] | null
  reads: PrepRead[] | null
  points: PrepPoint[] | null
  incidents: PrepIncident[] | null
  forecasts: PrepForecast[] | null
  series: PrepSeries[] | null
  /** The inset's subject: every layer is cut to its area. Null on the Board. */
  scopeSubject: string | null
}

export interface WorldFeature {
  type: 'Feature'
  geometry: { type: 'Polygon' | 'MultiPolygon'; coordinates: unknown }
  /** `a3`: Natural Earth's ADM0_A3, unique per feature — the map's feature-state id. */
  properties: { iso: string; a3: string; name: string; labelX: number | null; labelY: number | null; minLabel: number }
}

export interface World {
  type: 'FeatureCollection'
  features: WorldFeature[]
}

type FC = { type: 'FeatureCollection'; features: Array<{ type: 'Feature'; geometry: unknown; properties: Record<string, unknown>; id?: string | number }> }

export interface CountryFacts {
  iso: string
  name: string
  subjects: Array<{ id: string; name: string; changes: number }>
  changes: number
  blank: 'dense' | 'sparse' | null
}

export type SeriesStatus = 'good' | 'warn' | 'critical' | 'none'

export interface SeriesGlyph {
  key: string
  name: string
  lon: number
  lat: number
  latest: number
  pct: number | null
  status: SeriesStatus
  revised: boolean
  day: string
  values: number[]
}

export interface PrepOutput {
  wash: FC
  hatch: FC
  points: FC
  incidents: FC
  forecasts: FC
  seriesDots: FC
  seriesGlyphs: SeriesGlyph[]
  labels: FC
  countries: Record<string, CountryFacts>
  subjectCenters: Record<string, { center: Pos; bounds: [number, number, number, number] | null; isos: string[] }>
  home: { center: Pos; zoom: number }
  counts: {
    subjectsPlaced: number
    changed: number
    changes: number | null
    blanksDense: number
    blanksSparse: number
    pointsTotal: number | null
    pointsDrawn: number
    incidentsTotal: number | null
    incidentsDrawn: number
    forecastsDrawn: number
    forecastsUnplaced: number
    forecastsTotal: number | null
    seriesPlaced: number
    seriesUnplaced: string[]
    seriesTotal: number | null
  }
}

export const RING_STEPS = 12
const DAY = 86_400_000

export function washStep(n: number): 0 | 1 | 2 | 3 {
  if (n <= 0) return 0
  if (n <= 2) return 1
  if (n <= 5) return 2
  return 3
}

/** At or above this share of its baseline a place reads as a multiple, not a percentage. */
export const SERIES_MULTIPLE_FROM = 1000

/**
 * A series place's value as it is printed: a percentage of its 30-day
 * baseline; "≥10×" at ten times the baseline or more, where a percentage
 * looks like a measurement but only says the baseline was small; and "no
 * baseline" when the route gave no percentage (no earlier days, or too few
 * of them above zero to divide by). Never a dash, which reads as a value.
 */
export function seriesPctText(pct: number | null | undefined): string {
  if (pct == null || !Number.isFinite(pct) || pct < 0) return 'no baseline'
  if (pct >= SERIES_MULTIPLE_FROM) return '≥10×'
  return `${Math.round(pct)} %`
}

/** The same, as a sentence for a hover card or an aria label. */
export function seriesPctSentence(pct: number | null | undefined): string {
  const t = seriesPctText(pct)
  if (t === 'no baseline') return 'No usable 30-day baseline'
  if (t === '≥10×') return 'Ten times its 30-day baseline or more'
  return `${t} of its 30-day baseline`
}

export function seriesStatus(pct: number | null): SeriesStatus {
  if (pct == null || !Number.isFinite(pct)) return 'none'
  if (pct < 50) return 'critical'
  if (pct < 75) return 'warn'
  if (pct >= 85 && pct <= 115) return 'good'
  return 'none'
}

/** Time elapsed from start to due, as one of RING_STEPS + 1 arc steps. */
export function ringStep(startMs: number, dueMs: number, nowMs: number): number {
  if (!Number.isFinite(startMs) || !Number.isFinite(dueMs) || dueMs <= startMs) return RING_STEPS
  const share = (nowMs - startMs) / (dueMs - startMs)
  return Math.max(0, Math.min(RING_STEPS, Math.round(share * RING_STEPS)))
}

function inWindow(ts: number, w: { startMs: number; endMs: number }): boolean {
  return Number.isFinite(ts) && ts >= w.startMs && ts <= w.endMs
}

/** Bounding box of a polygon/multipolygon's outer rings. */
function bboxOf(g: WorldFeature['geometry']): [number, number, number, number] | null {
  let w = Infinity
  let s = Infinity
  let e = -Infinity
  let n = -Infinity
  const polys = (g.type === 'Polygon' ? [g.coordinates] : g.coordinates) as number[][][][]
  for (const poly of polys ?? []) {
    for (const p of poly?.[0] ?? []) {
      if (p[0] < w) w = p[0]
      if (p[0] > e) e = p[0]
      if (p[1] < s) s = p[1]
      if (p[1] > n) n = p[1]
    }
  }
  return Number.isFinite(w) ? [w, s, e, n] : null
}

function unionBox(a: [number, number, number, number] | null, b: [number, number, number, number] | null) {
  if (!a) return b
  if (!b) return a
  return [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[2], b[2]), Math.max(a[3], b[3])] as [number, number, number, number]
}

function inBox(lon: number, lat: number, b: [number, number, number, number], pad: number): boolean {
  return lon >= b[0] - pad && lon <= b[2] + pad && lat >= b[1] - pad && lat <= b[3] + pad
}

/** Natural Earth marks a few states `-99` in ISO_A2; ISO_A2_EH carries them. */
export function slimWorld(raw: unknown): World {
  const out: WorldFeature[] = []
  const feats = (raw as { features?: unknown[] } | null)?.features ?? []
  for (const f of feats) {
    const ff = f as { geometry?: WorldFeature['geometry']; properties?: Record<string, unknown> }
    const p = ff.properties ?? {}
    const iso = [p.ISO_A2, p.ISO_A2_EH].find((v) => typeof v === 'string' && /^[A-Z]{2}$/.test(v)) as string | undefined
    if (!ff.geometry || (ff.geometry.type !== 'Polygon' && ff.geometry.type !== 'MultiPolygon')) continue
    out.push({
      type: 'Feature',
      geometry: ff.geometry,
      properties: {
        iso: iso ?? '',
        a3: typeof p.ADM0_A3 === 'string' ? p.ADM0_A3 : '',
        name: typeof p.NAME === 'string' ? p.NAME : typeof p.ADMIN === 'string' ? p.ADMIN : '',
        labelX: typeof p.LABEL_X === 'number' ? p.LABEL_X : null,
        labelY: typeof p.LABEL_Y === 'number' ? p.LABEL_Y : null,
        minLabel: typeof p.MIN_LABEL === 'number' ? p.MIN_LABEL : 5,
      },
    })
  }
  return { type: 'FeatureCollection', features: out }
}

const point = (lon: number, lat: number, properties: Record<string, unknown>) => ({
  type: 'Feature' as const,
  geometry: { type: 'Point', coordinates: [lon, lat] },
  properties,
})

/** Mean of positions on a sphere's longitude circle (so ±180 does not average to 0). */
function meanCenter(ps: Pos[]): Pos | null {
  if (ps.length === 0) return null
  let x = 0
  let y = 0
  let lat = 0
  for (const [lo, la] of ps) {
    x += Math.cos((lo * Math.PI) / 180)
    y += Math.sin((lo * Math.PI) / 180)
    lat += la
  }
  return [(Math.atan2(y, x) * 180) / Math.PI, lat / ps.length]
}

/** The drawn fallback's projection: plate carrée over lat −60…84, x 0…w, y 0…h. */
export const SVG_LAT_TOP = 84
export const SVG_LAT_BOTTOM = -60

export function svgProject(lon: number, lat: number, w: number, h: number): Pos {
  return [((lon + 180) / 360) * w, ((SVG_LAT_TOP - lat) / (SVG_LAT_TOP - SVG_LAT_BOTTOM)) * h]
}

/** Each country's outline as an SVG path in {@link svgProject} (for the fallback renderer). */
export function svgCountryPaths(world: World, w: number, h: number): Array<{ iso: string; name: string; d: string }> {
  const out: Array<{ iso: string; name: string; d: string }> = []
  for (const f of world.features) {
    const polys = (f.geometry.type === 'Polygon' ? [f.geometry.coordinates] : f.geometry.coordinates) as number[][][][]
    let d = ''
    for (const poly of polys) {
      for (const ring of poly) {
        ring.forEach((p, i) => {
          const [x, y] = svgProject(p[0], Math.max(SVG_LAT_BOTTOM - 2, Math.min(SVG_LAT_TOP + 2, p[1])), w, h)
          d += `${i === 0 ? 'M' : 'L'}${x.toFixed(1)} ${y.toFixed(1)}`
        })
        d += 'Z'
      }
    }
    out.push({ iso: f.properties.iso, name: f.properties.name, d })
  }
  return out
}

export function prepare(input: PrepInput, world: World): PrepOutput {
  const byIso = new Map<string, WorldFeature>()
  for (const f of world.features) if (f.properties.iso) byIso.set(f.properties.iso, f)

  // ── subjects: where each is, and its centre ─────────────────────────────
  const subjectCenters: PrepOutput['subjectCenters'] = {}
  const subjectsByIso = new Map<string, PrepSubject[]>()
  for (const s of input.subjects.slice(0, GLOBE_CAPS.subjects)) {
    const isos = [...new Set(s.geo.map((g) => g.toUpperCase()))].filter((g) => byIso.has(g))
    if (isos.length === 0) continue
    let box: [number, number, number, number] | null = null
    for (const iso of isos) box = unionBox(box, bboxOf(byIso.get(iso)!.geometry))
    const one = isos.length === 1 ? byIso.get(isos[0])!.properties : null
    const center: Pos | null =
      one && one.labelX != null && one.labelY != null
        ? [one.labelX, one.labelY]
        : box
          ? [(box[0] + box[2]) / 2, (box[1] + box[3]) / 2]
          : null
    if (!center) continue
    subjectCenters[s.id] = { center, bounds: box, isos }
    for (const iso of isos) {
      const list = subjectsByIso.get(iso) ?? []
      list.push(s)
      subjectsByIso.set(iso, list)
    }
  }

  const scope = input.scopeSubject ? subjectCenters[input.scopeSubject] ?? null : null
  const scopeIsos = scope ? new Set(scope.isos) : null
  const scopeBox = scope?.bounds ?? null
  const inScope = (lon: number, lat: number, pad: number) => !scopeBox || inBox(lon, lat, scopeBox, pad)

  // ── change since the last visit, cut to the window ───────────────────────
  const changesBySubject = new Map<string, number>()
  let changesInWindow = 0
  for (const c of input.changes ?? []) {
    if (!c.targetId || !inWindow(c.atMs, input.window)) continue
    changesInWindow++
    changesBySubject.set(c.targetId, (changesBySubject.get(c.targetId) ?? 0) + 1)
  }
  const readSubjects = new Set<string>()
  for (const r of input.reads ?? []) if (inWindow(r.atMs, input.window)) readSubjects.add(r.targetId)

  const countries: Record<string, CountryFacts> = {}
  const wash: FC = { type: 'FeatureCollection', features: [] }
  const hatch: FC = { type: 'FeatureCollection', features: [] }
  let dense = 0
  let sparse = 0
  let changed = 0
  for (const [iso, subs] of subjectsByIso) {
    if (scopeIsos && !scopeIsos.has(iso)) continue
    const f = byIso.get(iso)!
    const list = subs.map((s) => ({ id: s.id, name: s.name, changes: changesBySubject.get(s.id) ?? 0 }))
    const n = input.changes ? Math.max(0, ...list.map((s) => s.changes)) : 0
    let blank: CountryFacts['blank'] = null
    // "not covered" is a claim: only subjects KNOWN to declare no source make it
    const anySources = subs.some((s) => s.hasSources === true)
    const noneDeclared = subs.every((s) => s.hasSources === false)
    if (input.reads && !subs.some((s) => readSubjects.has(s.id))) {
      blank = anySources ? 'dense' : noneDeclared ? 'sparse' : null
    } else if (noneDeclared) {
      blank = 'sparse'
    }
    countries[iso] = { iso, name: f.properties.name, subjects: list, changes: n, blank }
    const step = washStep(n)
    if (step > 0) {
      changed++
      // no geometry: the map paints the wash as a feature state on its land
      // layer (one fill pass over the sphere, not two), keyed by `a3`
      wash.features.push({ type: 'Feature', id: iso, geometry: null, properties: { iso, a3: f.properties.a3, step, count: n } })
    }
    if (blank) {
      if (blank === 'dense') dense++
      else sparse++
      hatch.features.push({ type: 'Feature', id: iso, geometry: f.geometry, properties: { iso, kind: blank } })
    }
  }

  // ── events and situations, in the window, newest first, capped ───────────
  let pointsTotal: number | null = null
  const points: FC = { type: 'FeatureCollection', features: [] }
  if (input.points) {
    const inWin = input.points.filter((p) => inWindow(p.ts, input.window) && inScope(p.lon, p.lat, 3))
    pointsTotal = inWin.length
    inWin.sort((a, b) => b.ts - a.ts)
    for (const p of inWin.slice(0, GLOBE_CAPS.events)) {
      points.features.push(point(p.lon, p.lat, { id: p.id, kind: p.kind, title: p.title, sev: p.severity ?? '', life: p.lifecycle, ts: p.ts }))
    }
  }

  // ── incidents at sea, in the window (valid time, else record time) ───────
  let incidentsTotal: number | null = null
  const incidents: FC = { type: 'FeatureCollection', features: [] }
  if (input.incidents) {
    const inWin = input.incidents.filter(
      (i) => inWindow(Number.isFinite(i.ts) ? i.ts : i.recordedMs, input.window) && inScope(i.lon, i.lat, 5),
    )
    incidentsTotal = inWin.length
    for (const i of inWin.slice(0, GLOBE_CAPS.incidents)) {
      incidents.features.push(point(i.lon, i.lat, { id: i.id, title: i.title, place: i.place ?? '', coasts: i.coasts.join(', '), ts: Number.isFinite(i.ts) ? i.ts : i.recordedMs }))
    }
  }

  // ── open forecasts, at the first subject that declares a place ───────────
  const forecasts: FC = { type: 'FeatureCollection', features: [] }
  let unplaced = 0
  const perSubject = new Map<string, number>()
  for (const fc of input.forecasts ?? []) {
    if (forecasts.features.length >= GLOBE_CAPS.forecasts) break
    const key = fc.subjectKeys.find((k) => subjectCenters[k])
    if (!key || (input.scopeSubject && !fc.subjectKeys.includes(input.scopeSubject))) {
      if (!key) unplaced++
      continue
    }
    const at = input.scopeSubject && subjectCenters[input.scopeSubject] ? input.scopeSubject : key
    const [lon, lat] = subjectCenters[at].center
    const k = perSubject.get(at) ?? 0
    perSubject.set(at, k + 1)
    // several questions on one subject sit around its centre, not on top of each other
    const ang = (k * 72 * Math.PI) / 180
    const r = k === 0 ? 0 : 1.6
    forecasts.features.push(
      point(lon + r * Math.cos(ang), lat + r * Math.sin(ang), {
        id: fc.id,
        question: fc.question,
        due: fc.dueMs,
        step: ringStep(fc.startMs, fc.dueMs, input.nowMs),
        heavy: fc.dueMs - input.nowMs <= 7 * DAY ? 1 : 0,
        subject: at,
      }),
    )
  }

  // ── series at their place ─────────────────────────────────────────────────
  const placed: SeriesGlyph[] = []
  const seriesUnplaced: string[] = []
  for (const s of input.series ?? []) {
    const p = placeSeriesSubject(s.subject)
    if (!p) {
      seriesUnplaced.push(s.name ?? s.subject)
      continue
    }
    if (!inScope(p.lon, p.lat, 6)) continue
    placed.push({ key: s.subject, name: s.name ?? p.name, lon: p.lon, lat: p.lat, latest: s.latest, pct: s.pct, status: seriesStatus(s.pct), revised: s.revised, day: s.day, values: s.values })
  }
  placed.sort((a, b) => Math.abs((b.pct ?? 100) - 100) - Math.abs((a.pct ?? 100) - 100) || a.key.localeCompare(b.key))
  const capped = placed.slice(0, GLOBE_CAPS.seriesPoints)
  const seriesGlyphs = capped.slice(0, GLOBE_CAPS.seriesGlyphs)
  const seriesDots: FC = {
    type: 'FeatureCollection',
    features: capped.slice(GLOBE_CAPS.seriesGlyphs).map((g) => point(g.lon, g.lat, { id: g.key, name: g.name, status: g.status, pct: g.pct ?? -1 })),
  }

  // ── country labels (Natural Earth's own label points and minimum zoom) ───
  const labels: FC = { type: 'FeatureCollection', features: [] }
  for (const f of world.features) {
    const p = f.properties
    if (p.labelX == null || p.labelY == null || !p.name) continue
    if (scopeBox && !inBox(p.labelX, p.labelY, scopeBox, 8)) continue
    labels.features.push(point(p.labelX, p.labelY, { name: p.name.toUpperCase(), min: p.minLabel }))
  }

  // ── home: the subjects' own centre of mass, unless the inset has a subject
  const centers = Object.values(subjectCenters).map((c) => c.center)
  const home = scope
    ? { center: scope.center, zoom: 3 }
    : { center: meanCenter(centers) ?? ([30, 20] as Pos), zoom: 1.35 }

  return {
    wash,
    hatch,
    points,
    incidents,
    forecasts,
    seriesDots,
    seriesGlyphs,
    labels,
    countries,
    subjectCenters,
    home,
    counts: {
      subjectsPlaced: Object.keys(subjectCenters).length,
      changed,
      changes: input.changes ? changesInWindow : null,
      blanksDense: dense,
      blanksSparse: sparse,
      pointsTotal,
      pointsDrawn: points.features.length,
      incidentsTotal,
      incidentsDrawn: incidents.features.length,
      forecastsDrawn: forecasts.features.length,
      forecastsUnplaced: unplaced,
      forecastsTotal: input.forecasts ? input.forecasts.length : null,
      seriesPlaced: placed.length,
      seriesUnplaced,
      seriesTotal: input.series ? input.series.length : null,
    },
  }
}
