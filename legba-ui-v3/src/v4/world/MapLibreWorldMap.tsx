/**
 * The World — WebGL map renderer on maplibre-gl (S7-T5).
 *
 * This is the WebGL upgrade of LeafletWorldMap, rendered INSIDE the
 * TileWebGLOverlay harness so the GPU canvas escapes the Dockview tile transform
 * (the "black tile" the S7-T2 spike hit). It is fully self-contained — the dark
 * basemap is our bundled Natural-Earth GeoJSON (or the optional self-hosted
 * PMTiles archive), resolved by `resolveBasemapStyle()`. No external tile server,
 * no glyphs, no API key — a strict CSP blocks external hosts.
 *
 * Layers, bottom→top:
 *   - BANDED-VERDICT CHOROPLETH (default): each country tinted by the ICD-203
 *     confidence band of its verified `country_composition` — the platform's
 *     faithfulness selling point, on the map. Assessed countries only; the rest
 *     stay basemap land (honest "unassessed").
 *   - SIGNAL-DENSITY heatmap (toggle): GPU heatmap over the windowed signals.
 *   - signal clusters / finding / situation circles: parity with the Leaflet
 *     map, driving the same shared selection + drill Drawer via the world store.
 *
 * It reads/writes the SAME `useWorldState` store as LeafletWorldMap (window,
 * filters, per-layer toggles, decay, counts, drawer, readScope), so the reused
 * LayerPanel / Drawer / TimeScrubber chrome around it behaves identically.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import maplibregl from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import { Layers } from 'lucide-react'
import { cn } from '@/lib/cn'
import { useMapResize } from '@/lib/useMapResize'
import { resolveBasemapStyle, WORLD_GEOJSON_PATH } from '@/lib/basemap'
import { COUNTRY_BY_ISO2 } from '@/lib/countryGeo'
import { useSelection } from '@/state/selection'
import { useWorldSignals, useWorldFindings, useWorldSituations } from './mapData'
import { useWorldState } from './worldState'
import { useCountryVerdicts, CONFIDENCE_FILL, CHOROPLETH_LEGEND } from './countryVerdicts'
import { SEVERITY_COLOR, SITUATION_COLOR, type Severity } from './types'

const SEVERITY_RANK: Record<Severity, number> = { critical: 4, high: 3, medium: 2, low: 1, info: 0 }
const LAND_UNASSESSED = 'rgba(0,0,0,0)' // choropleth fallback — let basemap land show

// --- source / layer ids -----------------------------------------------------
const SRC_COUNTRIES = 'legba-countries'
const SRC_SIGNALS = 'legba-signals'
const SRC_FINDINGS = 'legba-findings'
const SRC_SITUATIONS = 'legba-situations'
const L_CHORO_FILL = 'legba-choropleth-fill'
const L_CHORO_LINE = 'legba-choropleth-line'
const L_HEAT = 'legba-signal-heat'
const L_SIGNALS = 'legba-signal-clusters'
const L_FINDINGS = 'legba-finding-circles'
const L_SITUATIONS = 'legba-situation-circles'

type FC = GeoJSON.FeatureCollection<GeoJSON.Point>

interface SignalCluster {
  lat: number
  lon: number
  count: number
  maxSeverity: Severity
  isEvidence: boolean
}

/** ISO2 → readable label for the hover popup (falls back to the code). */
function countryLabel(iso2: string): string {
  return COUNTRY_BY_ISO2[iso2]?.name ?? iso2
}

