/**
 * The World — WebGL map renderer on maplibre-gl (S7-T5, deepened by P4-3).
 *
 * The WebGL upgrade of LeafletWorldMap, rendered INSIDE the TileWebGLOverlay
 * harness so the GPU canvas escapes the Dockview tile transform (the "black
 * tile" the S7-T2 spike hit). Fully self-contained — the dark basemap is our
 * bundled Natural-Earth GeoJSON (or the optional self-hosted PMTiles archive),
 * resolved by `resolveBasemapStyle()`. No external tile server, no glyphs, no
 * API key; a strict CSP blocks external hosts.
 *
 * Layers, bottom→top:
 *   - BANDED-VERDICT CHOROPLETH (default): each country tinted by the ICD-203
 *     confidence band of its verified `country_composition`. Hover shows the
 *     band, faithfulness, and the country's windowed activity (top movers);
 *     click selects the desk into the Inspector (shared selection store).
 *   - SIGNAL-DENSITY: a light maplibre heatmap (Heat), OR a richer deck.gl
 *     HexagonLayer (Hex) camera-synced over the basemap (P4-3 feature 2).
 *   - CO-MENTION ARCS (deck.gl ArcLayer, P4-3 feature 2): the "narrative echo"
 *     flow graph — countries a single signal jointly references.
 *   - signal clusters / finding / situation circles: the shared point layers.
 *   - GEO-CONVERGENCE markers (P4-3 feature 4): the A7 deterministic
 *     cross-stream correlator's active bins, made visible.
 *   - WATCH-LOCATIONS (P4-3 feature 5): operator "watch here" points + radius
 *     rings; proximate signals are haloed.
 *
 * deck.gl is DYNAMICALLY IMPORTED only when a deck layer (Hex / Arcs) is first
 * turned on — the map opens without paying for it. It reads/writes the SAME
 * `useWorldState` store as LeafletWorldMap so the reused chrome behaves
 * identically.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import maplibregl from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import { Layers, MapPin, Plus, X } from 'lucide-react'
import { cn } from '@/lib/cn'
import { useMapResize } from '@/lib/useMapResize'
import { resolveBasemapStyle, WORLD_GEOJSON_PATH } from '@/lib/basemap'
import { COUNTRY_BY_ISO2, resolveCountry } from '@/lib/countryGeo'
import { useSelection, selectRow } from '@/state/selection'
import { densityPoints, coMentionArcs } from '@/lib/mapLayers'
import {
  circleRing,
  nearbyCountByWatch,
  nearestWatch,
  WATCH_RADIUS_OPTIONS,
  type WatchLocation,
} from '@/lib/watchLocations'
import { useWorldSignals, useWorldFindings, useWorldSituations } from './mapData'
import { useWorldState } from './worldState'
import { useCountryVerdicts, CONFIDENCE_FILL, CHOROPLETH_LEGEND } from './countryVerdicts'
import { useConvergenceMarkers } from './convergenceData'
import { useWatchState } from './watchState'
import { SEVERITY_COLOR, SITUATION_COLOR, type Severity, type WorldFinding } from './types'

const SEVERITY_RANK: Record<Severity, number> = { critical: 4, high: 3, medium: 2, low: 1, info: 0 }
const LAND_UNASSESSED = 'rgba(0,0,0,0)' // choropleth fallback — let basemap land show

/** Accent colours for the P4-3 layers, distinct from every severity / situation
 *  colour so they read as their own thing on the map. */
const CONVERGENCE_COLOR = '#22d3ee' // cyan-400 — the cross-stream correlator
const WATCH_COLOR = '#a78bfa' // violet-400 — operator watch points

type DensityMode = 'off' | 'heat' | 'hex'

// --- source / layer ids -----------------------------------------------------
const SRC_COUNTRIES = 'legba-countries'
const SRC_SIGNALS = 'legba-signals'
const SRC_FINDINGS = 'legba-findings'
const SRC_SITUATIONS = 'legba-situations'
const SRC_CONVERGENCE = 'legba-convergence'
const SRC_WATCH = 'legba-watch'
const SRC_WATCH_RING = 'legba-watch-ring'
const L_CHORO_FILL = 'legba-choropleth-fill'
const L_CHORO_LINE = 'legba-choropleth-line'
const L_HEAT = 'legba-signal-heat'
const L_WATCH_RING = 'legba-watch-ring-line'
const L_WATCH_HALO = 'legba-watch-halo'
const L_SIGNALS = 'legba-signal-clusters'
const L_FINDINGS = 'legba-finding-circles'
const L_SITUATIONS = 'legba-situation-circles'
const L_CONVERGENCE_GLOW = 'legba-convergence-glow'
const L_CONVERGENCE = 'legba-convergence-ring'
const L_WATCH = 'legba-watch-points'

type FC = GeoJSON.FeatureCollection<GeoJSON.Point>
type LineFC = GeoJSON.FeatureCollection<GeoJSON.LineString>

/** The deck.gl classes we lazy-load (kept off the initial bundle). */
interface DeckLib {
  MapboxOverlay: typeof import('@deck.gl/mapbox').MapboxOverlay
  HexagonLayer: typeof import('@deck.gl/aggregation-layers').HexagonLayer
  ArcLayer: typeof import('@deck.gl/layers').ArcLayer
}

