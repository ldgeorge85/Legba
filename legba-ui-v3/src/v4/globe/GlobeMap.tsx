/**
 * The globe (U-4) — the Board's showpiece and the Subject inset, on
 * maplibre-gl 5 (layout design §3).
 *
 * At rest on the Board: a globe at world zoom over the subjects' own centre,
 * flattening to Mercator as you zoom in (`style.ts::projectionSpec`), filling
 * its region edge to edge with the controls floating over it. No spin, no
 * pulse: the camera moves only for a click, a selection made elsewhere, a
 * Consult step or Reset, in at most 900 ms (a jump under reduced motion).
 * On the Subject surface the same component is a flat inset fitted to the
 * subject, every layer cut to its area.
 *
 * ONE POINT. A click sets the SHARED selection (`state/selection`): a country
 * selects its subject, a mark selects its record, so the list, the Record
 * sheet and Consult follow; a selection made anywhere else flies the camera
 * here and draws the one magenta ring, dimming every other mark to 45 %. A
 * series place is a `place` selection (U-7: `portwatch:<key>`), so a click on
 * a chokepoint glyph is the shared point too: the Record sheet shows the place,
 * Consult's point names it, and a link or another view can select it back.
 *
 * Every drawn layer is prepared in a worker (`usePrep`), capped
 * (`GLOBE_CAPS`), and fetched only while the region is shown and its row is
 * on (`data.ts`).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import maplibregl from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import { useSelection } from '@/state/selection'
import { useActiveSurface } from '@/state/activeSurface'
import { useCasePlayer } from '@/state/casePlayer'
import { chromeFolds, ConsultChip, HoverCard, LayerCard, Legend, ViewControls, type HoverInfo } from './chrome'
import { mapCoverage, type MapCoverage } from './coverage'
import { GLOBE_CAPS, type GlobeLayerId } from './catalog'
import { ensureMapFonts, globeStyle, labelLayers, markImages, projectionSpec, T } from './style'
import {
  INSET_LAYERS,
  countryHover,
  forecastHover,
  incidentHover,
  pointHover,
  selectPlace,
  selectRecord,
  selectSubject,
  subjectForCountry,
  useGapReason,
  useGlobeModel,
  type GlobePreset,
} from './useGlobeModel'
import { seriesPctSentence, seriesPctText, type SeriesGlyph } from './prep'
import { useGlobeCamera } from './actions'
import { placeId, placeOfId } from './chokepoints'
import { usePinScope } from '@/lib/consultContext'

const EMPTY: GeoJSON.FeatureCollection = { type: 'FeatureCollection', features: [] }
const S = {
  hatch: 'g-hatch',
  points: 'g-points',
  incidents: 'g-incidents',
  forecasts: 'g-forecasts',
  seriesDots: 'g-series-dots',
  labels: 'g-labels',
  sel: 'g-sel',
} as const

const LAYER_IDS: Record<GlobeLayerId, string[]> = {
  change: [],
  blanks: ['hatch-dense', 'hatch-sparse'],
  series: ['series-dots'],
  forecasts: ['forecast-rings'],
  events: ['point-clusters', 'point-cluster-count', 'point-dots'],
  at_sea: ['incidents'],
  drift: [],
}

function reducedMotion(): boolean {
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches
  } catch {
    return false
  }
}
const camMs = () => (reducedMotion() ? 0 : 900)

function projectionKey(surface: string) {
  return `legba_map_projection_${surface}`
}
/**
 * A browser with no GPU renders WebGL in software (SwiftShader, llvmpipe):
 * every globe frame is tessellated and shaded on the CPU — 0.3 to 1.5 s a
 * frame measured in the walk's headless Chromium, depending on the host's
 * load. The degradation ladder (layout design §3) answers that the way it
 * answers a lost context: the map OPENS flat there. The reader's own choice
 * (the Globe/Flat switch, remembered per surface) still wins.
 */
export function softwareRenderer(): boolean {
  try {
    const gl = document.createElement('canvas').getContext('webgl2')
    const ext = gl?.getExtension('WEBGL_debug_renderer_info')
    const name = gl && ext ? String(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL)) : ''
    gl?.getExtension('WEBGL_lose_context')?.loseContext()
    return /swiftshader|llvmpipe|softpipe|software/i.test(name)
  } catch {
    return false
  }
}

function readProjection(surface: string, preset: GlobePreset, software: boolean): 'globe' | 'flat' {
  if (preset === 'inset') return 'flat'
  try {
    const v = localStorage.getItem(projectionKey(surface))
    if (v === 'globe' || v === 'flat') return v
  } catch {
    // per-viewer convenience
  }
  return software ? 'flat' : 'globe'
}

/**
 * The home zoom for this region: the globe fills ~92 % of the shorter side
 * (radius = 512·2^z / 2π px at zoom z); flat, the inhabited world (58° S to
 * 78° N, 56 % of the Mercator square's height) fits both the width and the
 * height — so a short wide band (the stage over Consult at 1366, about
 * 576 × 271) shows the whole world rather than a crop of Eurasia (U-7).
 */