export default function MapLibreWorldMap() {
  const { signals } = useWorldSignals()
  const { findings } = useWorldFindings()
  const { situations } = useWorldSituations()
  const { verdicts } = useCountryVerdicts()

  const layers = useWorldState((s) => s.layers)
  const windowStartMs = useWorldState((s) => s.windowStartMs)
  const windowEndMs = useWorldState((s) => s.windowEndMs)
  const filters = useWorldState((s) => s.filters)
  const setCount = useWorldState((s) => s.setCount)
  const setFilterOptions = useWorldState((s) => s.setFilterOptions)
  const openDrawer = useWorldState((s) => s.openDrawer)
  const readScope = useWorldState((s) => s.readScope)
  const select = useSelection((s) => s.select)

  // Map-mode layers (not in the world store — specific to the WebGL renderer).
  const [choropleth, setChoropleth] = useState(true)
  const [density, setDensity] = useState(false)

  const mapContainerRef = useRef<HTMLDivElement | null>(null)
  const mapRef = useRef<maplibregl.Map | null>(null)
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

  // Publish source/country dropdown options + counts for the reused LayerPanel.
  useEffect(() => {
    const sources = new Set<string>()
    const countries = new Set<string>()
    for (const s of signals) {
      if (
        s.ts >= windowStartMs &&
        s.ts <= windowEndMs &&
        SEVERITY_RANK[s.severity] >= minRank
      ) {
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

  // Aggregate signals to per-coordinate clusters (same keying as Leaflet).
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
        m.set(key, { lat: s.lat, lon: s.lon, count: 1, maxSeverity: s.severity, isEvidence: ev })
      }
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
        },
      })),
    }
  }, [winSignals, evidenceIds])

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

  // Choropleth fill-color expression: match ISO_A2 → confidence-band colour.
  const choroExpr = useMemo(() => {
    const pairs: string[] = []
    for (const [iso2, cv] of verdicts) pairs.push(iso2, CONFIDENCE_FILL[cv.verdict.confidence])
    return pairs
  }, [verdicts])

  // A counter bumped on style `load` so the data/paint sync effects run once the
  // sources/layers exist (they no-op before then).
  const [styleReady, setStyleReady] = useState(0)
  // Latest verdicts for the imperative hover handler (closes over a ref).
  const verdictsRef = useRef(verdicts)
  verdictsRef.current = verdicts

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
      const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 8 })
      popupRef.current = popup

      map.on('load', () => {
        map.addSource(SRC_COUNTRIES, { type: 'geojson', data: WORLD_GEOJSON_PATH })
        map.addSource(SRC_SIGNALS, { type: 'geojson', data: { type: 'FeatureCollection', features: [] } })
        map.addSource(SRC_FINDINGS, { type: 'geojson', data: { type: 'FeatureCollection', features: [] } })
        map.addSource(SRC_SITUATIONS, { type: 'geojson', data: { type: 'FeatureCollection', features: [] } })

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
            // Density weighted by each cluster's aggregated signal count.
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

        // Click → shared selection + drill drawer.
        map.on('click', L_FINDINGS, (e) => {
          const p = e.features?.[0]?.properties
          if (!p) return
          openDrawer({ title: String(p.title ?? 'finding'), signals: [], findings: [] })
          select({ kind: 'finding', id: String(p.id), label: String(p.title ?? '') })
        })
        map.on('click', L_SITUATIONS, (e) => {
          const p = e.features?.[0]?.properties
          if (!p) return
          openDrawer({ title: String(p.title ?? 'situation'), signals: [], findings: [] })
          select({ kind: 'situation', id: String(p.id), label: String(p.title ?? '') })
        })
        for (const id of [L_FINDINGS, L_SITUATIONS, L_SIGNALS, L_CHORO_FILL]) {
          map.on('mouseenter', id, () => (map.getCanvas().style.cursor = 'pointer'))
          map.on('mouseleave', id, () => {
            map.getCanvas().style.cursor = ''
            if (id === L_CHORO_FILL) popup.remove()
          })
        }
        // Choropleth hover → country name + verdict band.
        map.on('mousemove', L_CHORO_FILL, (e) => {
          const f = e.features?.[0]
          const iso2 = f?.properties?.ISO_A2 as string | undefined
          if (!iso2) return
          const cv = verdictsRef.current.get(iso2)
          if (!cv) {
            popup.remove()
            return
          }
          popup
            .setLngLat(e.lngLat)
            .setHTML(
              `<div style="font:12px system-ui;color:#e2e8f0"><b>${countryLabel(iso2)}</b><br/>` +
                `confidence: ${cv.verdict.confidence}` +
                (cv.verdict.faithfulness != null
                  ? ` · faithful ${Math.round(cv.verdict.faithfulness * 100)}%`
                  : '') +
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
      mapRef.current?.remove()
      mapRef.current = null
      loadedRef.current = false
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps -- init once

  const setData = (srcId: string, fc: FC) => {
    const map = mapRef.current
    if (!map || !loadedRef.current) return
    const src = map.getSource(srcId) as maplibregl.GeoJSONSource | undefined
    src?.setData(fc)
  }

  // The heatmap and signal-cluster layers share SRC_SIGNALS, so one setData
  // keeps both fed.
  useEffect(() => setData(SRC_SIGNALS, signalFC), [signalFC, styleReady])
  useEffect(() => setData(SRC_FINDINGS, findingFC), [findingFC, styleReady])
  useEffect(() => setData(SRC_SITUATIONS, situationFC), [situationFC, styleReady])

  // Choropleth colour.
  useEffect(() => {
    const map = mapRef.current
    if (!map || !loadedRef.current) return
    const expr =
      choroExpr.length > 0
        ? ['match', ['get', 'ISO_A2'], ...choroExpr, LAND_UNASSESSED]
        : LAND_UNASSESSED
    // maplibre's setPaintProperty value is loosely typed; the expression array is
    // a valid data-driven fill-color. Cast keeps tsc happy across type versions.
    map.setPaintProperty(L_CHORO_FILL, 'fill-color', expr as never)
  }, [choroExpr, styleReady])

  // Layer visibility toggles.
  useEffect(() => {
    const map = mapRef.current
    if (!map || !loadedRef.current) return
    const vis = (id: string, on: boolean) => map.setLayoutProperty(id, 'visibility', on ? 'visible' : 'none')
    vis(L_CHORO_FILL, choropleth)
    vis(L_CHORO_LINE, choropleth)
    vis(L_HEAT, density)
    vis(L_SIGNALS, layers.signals)
    vis(L_FINDINGS, layers.findings)
    vis(L_SITUATIONS, layers.situations)
  }, [choropleth, density, layers.signals, layers.findings, layers.situations, styleReady])

  return (
    <div className="relative h-full w-full" style={{ background: '#0a0c10' }} data-testid="maplibre-world">
      <div ref={mapContainerRef} className="h-full w-full" />
      <MapModeControl
        choropleth={choropleth}
        density={density}
        onToggleChoropleth={() => setChoropleth((v) => !v)}
        onToggleDensity={() => setDensity((v) => !v)}
      />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Map-mode control — the choropleth / density toggles + the choropleth legend,
// bottom-left. Collapsed to a single icon button by default (LAYERS-collapsed
// per the UI direction); the data-layer toggles live in the reused LayerPanel.
// ---------------------------------------------------------------------------
function MapModeControl({
  choropleth,
  density,
  onToggleChoropleth,
  onToggleDensity,
}: {
  choropleth: boolean
  density: boolean
  onToggleChoropleth: () => void
  onToggleDensity: () => void
}) {
  const [open, setOpen] = useState(false)
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
        <div className="w-[190px] rounded-lg border border-slate-800 bg-surface-200/95 p-2 shadow-lg backdrop-blur-sm">
          <div className="mb-1 flex items-center justify-between">
            <span className="text-[10px] font-semibold uppercase tracking-wide text-slate-500">Map layers</span>
            <button type="button" onClick={() => setOpen(false)} className="text-[10px] text-slate-400 hover:text-slate-200">
              hide
            </button>
          </div>
          <label className="flex cursor-pointer items-center gap-2 py-0.5 text-xs text-slate-300">
            <input type="checkbox" checked={choropleth} onChange={onToggleChoropleth} className="h-3.5 w-3.5 accent-accent-info" />
            Verdict choropleth
          </label>
          <label className="flex cursor-pointer items-center gap-2 py-0.5 text-xs text-slate-300">
            <input type="checkbox" checked={density} onChange={onToggleDensity} className="h-3.5 w-3.5 accent-accent-info" />
            Signal density
          </label>
          {choropleth && (
            <ul className="mt-1.5 space-y-0.5 border-t border-slate-800 pt-1.5">
              {CHOROPLETH_LEGEND.map((b) => (
                <li key={b.level} className="flex items-center gap-2 text-[11px] text-slate-400">
                  <span aria-hidden className="h-2.5 w-2.5 rounded-sm" style={{ backgroundColor: CONFIDENCE_FILL[b.level] }} />
                  {b.label}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