interface SignalCluster {
  lat: number
  lon: number
  count: number
  maxSeverity: Severity
  isEvidence: boolean
  near: boolean
}

/** ISO2 → readable label for hover popups (falls back to the code). */
function countryLabel(iso2: string): string {
  return COUNTRY_BY_ISO2[iso2]?.name ?? iso2
}

/** Minimal HTML escape for popup content (titles are arbitrary text). */
function esc(s: string): string {
  return s.replace(/[&<>"']/g, (c) =>
    c === '&' ? '&amp;' : c === '<' ? '&lt;' : c === '>' ? '&gt;' : c === '"' ? '&quot;' : '&#39;',
  )
}

export default function MapLibreWorldMap() {
  const { signals } = useWorldSignals()
  const { findings } = useWorldFindings()
  const { situations } = useWorldSituations()
  const { verdicts } = useCountryVerdicts()
  const { markers: convergence } = useConvergenceMarkers()

  const layers = useWorldState((s) => s.layers)
  const windowStartMs = useWorldState((s) => s.windowStartMs)
  const windowEndMs = useWorldState((s) => s.windowEndMs)
  const filters = useWorldState((s) => s.filters)
  const setCount = useWorldState((s) => s.setCount)
  const setFilterOptions = useWorldState((s) => s.setFilterOptions)
  const openDrawer = useWorldState((s) => s.openDrawer)
  const readScope = useWorldState((s) => s.readScope)
  const select = useSelection((s) => s.select)

  // Watch-locations (P4-3 feature 5).
  const watches = useWatchState((s) => s.watches)
  const placing = useWatchState((s) => s.placing)
  const setPlacing = useWatchState((s) => s.setPlacing)
  const addWatch = useWatchState((s) => s.add)

  // Map-mode layers (not in the world store — specific to the WebGL renderer).
  const [choropleth, setChoropleth] = useState(true)
  const [density, setDensity] = useState<DensityMode>('off')
  const [arcs, setArcs] = useState(false)
  const [showConvergence, setShowConvergence] = useState(true)
  const [showWatch, setShowWatch] = useState(true)

  const [deckLib, setDeckLib] = useState<DeckLib | null>(null)

  const mapContainerRef = useRef<HTMLDivElement | null>(null)
  const mapRef = useRef<maplibregl.Map | null>(null)
  const overlayRef = useRef<InstanceType<DeckLib['MapboxOverlay']> | null>(null)
  const loadedRef = useRef(false)
  const popupRef = useRef<maplibregl.Popup | null>(null)

  useMapResize(mapContainerRef, () => mapRef.current)

  const minRank = filters.minSeverity != null ? SEVERITY_RANK[filters.minSeverity] : 0
  const evidenceIds = useMemo(() => new Set(readScope?.signalIds ?? []), [readScope])

  // --- windowed + filtered slices (parity with LeafletWorldMap) --------------
  const winSignals = useMemo(
    () =>
      signals.filter(
        (s) =>
          s.ts >= windowStartMs &&
          s.ts <= windowEndMs &&
          SEVERITY_RANK[s.severity] >= minRank &&
          (filters.source == null || s.sourceId === filters.source) &&
          (filters.country == null || s.countries.includes(filters.country)),
      ),
    [signals, windowStartMs, windowEndMs, minRank, filters.source, filters.country],
  )
  const winFindings = useMemo(
    () =>
      findings.filter(
        (f) =>
          f.lat != null &&
          f.lon != null &&
          f.ts >= windowStartMs &&
          f.ts <= windowEndMs &&
          SEVERITY_RANK[f.severity] >= minRank &&
          (filters.country == null || f.countries.includes(filters.country)),
      ),
    [findings, windowStartMs, windowEndMs, minRank, filters.country],
  )
  const winSituations = useMemo(
    () =>
      situations.filter(
        (s) =>
          s.lat != null &&
          s.lon != null &&
          s.ts >= windowStartMs &&
          s.ts <= windowEndMs &&
          (filters.country == null || s.countries.includes(filters.country)),
      ),
    [situations, windowStartMs, windowEndMs, filters.country],
  )

  // Per-country windowed activity — drives the choropleth "top movers" hover.
  const findingsByCountry = useMemo(() => {
    const m = new Map<string, WorldFinding[]>()
    for (const f of winFindings) {
      for (const c of f.countries) {
        const arr = m.get(c) ?? []
        arr.push(f)
        m.set(c, arr)
      }
    }
    for (const arr of m.values()) {
      arr.sort((a, b) => SEVERITY_RANK[b.severity] - SEVERITY_RANK[a.severity])
    }
    return m
  }, [winFindings])
  const signalCountByCountry = useMemo(() => {
    const m = new Map<string, number>()
    for (const s of winSignals) for (const c of s.countries) m.set(c, (m.get(c) ?? 0) + 1)
    return m
  }, [winSignals])

  // Signals near ANY watch — per-watch counts for the watch panel (feature 5).
  const nearWatchCounts = useMemo(
    () => nearbyCountByWatch(watches, winSignals),
    [watches, winSignals],
  )

  // Publish source/country dropdown options + counts for the reused LayerPanel.
  useEffect(() => {
    const sources = new Set<string>()
    const countries = new Set<string>()
    for (const s of signals) {
      if (s.ts >= windowStartMs && s.ts <= windowEndMs && SEVERITY_RANK[s.severity] >= minRank) {
        if (s.sourceId) sources.add(s.sourceId)
        for (const c of s.countries) countries.add(c)
      }
    }
    for (const f of findings) for (const c of f.countries) countries.add(c)
    for (const s of situations) for (const c of s.countries) countries.add(c)
    setFilterOptions({ sources: [...sources].sort(), countries: [...countries].sort() })
  }, [signals, findings, situations, windowStartMs, windowEndMs, minRank, setFilterOptions])

  useEffect(() => setCount('signals', winSignals.length), [winSignals.length, setCount])
  useEffect(() => setCount('findings', winFindings.length), [winFindings.length, setCount])
  useEffect(() => setCount('situations', winSituations.length), [winSituations.length, setCount])

  // Aggregate signals to per-coordinate clusters (same keying as Leaflet), plus
  // a `near` flag for the watch-proximity halo.
  const signalFC = useMemo<FC>(() => {
    const m = new Map<string, SignalCluster>()
    for (const s of winSignals) {
      const key = `${s.lat.toFixed(2)},${s.lon.toFixed(2)}`
      const c = m.get(key)
      const ev = evidenceIds.size > 0 && evidenceIds.has(s.id)
      if (c) {
        c.count++
        c.isEvidence = c.isEvidence || ev
        if (SEVERITY_RANK[s.severity] > SEVERITY_RANK[c.maxSeverity]) c.maxSeverity = s.severity
      } else {
        m.set(key, {
          lat: s.lat,
          lon: s.lon,
          count: 1,
          maxSeverity: s.severity,
          isEvidence: ev,
          near: false,
        })
      }
    }
    for (const c of m.values()) {
      c.near = watches.length > 0 && nearestWatch({ lat: c.lat, lon: c.lon }, watches) !== null
    }
    return {
      type: 'FeatureCollection',
      features: [...m.values()].map((c) => ({
        type: 'Feature',
        geometry: { type: 'Point', coordinates: [c.lon, c.lat] },
        properties: {
          count: c.count,
          color: SEVERITY_COLOR[c.maxSeverity],
          radius: Math.min(6 + Math.sqrt(c.count) * 2.5, 28),
          evidence: c.isEvidence ? 1 : 0,
          near: c.near ? 1 : 0,
        },
      })),
    }
  }, [winSignals, evidenceIds, watches])

  const findingFC = useMemo<FC>(
    () => ({
      type: 'FeatureCollection',
      features: winFindings.map((f) => ({
        type: 'Feature',
        geometry: { type: 'Point', coordinates: [f.lon as number, f.lat as number] },
        properties: { id: f.id, title: f.title, color: SEVERITY_COLOR[f.severity] },
      })),
    }),
    [winFindings],
  )

  const situationFC = useMemo<FC>(
    () => ({
      type: 'FeatureCollection',
      features: winSituations.map((s) => ({
        type: 'Feature',
        geometry: { type: 'Point', coordinates: [s.lon as number, s.lat as number] },
        properties: { id: s.id, title: s.title, color: SITUATION_COLOR[s.lifecycle] },
      })),
    }),
    [winSituations],
  )

  const convergenceFC = useMemo<FC>(
    () => ({
      type: 'FeatureCollection',
      features: convergence.map((m) => ({
        type: 'Feature',
        geometry: { type: 'Point', coordinates: [m.lon, m.lat] },
        properties: {
          id: m.id,
          label: m.label,
          binKind: m.binKind,
          familyCount: m.familyCount ?? 0,
          signalCount: m.signalCount ?? 0,
          radius: Math.min(10 + (m.familyCount ?? 3) * 2, 22),
        },
      })),
    }),
    [convergence],
  )

  const watchFC = useMemo<FC>(
    () => ({
      type: 'FeatureCollection',
      features: watches.map((w) => ({
        type: 'Feature',
        geometry: { type: 'Point', coordinates: [w.lon, w.lat] },
        properties: { id: w.id, label: w.label },
      })),
    }),
    [watches],
  )

  const watchRingFC = useMemo<LineFC>(
    () => ({
      type: 'FeatureCollection',
      features: watches.map((w) => ({
        type: 'Feature',
        geometry: { type: 'LineString', coordinates: circleRing(w, w.radiusKm) },
        properties: { id: w.id },
      })),
    }),
    [watches],
  )

  // deck.gl layer data (feature 2) — density + co-mention arcs.
  const densityData = useMemo(() => densityPoints(winSignals), [winSignals])
  // DATA SEAM (recorded 2026-07, not a UI bug): an arc needs a signal whose
  // `geo[]` names >=2 countries, but enrichment currently resolves each
  // signal to a SINGLE country — so no pair ever forms and this layer is
  // honest-empty until multi-country geo lands upstream. See the matching
  // note on `coMentionArcs` in lib/mapLayers.ts.
  const arcData = useMemo(
    () => coMentionArcs(winSignals, (iso2) => resolveCountry(iso2)),
    [winSignals],
  )

  // Choropleth fill-color expression: match ISO_A2 → confidence-band colour.
  const choroExpr = useMemo(() => {
    const pairs: string[] = []
    for (const [iso2, cv] of verdicts) pairs.push(iso2, CONFIDENCE_FILL[cv.verdict.confidence])
    return pairs
  }, [verdicts])

  // A counter bumped on style `load` so the data/paint sync effects run once the
  // sources/layers exist (they no-op before then).
  const [styleReady, setStyleReady] = useState(0)
  // Latest values for the imperative hover/click handlers (they close over refs).
  const verdictsRef = useRef(verdicts)
  verdictsRef.current = verdicts
  const findingsByCountryRef = useRef(findingsByCountry)
  findingsByCountryRef.current = findingsByCountry
  const signalCountByCountryRef = useRef(signalCountByCountry)
  signalCountByCountryRef.current = signalCountByCountry
  const placingRef = useRef(placing)
  placingRef.current = placing
  const addWatchRef = useRef(addWatch)
  addWatchRef.current = addWatch

  // --- init the map once -----------------------------------------------------
  useEffect(() => {
    if (!mapContainerRef.current || mapRef.current) return
    let cancelled = false
    const container = mapContainerRef.current
    resolveBasemapStyle().then((style) => {
      if (cancelled) return
      const map = new maplibregl.Map({
        container,
        style,
        center: [0, 20],
        zoom: 1.3,
        minZoom: 1,
        maxZoom: 8,
        attributionControl: false,
        dragRotate: false,
      })
      map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right')
      // `legba-map-popup` keys the dark-card override in globals.css —
      // MapLibre's stock popup is a white card, which renders this popup
      // HTML's light ink invisible.
      const popup = new maplibregl.Popup({
        closeButton: false,
        closeOnClick: false,
        offset: 8,
        className: 'legba-map-popup',
      })
      popupRef.current = popup

      map.on('load', () => {
        map.addSource(SRC_COUNTRIES, { type: 'geojson', data: WORLD_GEOJSON_PATH })
        const empty: FC = { type: 'FeatureCollection', features: [] }
        map.addSource(SRC_SIGNALS, { type: 'geojson', data: empty })
        map.addSource(SRC_FINDINGS, { type: 'geojson', data: empty })
        map.addSource(SRC_SITUATIONS, { type: 'geojson', data: empty })
        map.addSource(SRC_CONVERGENCE, { type: 'geojson', data: empty })
        map.addSource(SRC_WATCH, { type: 'geojson', data: empty })
        map.addSource(SRC_WATCH_RING, {
          type: 'geojson',
          data: { type: 'FeatureCollection', features: [] } as LineFC,
        })

        map.addLayer({
          id: L_CHORO_FILL,
          type: 'fill',
          source: SRC_COUNTRIES,
          paint: { 'fill-color': LAND_UNASSESSED, 'fill-opacity': 0.55 },
        })
        map.addLayer({
          id: L_CHORO_LINE,
          type: 'line',
          source: SRC_COUNTRIES,
          paint: { 'line-color': '#2a3346', 'line-width': 0.4 },
        })
        map.addLayer({
          id: L_HEAT,
          type: 'heatmap',
          source: SRC_SIGNALS,
          layout: { visibility: 'none' },
          paint: {
            'heatmap-weight': ['interpolate', ['linear'], ['get', 'count'], 1, 0.3, 50, 1],
            'heatmap-intensity': 0.8,
            'heatmap-radius': 26,
            'heatmap-opacity': 0.7,
            'heatmap-color': [
              'interpolate', ['linear'], ['heatmap-density'],
              0, 'rgba(10,12,16,0)',
              0.3, '#1d4ed8',
              0.55, '#eab308',
              0.8, '#f97316',
              1, '#ef4444',
            ],
          },
        })
        // Watch radius ring (above basemap, below markers).
        map.addLayer({
          id: L_WATCH_RING,
          type: 'line',
          source: SRC_WATCH_RING,
          paint: {
            'line-color': WATCH_COLOR,
            'line-width': 1,
            'line-opacity': 0.5,
            'line-dasharray': [2, 2],
          },
        })
        // Halo behind signals proximate to a watch (feature 5).
        map.addLayer({
          id: L_WATCH_HALO,
          type: 'circle',
          source: SRC_SIGNALS,
          filter: ['==', ['get', 'near'], 1],
          paint: {
            'circle-radius': ['+', ['get', 'radius'], 6],
            'circle-color': 'rgba(0,0,0,0)',
            'circle-stroke-width': 2,
            'circle-stroke-color': WATCH_COLOR,
            'circle-stroke-opacity': 0.9,
          },
        })
        map.addLayer({
          id: L_SIGNALS,
          type: 'circle',
          source: SRC_SIGNALS,
          paint: {
            'circle-radius': ['get', 'radius'],
            'circle-color': ['get', 'color'],
            'circle-opacity': 0.45,
            'circle-stroke-width': ['case', ['==', ['get', 'evidence'], 1], 2.5, 1],
            'circle-stroke-color': ['case', ['==', ['get', 'evidence'], 1], '#ffffff', ['get', 'color']],
          },
        })
        map.addLayer({
          id: L_FINDINGS,
          type: 'circle',
          source: SRC_FINDINGS,
          paint: {
            'circle-radius': 6,
            'circle-color': ['get', 'color'],
            'circle-opacity': 0.85,
            'circle-stroke-width': 1,
            'circle-stroke-color': '#ffffff',
          },
        })
        map.addLayer({
          id: L_SITUATIONS,
          type: 'circle',
          source: SRC_SITUATIONS,
          paint: {
            'circle-radius': 9,
            'circle-color': ['get', 'color'],
            'circle-opacity': 0.85,
            'circle-stroke-width': 2,
            'circle-stroke-color': '#0a0c10',
          },
        })
        // Geo-convergence markers (feature 4) — a bright hollow ring + glow.
        map.addLayer({
          id: L_CONVERGENCE_GLOW,
          type: 'circle',
          source: SRC_CONVERGENCE,
          paint: {
            'circle-radius': ['+', ['get', 'radius'], 6],
            'circle-color': CONVERGENCE_COLOR,
            'circle-opacity': 0.12,
          },
        })
        map.addLayer({
          id: L_CONVERGENCE,
          type: 'circle',
          source: SRC_CONVERGENCE,
          paint: {
            'circle-radius': ['get', 'radius'],
            'circle-color': 'rgba(0,0,0,0)',
            'circle-stroke-width': 2.5,
            'circle-stroke-color': CONVERGENCE_COLOR,
            'circle-stroke-opacity': 0.95,
          },
        })
        // Watch points on top.
        map.addLayer({
          id: L_WATCH,
          type: 'circle',
          source: SRC_WATCH,
          paint: {
            'circle-radius': 6,
            'circle-color': WATCH_COLOR,
            'circle-opacity': 0.9,
            'circle-stroke-width': 2,
            'circle-stroke-color': '#0a0c10',
          },
        })

        // Placement: any map click while arming a watch drops one and disarms.
        map.on('click', (e) => {
          if (!placingRef.current) return
          addWatchRef.current('', e.lngLat.lat, e.lngLat.lng)
        })

        // Click → shared selection + drill drawer (skip while placing a watch).
        map.on('click', L_FINDINGS, (e) => {
          if (placingRef.current) return
          const p = e.features?.[0]?.properties
          if (!p) return
          openDrawer({ title: String(p.title ?? 'finding'), signals: [], findings: [] })
          select({ kind: 'finding', id: String(p.id), label: String(p.title ?? '') })
        })
        map.on('click', L_SITUATIONS, (e) => {
          if (placingRef.current) return
          const p = e.features?.[0]?.properties
          if (!p) return
          openDrawer({ title: String(p.title ?? 'situation'), signals: [], findings: [] })
          select({ kind: 'situation', id: String(p.id), label: String(p.title ?? '') })
        })
        // Convergence marker click → open the alert in the Inspector.
        map.on('click', L_CONVERGENCE, (e) => {
          if (placingRef.current) return
          const p = e.features?.[0]?.properties
          if (!p) return
          selectRow('alert', String(p.id), `Convergence: ${String(p.label ?? '')}`, {
            origin: 'world-map-convergence',
          })
        })
        // Choropleth click → select the country's desk into the Inspector.
        map.on('click', L_CHORO_FILL, (e) => {
          if (placingRef.current) return
          const iso2 = e.features?.[0]?.properties?.ISO_A2 as string | undefined
          if (!iso2) return
          const cv = verdictsRef.current.get(iso2)
          if (!cv?.targetId) return
          selectRow('target', cv.targetId, countryLabel(iso2), { origin: 'world-map-choropleth' })
        })

        for (const id of [L_FINDINGS, L_SITUATIONS, L_SIGNALS, L_CONVERGENCE, L_WATCH, L_CHORO_FILL]) {
          map.on('mouseenter', id, () => (map.getCanvas().style.cursor = 'pointer'))
          map.on('mouseleave', id, () => {
            map.getCanvas().style.cursor = ''
            if (id === L_CHORO_FILL || id === L_CONVERGENCE) popup.remove()
          })
        }
        // Convergence hover → the bin's diversity + volume.
        map.on('mousemove', L_CONVERGENCE, (e) => {
          const p = e.features?.[0]?.properties
          if (!p) return
          popup
            .setLngLat(e.lngLat)
            .setHTML(
              `<div style="font:12px system-ui;color:#e2e8f0"><b style="color:${CONVERGENCE_COLOR}">Geo convergence</b><br/>` +
                `${esc(String(p.label ?? ''))}<br/>` +
                `${Number(p.familyCount)} source families · ${Number(p.signalCount)} signals</div>`,
            )
            .addTo(map)
        })
        // Choropleth hover → country name, band, faithfulness + windowed movers.
        map.on('mousemove', L_CHORO_FILL, (e) => {
          const iso2 = e.features?.[0]?.properties?.ISO_A2 as string | undefined
          if (!iso2) return
          const cv = verdictsRef.current.get(iso2)
          if (!cv) {
            popup.remove()
            return
          }
          const sig = signalCountByCountryRef.current.get(iso2) ?? 0
          const finds = findingsByCountryRef.current.get(iso2) ?? []
          const movers = finds
            .slice(0, 2)
            .map((f) => `<div style="color:#94a3b8">• ${esc(f.title)}</div>`)
            .join('')
          popup
            .setLngLat(e.lngLat)
            .setHTML(
              `<div style="font:12px system-ui;color:#e2e8f0;max-width:220px"><b>${esc(countryLabel(iso2))}</b><br/>` +
                `confidence: ${esc(cv.verdict.confidence)}` +
                (cv.verdict.faithfulness != null
                  ? ` · faithful ${Math.round(cv.verdict.faithfulness * 100)}%`
                  : '') +
                `<div style="margin-top:3px;color:#64748b">${sig} signals · ${finds.length} findings in window</div>` +
                (movers ? `<div style="margin-top:2px">${movers}</div>` : '') +
                `</div>`,
            )
            .addTo(map)
        })

        loadedRef.current = true
        setStyleReady((n) => n + 1) // kick the sync effects now that layers exist
      })

      mapRef.current = map
    })
    return () => {
      cancelled = true
      overlayRef.current = null
      mapRef.current?.remove()
      mapRef.current = null
      loadedRef.current = false
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps -- init once

  const setData = (srcId: string, fc: FC | LineFC) => {
    const map = mapRef.current
    if (!map || !loadedRef.current) return
    const src = map.getSource(srcId) as maplibregl.GeoJSONSource | undefined
    src?.setData(fc)
  }

  useEffect(() => setData(SRC_SIGNALS, signalFC), [signalFC, styleReady])
  useEffect(() => setData(SRC_FINDINGS, findingFC), [findingFC, styleReady])
  useEffect(() => setData(SRC_SITUATIONS, situationFC), [situationFC, styleReady])
  useEffect(() => setData(SRC_CONVERGENCE, convergenceFC), [convergenceFC, styleReady])
  useEffect(() => setData(SRC_WATCH, watchFC), [watchFC, styleReady])
  useEffect(() => setData(SRC_WATCH_RING, watchRingFC), [watchRingFC, styleReady])

  // Choropleth colour.
  useEffect(() => {
    const map = mapRef.current
    if (!map || !loadedRef.current) return
    const expr =
      choroExpr.length > 0
        ? ['match', ['get', 'ISO_A2'], ...choroExpr, LAND_UNASSESSED]
        : LAND_UNASSESSED
    map.setPaintProperty(L_CHORO_FILL, 'fill-color', expr as never)
  }, [choroExpr, styleReady])

  // Layer visibility toggles.
  useEffect(() => {
    const map = mapRef.current
    if (!map || !loadedRef.current) return
    const vis = (id: string, on: boolean) =>
      map.setLayoutProperty(id, 'visibility', on ? 'visible' : 'none')
    vis(L_CHORO_FILL, choropleth)
    vis(L_CHORO_LINE, choropleth)
    vis(L_HEAT, density === 'heat')
    vis(L_SIGNALS, layers.signals)
    vis(L_WATCH_HALO, layers.signals && showWatch && watches.length > 0)
    vis(L_FINDINGS, layers.findings)
    vis(L_SITUATIONS, layers.situations)
    vis(L_CONVERGENCE, showConvergence)
    vis(L_CONVERGENCE_GLOW, showConvergence)
    vis(L_WATCH, showWatch)
    vis(L_WATCH_RING, showWatch)
  }, [
    choropleth, density, layers.signals, layers.findings, layers.situations,
    showConvergence, showWatch, watches.length, styleReady,
  ])

  // --- deck.gl overlay (lazy) ------------------------------------------------
  const deckWanted = density === 'hex' || arcs
  useEffect(() => {
    if (!deckWanted || deckLib) return
    let cancelled = false
    Promise.all([
      import('@deck.gl/mapbox'),
      import('@deck.gl/aggregation-layers'),
      import('@deck.gl/layers'),
    ]).then(([mb, agg, lay]) => {
      if (cancelled) return
      setDeckLib({
        MapboxOverlay: mb.MapboxOverlay,
        HexagonLayer: agg.HexagonLayer,
        ArcLayer: lay.ArcLayer,
      })
    })
    return () => {
      cancelled = true
    }
  }, [deckWanted, deckLib])

  // Create the camera-synced overlay control once deck is loaded + map ready.
  useEffect(() => {
    const map = mapRef.current
    if (!deckLib || !map || !loadedRef.current || overlayRef.current) return
    const overlay = new deckLib.MapboxOverlay({ interleaved: false, layers: [] })
    map.addControl(overlay as unknown as maplibregl.IControl)
    overlayRef.current = overlay
  }, [deckLib, styleReady])

  // Rebuild deck layers from the current data + toggles.
  useEffect(() => {
    const overlay = overlayRef.current
    if (!overlay || !deckLib) return
    const built: unknown[] = []
    if (density === 'hex') {
      // deck.gl's layer props are heavily generic across sub-packages; the
      // prop object is cast so tsc doesn't chase HexagonLayerProps' accessor +
      // Color-tuple generics (the shapes below are valid at runtime).
      const hexProps = {
        id: 'legba-deck-hex',
        data: densityData,
        getPosition: (d: { position: [number, number] }) => d.position,
        getElevationWeight: (d: { weight: number }) => d.weight,
        getColorWeight: (d: { weight: number }) => d.weight,
        radius: 60_000,
        elevationScale: 0,
        extruded: false,
        opacity: 0.5,
        colorRange: [
          [29, 78, 216],
          [234, 179, 8],
          [249, 115, 22],
          [239, 68, 68],
          [220, 38, 38],
          [153, 27, 27],
        ],
        pickable: false,
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
      } as any
      built.push(new deckLib.HexagonLayer(hexProps))
    }
    if (arcs) {
      const arcProps = {
        id: 'legba-deck-arcs',
        data: arcData,
        getSourcePosition: (d: { source: [number, number] }) => d.source,
        getTargetPosition: (d: { target: [number, number] }) => d.target,
        getSourceColor: [34, 211, 238, 180],
        getTargetColor: [167, 139, 250, 180],
        getWidth: (d: { count: number }) => Math.min(1 + Math.sqrt(d.count), 6),
        greatCircle: true,
        pickable: false,
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
      } as any
      built.push(new deckLib.ArcLayer(arcProps))
    }
    // deck's setProps layer typing is nominal across its sub-packages; the
    // instances above are valid Layers.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    overlay.setProps({ layers: built as any })
  }, [deckLib, density, arcs, densityData, arcData])

  const placeWatchAtCenter = () => {
    const map = mapRef.current
    if (!map) return
    const c = map.getCenter()
    addWatch('', c.lat, c.lng)
  }

  return (
    <div className="relative h-full w-full" style={{ background: '#0a0c10' }} data-testid="maplibre-world">
      <div ref={mapContainerRef} className="h-full w-full" />
      {placing && (
        <div className="pointer-events-none absolute inset-x-0 top-3 z-20 flex justify-center">
          <span className="rounded-full border border-violet-500/60 bg-surface-200/95 px-3 py-1 text-xs text-violet-200 shadow-lg backdrop-blur-sm">
            Click the map to drop a watch-location
          </span>
        </div>
      )}
      <MapModeControl
        choropleth={choropleth}
        density={density}
        arcs={arcs}
        showConvergence={showConvergence}
        showWatch={showWatch}
        convergenceCount={convergence.length}
        onToggleChoropleth={() => setChoropleth((v) => !v)}
        onSetDensity={setDensity}
        onToggleArcs={() => setArcs((v) => !v)}
        onToggleConvergence={() => setShowConvergence((v) => !v)}
        onToggleWatch={() => setShowWatch((v) => !v)}
      />
      <WatchControl
        watches={watches}
        placing={placing}
        nearCounts={nearWatchCounts}
        onArm={() => setPlacing(!placing)}
        onPlaceCenter={placeWatchAtCenter}
      />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Map-mode control — choropleth / density(off·heat·hex) / arcs / convergence /
// watch toggles + the choropleth legend, bottom-left. Collapsed to a single
// icon button by default; the data-layer toggles live in the reused LayerPanel.
// ---------------------------------------------------------------------------
function MapModeControl({
  choropleth,
  density,
  arcs,
  showConvergence,
  showWatch,
  convergenceCount,
  onToggleChoropleth,
  onSetDensity,
  onToggleArcs,
  onToggleConvergence,
  onToggleWatch,
}: {
  choropleth: boolean
  density: DensityMode
  arcs: boolean
  showConvergence: boolean
  showWatch: boolean
  convergenceCount: number
  onToggleChoropleth: () => void
  onSetDensity: (m: DensityMode) => void
  onToggleArcs: () => void
  onToggleConvergence: () => void
  onToggleWatch: () => void
}) {
  const [open, setOpen] = useState(false)
  const checkbox = 'h-3.5 w-3.5 accent-accent-info'
  return (
    <div className="pointer-events-auto absolute bottom-3 left-3 z-10">
      {!open ? (
        <button
          type="button"
          onClick={() => setOpen(true)}
          aria-label="Map layers"
          className={cn(
            'flex items-center gap-1.5 rounded-lg border border-slate-800 bg-surface-200/95 px-2.5 py-1.5',
            'text-xs text-slate-300 shadow-lg backdrop-blur-sm hover:text-slate-100',
          )}
        >
          <Layers className="h-3.5 w-3.5" />
          Map layers
        </button>
      ) : (
        <div className="w-[210px] rounded-lg border border-slate-800 bg-surface-200/95 p-2 shadow-lg backdrop-blur-sm">
          <div className="mb-1 flex items-center justify-between">
            <span className="text-[10px] font-semibold uppercase tracking-wide text-slate-500">Map layers</span>
            <button type="button" onClick={() => setOpen(false)} className="text-[10px] text-slate-400 hover:text-slate-200">
              hide
            </button>
          </div>

          <label className="flex cursor-pointer items-center gap-2 py-0.5 text-xs text-slate-300">
            <input type="checkbox" checked={choropleth} onChange={onToggleChoropleth} className={checkbox} />
            Verdict choropleth
          </label>

          <div className="mt-1.5 flex items-center justify-between gap-2 py-0.5 text-xs text-slate-300">
            <span>Signal density</span>
            <div className="flex overflow-hidden rounded border border-slate-800">
              {(['off', 'heat', 'hex'] as DensityMode[]).map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => onSetDensity(m)}
                  aria-pressed={density === m}
                  className={cn(
                    'px-1.5 py-0.5 text-[11px] capitalize transition-colors',
                    density === m ? 'bg-accent-info/20 text-accent-info' : 'text-slate-500 hover:text-slate-300',
                  )}
                >
                  {m}
                </button>
              ))}
            </div>
          </div>

          <label className="mt-1 flex cursor-pointer items-center gap-2 py-0.5 text-xs text-slate-300">
            <input type="checkbox" checked={arcs} onChange={onToggleArcs} className={checkbox} />
            Co-mention arcs
          </label>

          <label className="flex cursor-pointer items-center gap-2 py-0.5 text-xs text-slate-300">
            <input type="checkbox" checked={showConvergence} onChange={onToggleConvergence} className={checkbox} />
            <span className="flex-1">Geo convergence</span>
            <span className="tabular-nums text-[10px] text-slate-500">{convergenceCount}</span>
          </label>

          <label className="flex cursor-pointer items-center gap-2 py-0.5 text-xs text-slate-300">
            <input type="checkbox" checked={showWatch} onChange={onToggleWatch} className={checkbox} />
            Watch-locations
          </label>

          {choropleth && (
            <ul className="mt-1.5 space-y-0.5 border-t border-slate-800 pt-1.5">
              {CHOROPLETH_LEGEND.map((b) => (
                <li key={b.level} className="flex items-center gap-2 text-[11px] text-slate-400">
                  <span aria-hidden className="h-2.5 w-2.5 rounded-sm" style={{ backgroundColor: CONFIDENCE_FILL[b.level] }} />
                  {b.label}
                </li>
              ))}
              <li className="flex items-center gap-2 pt-0.5 text-[11px] text-slate-400">
                <span aria-hidden className="h-2.5 w-2.5 rounded-full border-2" style={{ borderColor: CONVERGENCE_COLOR }} />
                Convergence bin
              </li>
            </ul>
          )}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Watch-locations control (feature 5) — bottom-right. Arm "watch here" (next
// map click drops a point) or drop one at the current center; the list shows
// each watch's radius (editable), proximate-signal count, and a remove button.
// ---------------------------------------------------------------------------
function WatchControl({
  watches,
  placing,
  nearCounts,
  onArm,
  onPlaceCenter,
}: {
  watches: WatchLocation[]
  placing: boolean
  nearCounts: Record<string, number>
  onArm: () => void
  onPlaceCenter: () => void
}) {
  const [open, setOpen] = useState(false)
  const setRadius = useWatchState((s) => s.setRadius)
  const remove = useWatchState((s) => s.remove)

  return (
    <div className="pointer-events-auto absolute bottom-3 right-3 z-10">
      {!open ? (
        <button
          type="button"
          onClick={() => setOpen(true)}
          aria-label="Watch-locations"
          className={cn(
            'flex items-center gap-1.5 rounded-lg border border-slate-800 bg-surface-200/95 px-2.5 py-1.5',
            'text-xs text-slate-300 shadow-lg backdrop-blur-sm hover:text-slate-100',
          )}
        >
          <MapPin className="h-3.5 w-3.5" style={{ color: WATCH_COLOR }} />
          Watch
          {watches.length > 0 && (
            <span className="tabular-nums text-[10px] text-slate-500">{watches.length}</span>
          )}
        </button>
      ) : (
        <div className="w-[230px] rounded-lg border border-slate-800 bg-surface-200/95 p-2 shadow-lg backdrop-blur-sm">
          <div className="mb-1 flex items-center justify-between">
            <span className="text-[10px] font-semibold uppercase tracking-wide text-slate-500">Watch-locations</span>
            <button type="button" onClick={() => setOpen(false)} className="text-[10px] text-slate-400 hover:text-slate-200">
              hide
            </button>
          </div>

          <div className="flex gap-1">
            <button
              type="button"
              onClick={onArm}
              aria-pressed={placing}
              className={cn(
                'flex flex-1 items-center justify-center gap-1 rounded border px-2 py-1 text-[11px] transition-colors',
                placing
                  ? 'border-violet-500/60 bg-violet-500/20 text-violet-200'
                  : 'border-slate-800 bg-surface-100 text-slate-300 hover:text-slate-100',
              )}
            >
              <Plus className="h-3 w-3" />
              {placing ? 'Click map…' : 'Watch here'}
            </button>
            <button
              type="button"
              onClick={onPlaceCenter}
              title="Drop at map center"
              className="rounded border border-slate-800 bg-surface-100 px-2 py-1 text-[11px] text-slate-300 hover:text-slate-100"
            >
              Center
            </button>
          </div>

          {watches.length === 0 ? (
            <p className="mt-2 text-[11px] text-slate-500">
              No watch-locations yet. Drop one to highlight nearby signals.
            </p>
          ) : (
            <ul className="mt-2 max-h-48 space-y-1 overflow-y-auto">
              {watches.map((w) => (
                <li key={w.id} className="rounded border border-slate-800 bg-surface-100 px-2 py-1">
                  <div className="flex items-center justify-between gap-1">
                    <span className="truncate text-[11px] text-slate-200" title={w.label}>
                      {w.label}
                    </span>
                    <button
                      type="button"
                      onClick={() => remove(w.id)}
                      aria-label={`Remove watch ${w.label}`}
                      className="shrink-0 text-slate-500 hover:text-slate-200"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </div>
                  <div className="mt-0.5 flex items-center justify-between gap-1">
                    <select
                      value={w.radiusKm}
                      onChange={(e) => setRadius(w.id, Number(e.target.value))}
                      aria-label={`Radius for ${w.label}`}
                      className="rounded border border-slate-800 bg-surface-200 px-1 py-0.5 text-[10px] text-slate-300"
                    >
                      {WATCH_RADIUS_OPTIONS.map((r) => (
                        <option key={r} value={r}>
                          {r} km
                        </option>
                      ))}
                    </select>
                    <span className="tabular-nums text-[10px] text-slate-500">
                      {nearCounts[w.id] ?? 0} nearby
                    </span>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