export const MIN_ZOOM = -1

/**
 * Where home looks (U-7). The globe turns to the subjects' own centre. Flat,
 * MapLibre never lets the Mercator world be smaller than the region in either
 * direction (with world copies off), so the world fills the region: in a
 * region at least as wide as tall the whole width is in view and home centres
 * on it (0°, 27° N — the middle of 58° S to 78° N), rather than on the
 * subjects, which would push a continent off one edge; in a tall column (the
 * lead column at 2560 and 3840) the height sets the zoom and some longitudes
 * fall outside whatever the centre, so home keeps the subjects' longitude in
 * the middle. The globe (the reader's switch) fits any region whole.
 */
export function homeCenter(center: [number, number], projection: 'globe' | 'flat', width = 1, height = 0): [number, number] {
  if (projection !== 'flat') return center
  return height > width ? [center[0], 27] : [0, 27]
}
export function homeZoom(width: number, height: number, projection: 'globe' | 'flat'): number {
  const w = Math.max(120, width)
  const h = Math.max(120, height)
  const z =
    projection === 'globe'
      ? Math.log2((0.46 * Math.min(w, h) * 2 * Math.PI) / 512)
      : Math.min(Math.log2(w / 512), Math.log2(h / (512 * 0.56)))
  return Math.max(MIN_ZOOM, Math.min(3, z))
}

function esc(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c] as string)
}

const STATUS_COLOR: Record<SeriesGlyph['status'], string> = { good: T.good, warn: T.warn, critical: T.critical, none: T.ink }

function glyphElement(g: SeriesGlyph, onPick: (g: SeriesGlyph) => void, onHover: (g: SeriesGlyph | null, el: HTMLElement) => void): HTMLElement {
  const el = document.createElement('button')
  el.type = 'button'
  el.className = 'legba-series-glyph'
  el.dataset.testid = 'globe-series-glyph'
  el.dataset.key = g.key
  const pct = seriesPctText(g.pct)
  el.setAttribute('aria-label', `${g.name}: ${g.latest} on ${g.day}, ${seriesPctSentence(g.pct).toLowerCase()}${g.revised ? ', revised' : ''}`)
  el.innerHTML =
    `<span class="legba-series-dot" style="background:${STATUS_COLOR[g.status]}"></span>` +
    `<span class="legba-series-val">${esc(pct)}</span>` +
    (g.revised ? '<span class="legba-series-r">r</span>' : '')
  el.addEventListener('click', (e) => {
    e.stopPropagation()
    onPick(g)
  })
  el.addEventListener('mouseenter', () => onHover(g, el))
  el.addEventListener('mouseleave', () => onHover(null, el))
  return el
}

export default function GlobeMap({
  preset,
  active,
  onFallback,
  onCoverage,
}: {
  preset: GlobePreset
  active: boolean
  onFallback: (why: string) => void
  /** Told what the map can vouch for (the coverage strip under the Board's map). */
  onCoverage?: (c: MapCoverage) => void
}) {
  const surface = useActiveSurface((s) => s.surface)
  const model = useGlobeModel(preset, active, null)
  const { on, setOn, out, figures, selection, selectedAt, reads } = model
  const [layersOpen, setLayersOpen] = useState(false)
  // Told on a change of what it says only (the reads object is new on every render).
  const blanksUnreadable = !!reads.unreadable.blanks
  const coverage = useMemo(
    () => mapCoverage(out, { reads: reads.reads, subjects: reads.subjects }, { blanks: on.blanks }, blanksUnreadable ? { blanks: true } : {}),
    [out, reads.reads, reads.subjects, on.blanks, blanksUnreadable],
  )
  const coverageKey = JSON.stringify(coverage)
  const coverageCb = useRef(onCoverage)
  coverageCb.current = onCoverage
  useEffect(() => coverageCb.current?.(JSON.parse(coverageKey) as MapCoverage), [coverageKey])

  const wrapRef = useRef<HTMLDivElement | null>(null)
  const containerRef = useRef<HTMLDivElement | null>(null)
  const mapRef = useRef<maplibregl.Map | null>(null)
  const markersRef = useRef<maplibregl.Marker[]>([])
  const [loaded, setLoaded] = useState(false)
  const [ready, setReady] = useState(false)
  // When the map was first ready with its data, in ms from navigation
  // (`performance.now()`): the walk reads it (U-8). Before, the walk timed the
  // map by when IT looked — after the landing's own ready, the 3 s settle and
  // the landing's screenshot — so a slow landing read made the map look slow.
  const [readyAt, setReadyAt] = useState<number | null>(null)
  const [software] = useState(softwareRenderer)
  const [projection, setProjection] = useState<'globe' | 'flat'>(() => readProjection(surface, preset, software))
  const projectionRef = useRef(projection)
  projectionRef.current = projection
  const [hover, setHover] = useState<HoverInfo | null>(null)
  const [hoverSubject, setHoverSubject] = useState<string | null>(null)
  // The series place that is the point: the shared selection when it is a
  // `place` (U-7), drawn from the glyph when it is on the map, else from the
  // gazetteer (the series layer off, or not read yet).
  const localPoint = useMemo<SeriesGlyph | null>(() => {
    if (selection?.kind !== 'place') return null
    const g = out?.seriesGlyphs.find((x) => placeId(x.key) === selection.id)
    if (g) return g
    const p = placeOfId(selection.id)
    return p ? { key: p.key, name: p.name, lon: p.lon, lat: p.lat, latest: 0, pct: null, status: 'none', revised: false, day: '', values: [] } : null
  }, [selection, out])
  usePinScope(localPoint ? { kind: 'place', id: placeId(localPoint.key), label: localPoint.name } : null, 'map')
  const [clusterNote, setClusterNote] = useState<string | null>(null)
  // 0 until measured: the chrome waits for it, so a narrow region opens with the layer card folded
  const [width, setWidth] = useState(0)
  const [height, setHeight] = useState(0)
  const gapReason = useGapReason(hoverSubject)
  const outRef = useRef(out)
  outRef.current = out
  const homeRef = useRef<{ center: [number, number]; zoom: number } | null>(null)
  /** The camera is at home and nobody has moved it since (a resize then follows the new home). */
  const atHomeRef = useRef(false)

  // ── create the map once the fonts are in ────────────────────────────────
  useEffect(() => {
    const container = containerRef.current
    if (!container || mapRef.current) return
    let cancelled = false
    void ensureMapFonts().then(() => {
      if (cancelled || !containerRef.current) return
      let map: maplibregl.Map
      try {
        map = new maplibregl.Map({
          container,
          style: globeStyle(projection),
          center: [30, 20],
          zoom: preset === 'inset' ? 2 : 1.35,
          minZoom: MIN_ZOOM,
          maxZoom: 8,
          attributionControl: { compact: true },
          dragRotate: false,
          pitchWithRotate: false,
          renderWorldCopies: false,
          // Not 0: a zero fade forces a FULL symbol placement on every frame;
          // any other value lets placement time-slice (2 ms a frame).
          fadeDuration: 120,
        })
      } catch (e) {
        onFallback(`the WebGL map could not start (${String(e).slice(0, 80)})`)
        return
      }
      map.touchZoomRotate.disableRotation()
      mapRef.current = map
      // The browser walk drives and measures the camera through this handle.
      ;(container as HTMLElement & { __legbaMap?: maplibregl.Map }).__legbaMap = map
      map.on('webglcontextlost', () => onFallback('the WebGL context was lost'))
      // Where the camera rests, for the on-screen report and an Undo (U-7).
      // a reader's drag, wheel or pinch leaves home
      map.on('movestart', (e: { originalEvent?: unknown }) => {
        if (e.originalEvent) atHomeRef.current = false
      })
      map.on('moveend', () => {
        const c = map.getCenter()
        useGlobeCamera.getState().settle({ lon: c.lng, lat: c.lat, zoom: map.getZoom(), projection: projectionRef.current })
      })
      map.on('load', () => {
        for (const img of markImages()) if (!map.hasImage(img.id)) map.addImage(img.id, img.data, { pixelRatio: img.pixelRatio })
        for (const id of Object.values(S)) {
          if (id === S.points) map.addSource(id, { type: 'geojson', data: EMPTY, cluster: true, clusterRadius: 40, clusterMaxZoom: 6 })
          else map.addSource(id, { type: 'geojson', data: EMPTY })
        }
        const add = (l: maplibregl.LayerSpecification, before?: string) => map.addLayer(l, before)
        add({ id: 'hatch-dense', type: 'fill', source: S.hatch, filter: ['==', ['get', 'kind'], 'dense'], paint: { 'fill-pattern': 'hatch-dense' } })
        add({ id: 'hatch-sparse', type: 'fill', source: S.hatch, filter: ['==', ['get', 'kind'], 'sparse'], paint: { 'fill-pattern': 'hatch-sparse' } })
        add({
          id: 'point-clusters',
          type: 'circle',
          source: S.points,
          filter: ['has', 'point_count'],
          paint: {
            'circle-color': 'rgba(230,226,216,0.16)',
            'circle-stroke-color': T.ink,
            'circle-stroke-width': 0.75,
            'circle-radius': ['step', ['get', 'point_count'], 10, 10, 13, 50, 17, 200, 21],
          },
        })
        add({
          id: 'point-cluster-count',
          type: 'symbol',
          source: S.points,
          filter: ['has', 'point_count'],
          layout: { 'text-field': ['to-string', ['get', 'point_count']], 'text-font': ['IBM Plex Mono'], 'text-size': 12, 'text-allow-overlap': true },
          paint: { 'text-color': T.ink },
        })
        add({
          id: 'point-dots',
          type: 'circle',
          source: S.points,
          filter: ['!', ['has', 'point_count']],
          paint: {
            'circle-radius': 3.5,
            'circle-color': ['coalesce', ['get', ['get', 'sev'], ['literal', T.sev]], T.ink],
            'circle-stroke-color': T.ground,
            'circle-stroke-width': 1,
          },
        })
        add({ id: 'incidents', type: 'symbol', source: S.incidents, layout: { 'icon-image': 'incident', 'icon-allow-overlap': true, 'icon-size': 1 } })
        add({
          id: 'series-dots',
          type: 'circle',
          source: S.seriesDots,
          paint: {
            'circle-radius': 3.5,
            'circle-color': ['match', ['get', 'status'], 'good', T.good, 'warn', T.warn, 'critical', T.critical, T.ink],
            'circle-stroke-color': T.ground,
            'circle-stroke-width': 1,
          },
        })
        add({
          id: 'forecast-rings',
          type: 'symbol',
          source: S.forecasts,
          layout: {
            'icon-image': ['concat', 'ring-', ['to-string', ['get', 'step']], ['case', ['==', ['get', 'heavy'], 1], '-h', '']],
            'icon-allow-overlap': true,
            'icon-ignore-placement': true,
          },
        })
        add({
          id: 'sel-ring',
          type: 'circle',
          source: S.sel,
          paint: { 'circle-radius': 15, 'circle-color': 'rgba(0,0,0,0)', 'circle-stroke-color': T.accent, 'circle-stroke-width': 2 },
        })
        setLoaded(true)
        // Labels join after the first settled frame, so the first frame does
        // not also rasterise every glyph (the load's longest task).
        map.once('idle', () => {
          for (const l of labelLayers()) if (!map.getLayer(l.id)) map.addLayer(l, 'point-clusters')
          map.once('idle', () => setReady(true))
        })
      })
    })
    return () => {
      cancelled = true
      for (const m of markersRef.current) m.remove()
      markersRef.current = []
      mapRef.current?.remove()
      mapRef.current = null
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps -- create once

  // ── size ─────────────────────────────────────────────────────────────────
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const ro = new ResizeObserver(() => {
      setWidth(el.clientWidth)
      setHeight(el.clientHeight)
      const map = mapRef.current
      map?.resize()
      // The frame resizes the map (a splitter, Consult moving, the stage): a
      // camera still at home follows to the new home; one the reader moved stays.
      const home = homeRef.current
      if (map && home && preset === 'board' && atHomeRef.current) {
        const hc = homeCenter(home.center, projectionRef.current, el.clientWidth, el.clientHeight)
        const z = homeZoom(el.clientWidth, el.clientHeight, projectionRef.current)
        const c = map.getCenter()
        if (Math.abs(z - home.zoom) >= 0.02 || Math.abs(c.lng - hc[0]) > 0.5) {
          homeRef.current = { ...home, zoom: z }
          map.jumpTo({ center: hc, zoom: z })
        }
      }
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  useEffect(() => {
    if (active) mapRef.current?.resize()
  }, [active])

  // ── data → sources ───────────────────────────────────────────────────────
  useEffect(() => {
    const map = mapRef.current
    if (!map || !loaded || !out) return
    const set = (id: string, data: unknown) => (map.getSource(id) as maplibregl.GeoJSONSource | undefined)?.setData(data as GeoJSON.FeatureCollection)
    // One layer family per frame: each new kind of mark compiles its shaders on
    // the frame it first draws, and a software renderer pays for that on the
    // main thread — spread over three frames, no one frame pays for all.
    const stages: Array<() => void> = [
      () => {
        set(S.hatch, out.hatch)
      },
      () => {
        set(S.points, out.points)
        set(S.incidents, out.incidents)
        set(S.labels, out.labels)
      },
      () => {
        set(S.forecasts, out.forecasts)
        set(S.seriesDots, out.seriesDots)
      },
    ]
    let raf = 0
    let i = 0
    const next = () => {
      stages[i++]()
      if (i < stages.length) raf = requestAnimationFrame(() => (raf = requestAnimationFrame(next)))
    }
    next()
    return () => cancelAnimationFrame(raf)
  }, [out, loaded])

  // ── the change wash: feature states on the land layer ───────────────────
  useEffect(() => {
    const map = mapRef.current
    if (!map || !loaded) return
    try {
      map.removeFeatureState({ source: 'world' })
      if (on.change && out) {
        for (const f of out.wash.features) {
          const a3 = String(f.properties.a3 ?? '')
          if (a3) map.setFeatureState({ source: 'world', id: a3 }, { wash: Number(f.properties.step) })
        }
      }
    } catch {
      // a state the renderer refuses leaves the land unwashed
    }
  }, [out, loaded, on.change])

  // ── the home view, once the subjects are placed ──────────────────────────
  useEffect(() => {
    const map = mapRef.current
    if (!map || !loaded || !out || homeRef.current) return
    if (out.counts.subjectsPlaced === 0 && reads.subjects === null) return
    const el = containerRef.current
    homeRef.current = { center: out.home.center, zoom: homeZoom(el?.clientWidth ?? 800, el?.clientHeight ?? 600, projection) }
    if (preset === 'inset' && model.scopeSubject && out.subjectCenters[model.scopeSubject]?.bounds) {
      const b = out.subjectCenters[model.scopeSubject].bounds!
      map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: 24, maxZoom: 5, duration: 0 })
    } else {
      map.jumpTo({ center: homeCenter(homeRef.current.center, projection, el?.clientWidth, el?.clientHeight), zoom: homeRef.current.zoom })
      atHomeRef.current = true
    }
  }, [out, loaded, preset, model.scopeSubject, reads.subjects]) // eslint-disable-line react-hooks/exhaustive-deps -- once

  // ── series glyphs (the only DOM marks; at most GLOBE_CAPS.seriesGlyphs) ───
  useEffect(() => {
    const map = mapRef.current
    for (const m of markersRef.current) m.remove()
    markersRef.current = []
    if (!map || !loaded || !out || !on.series) return
    const pick = (g: SeriesGlyph) => selectPlace(g.key, g.name)
    const hov = (g: SeriesGlyph | null, el: HTMLElement) => {
      if (!g) return setHover(null)
      const r = el.getBoundingClientRect()
      const w = wrapRef.current?.getBoundingClientRect()
      setHover({
        x: r.left - (w?.left ?? 0),
        y: r.bottom - (w?.top ?? 0) - 10,
        title: g.name,
        lines: [
          `${g.latest.toLocaleString('en-US')} ${reads.seriesUnit ?? 'per day'} on ${g.day}${g.revised ? ' (revised)' : ''}`,
          seriesPctSentence(g.pct),
          reads.seriesAsOf ? `As recorded ${reads.seriesAsOf.slice(0, 10)}` : '',
        ].filter(Boolean),
      })
    }
    for (const g of out.seriesGlyphs.slice(0, GLOBE_CAPS.seriesGlyphs)) {
      markersRef.current.push(new maplibregl.Marker({ element: glyphElement(g, pick, hov), anchor: 'left', offset: [-5, 0] }).setLngLat([g.lon, g.lat]).addTo(map))
    }
    // Labels that would overlap a more-moved place's label fold to their dot
    // (checked when the camera settles, never per frame).
    const declutter = () => {
      const taken: Array<[number, number, number, number]> = []
      for (const m of markersRef.current) {
        const p = map.project(m.getLngLat())
        const box: [number, number, number, number] = [p.x - 6, p.y - 10, p.x + 66, p.y + 10]
        const hit = taken.some((b) => box[0] < b[2] && box[2] > b[0] && box[1] < b[3] && box[3] > b[1])
        m.getElement().classList.toggle('legba-series-glyph--folded', hit)
        if (!hit) taken.push(box)
      }
    }
    declutter()
    map.on('moveend', declutter)
    return () => {
      map.off('moveend', declutter)
    }
  }, [out, loaded, on.series, reads.seriesUnit, reads.seriesAsOf])

  // ── visibility ───────────────────────────────────────────────────────────
  useEffect(() => {
    const map = mapRef.current
    if (!map || !loaded) return
    for (const [id, ids] of Object.entries(LAYER_IDS) as [GlobeLayerId, string[]][]) {
      for (const l of ids) if (map.getLayer(l)) map.setLayoutProperty(l, 'visibility', on[id] ? 'visible' : 'none')
    }
  }, [on, loaded])

  // ── the point: ring, dimming, camera ─────────────────────────────────────
  const selId = localPoint ? null : selection?.id ?? null
  useEffect(() => {
    const map = mapRef.current
    if (!map || !loaded) return
    const at = localPoint ? { lon: localPoint.lon, lat: localPoint.lat, isos: [] as string[], bounds: null } : selectedAt
    ;(map.getSource(S.sel) as maplibregl.GeoJSONSource | undefined)?.setData({
      type: 'FeatureCollection',
      features: at && !(at.isos.length > 0 && at.bounds) ? [{ type: 'Feature', geometry: { type: 'Point', coordinates: [at.lon, at.lat] }, properties: {} }] : [],
    })
    const dim = !!at
    const byId: maplibregl.ExpressionSpecification | number = dim && selId ? ['case', ['==', ['get', 'id'], selId], 1, 0.45] : dim ? 0.45 : 1
    const isos = at?.isos ?? []
    const safe = (fn: () => void) => {
      try {
        fn()
      } catch {
        // a paint the renderer refuses never takes the map down
      }
    }
    // the wash dims through a feature state on the land layer
    const washed = on.change ? out?.wash.features ?? [] : []
    for (const f of washed) {
      const a3 = String(f.properties.a3 ?? '')
      if (a3) safe(() => map.setFeatureState({ source: 'world', id: a3 }, { dim: dim && isos.length > 0 && !isos.includes(String(f.properties.iso)) ? true : dim && isos.length === 0 }))
    }
    safe(() => map.setPaintProperty('point-dots', 'circle-opacity', byId))
    safe(() => map.setPaintProperty('series-dots', 'circle-opacity', dim ? 0.45 : 1))
    safe(() => map.setPaintProperty('forecast-rings', 'icon-opacity', byId))
    safe(() => map.setPaintProperty('incidents', 'icon-opacity', byId))
    // a selected subject's area takes the accent outline
    if (!map.getLayer('sel-area')) {
      safe(() =>
        map.addLayer({ id: 'sel-area', type: 'line', source: 'world', paint: { 'line-color': T.accent, 'line-width': 2 }, filter: ['in', ['get', 'ISO_A2'], ['literal', []]] }, 'point-clusters'),
      )
    }
    safe(() => map.setFilter('sel-area', ['in', ['coalesce', ['get', 'ISO_A2_EH'], ['get', 'ISO_A2']], ['literal', at && at.bounds ? isos : []]]))
    for (const m of markersRef.current) {
      const el = m.getElement()
      el.style.opacity = dim && !(localPoint && el.dataset.key === localPoint.key) ? '0.45' : '1'
      el.classList.toggle('legba-series-glyph--point', !!localPoint && el.dataset.key === localPoint.key)
    }
  }, [selectedAt, localPoint, selId, loaded, out])

  // Fly to the point when it changes (a click here, or a selection elsewhere).
  const lastFlownRef = useRef<string | null>(null)
  useEffect(() => {
    const map = mapRef.current
    if (!map || !loaded) return
    const key = localPoint ? `local:${localPoint.key}` : selection && selectedAt ? `${selection.kind}:${selection.id}` : null
    if (!key || key === lastFlownRef.current) return
    lastFlownRef.current = key
    atHomeRef.current = false
    try {
      if (localPoint) map.easeTo({ center: [localPoint.lon, localPoint.lat], zoom: Math.max(map.getZoom(), 3), duration: camMs() })
      else if (selectedAt?.bounds) {
        const b = selectedAt.bounds
        map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: 60, maxZoom: preset === 'inset' ? 5 : 4, duration: camMs() })
      } else if (selectedAt) map.easeTo({ center: [selectedAt.lon, selectedAt.lat], zoom: Math.max(map.getZoom(), 3), duration: camMs() })
    } catch {
      // a refused camera move leaves the map where it was
    }
  }, [selection, selectedAt, localPoint, loaded, preset])
  useEffect(() => {
    if (!selection) lastFlownRef.current = null
  }, [selection])

  // A case's `map reset` (C-1b): back home.
  const resetNonce = useCasePlayer((s) => s.cameraResetNonce)
  const resetSeen = useRef(resetNonce)
  const goHome = useCallback(() => {
    const map = mapRef.current
    const home = homeRef.current
    if (!map || !home) return
    if (preset === 'inset' && model.scopeSubject && outRef.current?.subjectCenters[model.scopeSubject]?.bounds) {
      const b = outRef.current.subjectCenters[model.scopeSubject].bounds!
      map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: 24, maxZoom: 5, duration: camMs() })
    } else {
      // home for the region as it is now (the frame may have resized it)
      const el = containerRef.current
      if (el && el.clientWidth > 0) homeRef.current = { ...home, zoom: homeZoom(el.clientWidth, el.clientHeight, projectionRef.current) }
      map.easeTo({ center: homeCenter(home.center, projectionRef.current, el?.clientWidth, el?.clientHeight), zoom: homeRef.current!.zoom, duration: camMs() })
      atHomeRef.current = true
    }
  }, [preset, model.scopeSubject])
  useEffect(() => {
    if (resetNonce === resetSeen.current) return
    resetSeen.current = resetNonce
    goHome()
  }, [resetNonce, goHome])

  // The map's actions (`actions.ts`): a camera request from a frame, a panel
  // contract or a test, executed once the map is up (a request made while it
  // was hidden waits for it).
  const camReq = useGlobeCamera((s) => s.request)
  const camNonce = useGlobeCamera((s) => s.nonce)
  const camSeen = useRef(0)
  useEffect(() => {
    const map = mapRef.current
    if (!map || !loaded || !camReq || camNonce === camSeen.current) return
    // a subject's area is known once the layers are prepared; wait for it
    if ((camReq.kind === 'subject' || camReq.kind === 'home') && !outRef.current) return
    camSeen.current = camNonce
    atHomeRef.current = camReq.kind === 'home'
    try {
      if (camReq.kind === 'home') goHome()
      else if (camReq.kind === 'place') map.easeTo({ center: [camReq.lon, camReq.lat], zoom: camReq.zoom ?? Math.max(map.getZoom(), 3), duration: camMs() })
      else if (camReq.kind === 'bounds') map.fitBounds([[camReq.west, camReq.south], [camReq.east, camReq.north]], { padding: 40, maxZoom: 6, duration: camMs() })
      else {
        const c = outRef.current?.subjectCenters[camReq.id]
        if (c?.bounds) map.fitBounds([[c.bounds[0], c.bounds[1]], [c.bounds[2], c.bounds[3]]], { padding: 40, maxZoom: 5, duration: camMs() })
        else if (c) map.easeTo({ center: c.center, zoom: Math.max(map.getZoom(), 3), duration: camMs() })
      }
    } catch {
      // a refused camera move leaves the map where it was
    }
  }, [camReq, camNonce, loaded, goHome, out])

  // ── interactions ─────────────────────────────────────────────────────────
  useEffect(() => {
    const map = mapRef.current
    if (!map || !loaded) return
    const pickable = ['forecast-rings', 'series-dots', 'incidents', 'point-dots', 'point-clusters', 'hatch-dense', 'hatch-sparse', 'land']
    const featureAt = (pt: maplibregl.PointLike) => {
      const layers = pickable.filter((l) => map.getLayer(l) && map.getLayoutProperty(l, 'visibility') !== 'none')
      return map.queryRenderedFeatures(pt, { layers })[0] ?? null
    }
    const isoOf = (p: Record<string, unknown>) => {
      const v = [p.iso, p.ISO_A2, p.ISO_A2_EH].find((x) => typeof x === 'string' && /^[A-Z]{2}$/.test(x))
      return (v as string | undefined) ?? null
    }
    const onClick = (e: maplibregl.MapMouseEvent) => {
      const f = featureAt(e.point)
      const o = outRef.current
      if (!f) {
        useSelection.getState().clear()
        return
      }
      const p = (f.properties ?? {}) as Record<string, unknown>
      const layer = f.layer.id
      if (layer === 'point-clusters') {
        const src = map.getSource(S.points) as maplibregl.GeoJSONSource
        void src.getClusterExpansionZoom(Number(p.cluster_id)).then((z) =>
          map.easeTo({ center: (f.geometry as GeoJSON.Point).coordinates as [number, number], zoom: z, duration: camMs() }),
        )
        return
      }
      if (layer === 'point-dots') return selectRecord(p.kind === 'situation' ? 'situation' : 'event', String(p.id), String(p.title ?? ''))
      if (layer === 'incidents') return selectRecord('signal', String(p.id), String(p.title ?? 'Incident at sea'))
      if (layer === 'forecast-rings') {
        const sid = String(p.subject)
        return selectSubject(sid, o?.countries ? Object.values(o.countries).flatMap((c) => c.subjects).find((s) => s.id === sid)?.name ?? sid : sid)
      }
      if (layer === 'series-dots') {
        return selectPlace(String(p.id), String(p.name ?? p.id))
      }
      const iso = isoOf(p)
      const subj = iso ? subjectForCountry(o?.countries[iso]) : null
      if (subj) selectSubject(subj.id, subj.name)
      else useSelection.getState().clear()
    }
    const onMove = (e: maplibregl.MapMouseEvent) => {
      const f = featureAt(e.point)
      map.getCanvas().style.cursor = f && f.layer.id !== 'land' ? 'pointer' : ''
      if (!f) {
        setHover(null)
        setHoverSubject(null)
        return
      }
      const p = (f.properties ?? {}) as Record<string, unknown>
      const o = outRef.current
      const { x, y } = e.point
      switch (f.layer.id) {
        case 'point-dots':
          setHoverSubject(null)
          return setHover(pointHover(p, x, y))
        case 'point-clusters':
          setHoverSubject(null)
          return setHover({ x, y, title: `${String(p.point_count)} events and situations here`, lines: ['Click to spread them out'] })
        case 'incidents':
          setHoverSubject(null)
          return setHover(incidentHover(p, x, y))
        case 'forecast-rings': {
          setHoverSubject(null)
          const sid = String(p.subject)
          const name = o ? Object.values(o.countries).flatMap((c) => c.subjects).find((s) => s.id === sid)?.name ?? null : null
          return setHover(forecastHover(p, x, y, name))
        }
        case 'series-dots':
          setHoverSubject(null)
          return setHover({ x, y, title: String(p.name), lines: [seriesPctSentence(Number(p.pct) >= 0 ? Number(p.pct) : null)] })
        default: {
          const iso = isoOf(p)
          const facts = iso ? o?.countries[iso] : undefined
          if (!facts) {
            setHoverSubject(null)
            return setHover(null)
          }
          const subj = subjectForCountry(facts)
          setHoverSubject(facts.blank === 'dense' && subj ? subj.id : null)
          return setHover(countryHover(facts, x, y, null))
        }
      }
    }
    const onLeave = () => {
      setHover(null)
      setHoverSubject(null)
    }
    map.on('click', onClick)
    map.on('mousemove', onMove)
    map.on('mouseout', onLeave)
    return () => {
      map.off('click', onClick)
      map.off('mousemove', onMove)
      map.off('mouseout', onLeave)
    }
  }, [loaded])

  // The declared gap's reason joins the card when it arrives.
  useEffect(() => {
    if (!gapReason || !hover || !hoverSubject) return
    if (hover.lines.some((l) => l.startsWith('Declared gap:'))) return
    setHover({ ...hover, lines: [...hover.lines, `Declared gap: ${gapReason}`] })
  }, [gapReason, hover, hoverSubject])

  // Escape clears the point.
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      useSelection.getState().clear()
    }
    el.addEventListener('keydown', onKey)
    return () => el.removeEventListener('keydown', onKey)
  }, [])

  // The cluster note, after the map settles with events on.
  useEffect(() => {
    const map = mapRef.current
    if (!map || !loaded || !on.events || !out) {
      setClusterNote(null)
      return
    }
    const onIdle = () => {
      try {
        const feats = map.querySourceFeatures(S.points)
        const places = new Set(feats.map((f) => (f.properties?.cluster_id != null ? `c${f.properties.cluster_id}` : `p${f.properties?.id}`)))
        const total = out.counts.pointsTotal ?? 0
        setClusterNote(
          `${out.counts.pointsDrawn}${total > out.counts.pointsDrawn ? ` of ${total}` : ''} drawn · clustered into ${places.size} places at this zoom`,
        )
      } catch {
        setClusterNote(null)
      }
    }
    map.on('idle', onIdle)
    return () => {
      map.off('idle', onIdle)
    }
  }, [loaded, on.events, out])

  // ── projection ───────────────────────────────────────────────────────────
  const chooseProjection = useCallback(
    (p: 'globe' | 'flat') => {
      setProjection(p)
      try {
        localStorage.setItem(projectionKey(surface), p)
      } catch {
        // per-viewer convenience
      }
      try {
        mapRef.current?.setProjection(projectionSpec(p) as maplibregl.ProjectionSpecification)
        const el = containerRef.current
        if (homeRef.current && el) homeRef.current = { ...homeRef.current, zoom: homeZoom(el.clientWidth, el.clientHeight, p) }
        const m = mapRef.current
        if (m && homeRef.current && m.getZoom() < 3) m.easeTo({ center: homeCenter(homeRef.current.center, p, el?.clientWidth, el?.clientHeight), zoom: homeRef.current.zoom, duration: camMs() / 2 })
      } catch {
        // a refused projection leaves the current one
      }
    },
    [surface],
  )

  const marks = useMemo(() => {
    if (!out) return 0
    return (
      (on.change ? out.wash.features.length : 0) +
      (on.blanks ? out.hatch.features.length : 0) +
      (on.events ? out.points.features.length : 0) +
      (on.at_sea ? out.incidents.features.length : 0) +
      (on.forecasts ? out.forecasts.features.length : 0) +
      (on.series ? out.seriesGlyphs.length + out.seriesDots.features.length : 0)
    )
  }, [out, on])

  useEffect(() => {
    if (ready && out && readyAt == null) setReadyAt(Math.round(performance.now()))
  }, [ready, out, readyAt])

  const only = preset === 'inset' ? INSET_LAYERS : undefined
  const folds = chromeFolds(width, height, preset === 'inset')
  return (
    <div
      ref={wrapRef}
      className="relative h-full w-full overflow-hidden"
      style={{ background: T.ground }}
      data-testid="globe-map"
      data-renderer="webgl"
      data-preset={preset}
      data-projection={projection}
      data-software={software ? '1' : '0'}
      data-ready={ready && !!out ? '1' : '0'}
      data-ready-at={readyAt ?? ''}
      data-marks={marks}
      data-prep-ms={model.prepMs ?? ''}
      tabIndex={-1}
    >
      <div ref={containerRef} className="h-full w-full" />
      {width > 0 && height > 0 && <LayerCard on={on} figures={figures} onToggle={setOn} collapsedByDefault={folds.layers} only={only} onOpenChange={setLayersOpen} />}
      <ViewControls projection={projection} onProjection={chooseProjection} onZoom={(d) => (d > 0 ? mapRef.current?.zoomIn({ duration: camMs() / 3 }) : mapRef.current?.zoomOut({ duration: camMs() / 3 }))} onReset={goHome} showProjection={preset === 'board'} />
      {preset === 'board' && <ConsultChip />}
      {width > 0 && height > 0 && (
        <Legend
          on={on}
          figures={figures}
          out={out}
          seriesUnit={reads.seriesUnit}
          clusterNote={clusterNote}
          only={only}
          collapsedByDefault={folds.legend}
          room={layersOpen ? 'shared' : 'alone'}
          note={software && projection === 'flat' && preset === 'board' ? 'Opened flat: this browser draws without a GPU. The Globe switch turns the globe on.' : null}
        />
      )}
      <HoverCard hover={hover} width={width} />
    </div>
  )
}
