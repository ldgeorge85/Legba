/**
 * The World — map renderer on Leaflet (NOT WebGL).
 *
 * MapLibre/deck.gl render via WebGL, which Chrome refuses to composite inside a
 * Dockview tile's CSS transform — the canvas paints but stays invisible. Leaflet
 * draws with plain DOM + SVG/Canvas2D, which composites normally in a tile, so
 * this docks like any other panel. Reuses the same typed data hooks (./mapData),
 * the shared world store (./worldState), and the global selection store.
 *
 * Base = the bundled Natural Earth GeoJSON (dark land) over a dark background —
 * no external tile server. Signals are aggregated to their geocoded point
 * (country centroid) and drawn as severity-colored circles; findings + situations
 * as their own markers. Clicking opens the drill drawer + sets the selection.
 */
import { useEffect, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  MapContainer,
  GeoJSON,
  CircleMarker,
  Tooltip,
  useMap,
} from 'react-leaflet'
import type { PathOptions } from 'leaflet'
import 'leaflet/dist/leaflet.css'
import {
  useWorldSignals,
  useWorldFindings,
  useWorldSituations,
} from './mapData'
import { useWorldState } from './worldState'
import { useSelection, selectRow } from '@/state/selection'
import type { GeoPoint } from '@/lib/geoPoints'
import { cn } from '@/lib/cn'
import {
  SEVERITY_COLOR,
  SITUATION_COLOR,
  type Severity,
  type WorldSignal,
} from './types'

const SEVERITY_RANK: Record<Severity, number> = {
  critical: 4,
  high: 3,
  medium: 2,
  low: 1,
  info: 0,
}

// Dark land on a near-black void with faint muted borders, so the
// severity-colored markers dominate (matches the old map's subtle graticule —
// the prior #3a4965 borders read a touch too bright/blue against the markers).
const LAND_STYLE: PathOptions = {
  fillColor: '#1b2433',
  fillOpacity: 1,
  color: '#2a3346',
  weight: 0.5,
}

/**
 * Age-based decay multiplier in [DECAY_FLOOR, 1]. A point at the window end
 * (age 0) renders at full strength; a point at the window start fades toward
 * the floor (kept >0 so the oldest in-window points stay faintly visible
 * rather than vanishing). The curve is squared so recent activity dominates.
 * Returns 1 when decay is off or the window has no span.
 */
const DECAY_FLOOR = 0.18

function decayFactor(
  ts: number,
  startMs: number,
  endMs: number,
  enabled: boolean,
): number {
  if (!enabled) return 1
  const span = endMs - startMs
  if (span <= 0) return 1
  const age = endMs - ts
  const fresh = 1 - Math.min(Math.max(age / span, 0), 1)
  return DECAY_FLOOR + (1 - DECAY_FLOOR) * fresh * fresh
}

interface SignalCluster {
  key: string
  lat: number
  lon: number
  count: number
  maxSeverity: Severity
  /** Freshest signal ts in the cluster — drives its decay (a cluster with
   *  recent activity stays bright even if it also holds older signals). */
  freshestTs: number
  signals: WorldSignal[]
}

/** Keep Leaflet sized to its container — it measures on mount, and a tile that
 *  lays out after mount would otherwise leave the map at the wrong size. */
function ResizeFix() {
  const map = useMap()
  useEffect(() => {
    const el = map.getContainer()
    map.invalidateSize()
    const ro = new ResizeObserver(() => map.invalidateSize())
    ro.observe(el)
    const t = setTimeout(() => map.invalidateSize(), 300)
    return () => {
      ro.disconnect()
      clearTimeout(t)
    }
  }, [map])
  return null
}

/** Severity ramp shown on the map, high→low — mirrors the marker encoding and
 *  the feed's severity colors so the dots read at a glance (matches the old map's
 *  corner legend). Static; reads the same SEVERITY_COLOR source as the markers. */
const LEGEND_ORDER: Severity[] = ['critical', 'high', 'medium', 'low', 'info']

function SeverityLegend() {
  return (
    <div
      className={cn(
        'pointer-events-none absolute bottom-3 right-3 z-10 rounded-lg',
        'border border-slate-800 bg-surface-200/95 px-3 py-2 shadow-lg backdrop-blur-sm',
      )}
    >
      <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-slate-500">
        Severity
      </div>
      <ul className="space-y-1">
        {LEGEND_ORDER.map((sev) => (
          <li key={sev} className="flex items-center gap-2">
            <span
              aria-hidden
              className="h-2.5 w-2.5 shrink-0 rounded-full"
              style={{ backgroundColor: SEVERITY_COLOR[sev] }}
            />
            <span className="text-xs capitalize text-slate-300">{sev}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

export default function LeafletWorldMap() {
  const { signals } = useWorldSignals()
  const { findings } = useWorldFindings()
  const { situations } = useWorldSituations()
  const layers = useWorldState((s) => s.layers)
  const windowStartMs = useWorldState((s) => s.windowStartMs)
  const windowEndMs = useWorldState((s) => s.windowEndMs)
  const filters = useWorldState((s) => s.filters)
  const decay = useWorldState((s) => s.decay)
  const setCount = useWorldState((s) => s.setCount)
  const setFilterOptions = useWorldState((s) => s.setFilterOptions)
  const openDrawer = useWorldState((s) => s.openDrawer)
  const readScope = useWorldState((s) => s.readScope)
  const select = useSelection((s) => s.select)

  // P1-T7 — brush the SAME read: when a country/finding read is being lensed
  // elsewhere, ring the clusters that hold one of its cited-evidence signals.
  // Purely additive — with no active readScope the markers render unchanged.
  const evidenceIds = useMemo(
    () => new Set(readScope?.signalIds ?? []),
    [readScope],
  )

  const base = useQuery({
    queryKey: ['world-geojson'],
    queryFn: () => fetch('/world.geojson').then((r) => r.json()),
    staleTime: Infinity,
  })

  const minRank = filters.minSeverity != null ? SEVERITY_RANK[filters.minSeverity] : 0

  // Window first (the existing client-side cut), then the operator's plot
  // filters. Severity is a floor; source/country are exact-match on whichever
  // dimension a row carries (findings/situations have no sourceId, so the
  // source filter only narrows signals).
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

  // Publish the source/country option lists for the LayerPanel dropdowns,
  // derived from the in-window data (pre source/country filter so the operator
  // can still switch between values). Severity floor still applies.
  const winSignalsForOptions = useMemo(
    () =>
      signals.filter(
        (s) =>
          s.ts >= windowStartMs &&
          s.ts <= windowEndMs &&
          SEVERITY_RANK[s.severity] >= minRank,
      ),
    [signals, windowStartMs, windowEndMs, minRank],
  )
  const filterOptions = useMemo(() => {
    const sources = new Set<string>()
    const countries = new Set<string>()
    for (const s of winSignalsForOptions) {
      if (s.sourceId) sources.add(s.sourceId)
      for (const c of s.countries) countries.add(c)
    }
    for (const f of findings) for (const c of f.countries) countries.add(c)
    for (const s of situations) for (const c of s.countries) countries.add(c)
    return {
      sources: [...sources].sort(),
      countries: [...countries].sort(),
    }
  }, [winSignalsForOptions, findings, situations])
  useEffect(
    () => setFilterOptions(filterOptions),
    [filterOptions, setFilterOptions],
  )

  // Aggregate signals onto their geocoded point so the map shows a clean set of
  // severity-colored circles instead of thousands of overlapping dots.
  const clusters = useMemo<SignalCluster[]>(() => {
    const m = new Map<string, SignalCluster>()
    for (const s of winSignals) {
      const key = `${s.lat.toFixed(2)},${s.lon.toFixed(2)}`
      const c = m.get(key)
      if (c) {
        c.count++
        c.signals.push(s)
        if (s.ts > c.freshestTs) c.freshestTs = s.ts
        if (SEVERITY_RANK[s.severity] > SEVERITY_RANK[c.maxSeverity]) c.maxSeverity = s.severity
      } else {
        m.set(key, {
          key,
          lat: s.lat,
          lon: s.lon,
          count: 1,
          maxSeverity: s.severity,
          freshestTs: s.ts,
          signals: [s],
        })
      }
    }
    return [...m.values()]
  }, [winSignals])

  useEffect(() => setCount('signals', winSignals.length), [winSignals.length, setCount])
  useEffect(() => setCount('findings', winFindings.length), [winFindings.length, setCount])
  useEffect(() => setCount('situations', winSituations.length), [winSituations.length, setCount])

  return (
    <div className="h-full w-full" style={{ background: '#0a0c10' }}>
      <MapContainer
        center={[20, 0]}
        zoom={2}
        minZoom={1}
        maxZoom={8}
        worldCopyJump
        preferCanvas
        attributionControl={false}
        style={{ height: '100%', width: '100%', background: '#0a0c10' }}
      >
        <ResizeFix />
        {base.data ? <GeoJSON data={base.data} style={() => LAND_STYLE} /> : null}

        {layers.signals &&
          clusters.map((c) => {
            const d = decayFactor(c.freshestTs, windowStartMs, windowEndMs, decay)
            // P1-T7 — does this cluster carry one of the active read's cited
            // signals? If so, ring it white so the World map shows the lensed
            // read's evidence in place (no effect when no read is being lensed).
            const isEvidence =
              evidenceIds.size > 0 && c.signals.some((s) => evidenceIds.has(s.id))
            return (
            <CircleMarker
              key={`s-${c.key}`}
              center={[c.lat, c.lon]}
              radius={Math.min(6 + Math.sqrt(c.count) * 2.5, 28) * (0.5 + 0.5 * d)}
              pathOptions={{
                color: isEvidence ? '#ffffff' : SEVERITY_COLOR[c.maxSeverity],
                fillColor: SEVERITY_COLOR[c.maxSeverity],
                fillOpacity: 0.45 * d,
                opacity: isEvidence ? 1 : d,
                weight: isEvidence ? 2.5 : 1,
              }}
              eventHandlers={{
                click: () => {
                  openDrawer({
                    title: `${c.count} signal${c.count === 1 ? '' : 's'}`,
                    signals: c.signals,
                    findings: [],
                  })
                  const src = c.signals[0]?.sourceId
                  if (src) select({ kind: 'source', id: src, label: src })
                },
              }}
            >
              <Tooltip>{c.count} signals</Tooltip>
            </CircleMarker>
            )
          })}

        {layers.findings &&
          winFindings.map((f) => {
            const d = decayFactor(f.ts, windowStartMs, windowEndMs, decay)
            return (
            <CircleMarker
              key={`f-${f.id}`}
              center={[f.lat as number, f.lon as number]}
              radius={6 * (0.5 + 0.5 * d)}
              pathOptions={{
                color: '#ffffff',
                fillColor: SEVERITY_COLOR[f.severity],
                fillOpacity: 0.85 * d,
                opacity: d,
                weight: 1,
              }}
              eventHandlers={{
                click: () => {
                  openDrawer({ title: f.title, signals: [], findings: [f] })
                  select({ kind: 'finding', id: f.id, label: f.title })
                },
              }}
            >
              <Tooltip>{f.title}</Tooltip>
            </CircleMarker>
            )
          })}

        {layers.situations &&
          winSituations.map((s) => {
            const d = decayFactor(s.ts, windowStartMs, windowEndMs, decay)
            return (
            <CircleMarker
              key={`sit-${s.id}`}
              center={[s.lat as number, s.lon as number]}
              radius={10 * (0.5 + 0.5 * d)}
              pathOptions={{
                color: '#0a0c10',
                fillColor: SITUATION_COLOR[s.lifecycle],
                fillOpacity: 0.85 * d,
                opacity: d,
                weight: 2,
              }}
              eventHandlers={{
                click: () => {
                  openDrawer({ title: s.title, signals: [], findings: [] })
                  select({ kind: 'situation', id: s.id, label: s.title })
                },
              }}
            >
              <Tooltip>{s.title}</Tooltip>
            </CircleMarker>
            )
          })}
      </MapContainer>
      <SeverityLegend />
    </div>
  )
}

// ---------------------------------------------------------------------------
// ReadGeoLens (P1-T7) — the GEO half of the read's temporal lens.
//
// A compact, SELF-CONTAINED Leaflet map of one country/finding read's evidence
// geo points (resolved by the lens via `signalGeoPoints`, ISO-2 fallback). It
// does NOT read the World room's time window/filters — the read defines its own
// scope. The directly-cited evidence points are emphasised; the rest of the
// country's activity is faded context. Clicking a point `selectRow`s the
// underlying row, brushing every room (and re-opening it in the Inspector).
// ---------------------------------------------------------------------------

/** Per-kind marker fill for the lens (kept local — the world severity ramp
 *  keys on Severity, but lens points carry a free-form string). */
function lensPointColor(p: GeoPoint): string {
  if (p.kind === 'finding') return '#fbbf24' // amber-400 — analyst output
  if (p.kind === 'entity') return '#34d399' // emerald-400
  return '#60a5fa' // blue-400 — signal
}

export function ReadGeoLens({
  points,
  evidenceIds,
  selectedId,
}: {
  points: GeoPoint[]
  evidenceIds: Set<string>
  selectedId?: string | null
}) {
  const base = useQuery({
    queryKey: ['world-geojson'],
    queryFn: () => fetch('/world.geojson').then((r) => r.json()),
    staleTime: Infinity,
  })

  // Centre on the mean of the evidence points so the read's locus is framed.
  const center = useMemo<[number, number]>(() => {
    if (points.length === 0) return [20, 0]
    let lat = 0
    let lon = 0
    for (const p of points) {
      lat += p.lat
      lon += p.lon
    }
    return [lat / points.length, lon / points.length]
  }, [points])

  if (points.length === 0) {
    return (
      <div
        className="flex h-full w-full items-center justify-center bg-surface-200 px-4 text-center text-sm text-slate-500"
        data-testid="read-geo-lens-empty"
      >
        No geo-placeable evidence for this read.
      </div>
    )
  }

  return (
    <div className="h-full w-full" style={{ background: '#0a0c10' }} data-testid="read-geo-lens">
      <MapContainer
        center={center}
        zoom={3}
        minZoom={1}
        maxZoom={8}
        worldCopyJump
        preferCanvas
        attributionControl={false}
        style={{ height: '100%', width: '100%', background: '#0a0c10' }}
      >
        <ResizeFix />
        {base.data ? <GeoJSON data={base.data} style={() => LAND_STYLE} /> : null}
        {points.map((p) => {
          const isEvidence = evidenceIds.has(p.id)
          const isSelected = selectedId != null && p.id === selectedId
          const color = lensPointColor(p)
          return (
            <CircleMarker
              key={`${p.kind}-${p.id}`}
              center={[p.lat, p.lon]}
              radius={isEvidence ? 8 : 5}
              pathOptions={{
                color: isSelected ? '#ffffff' : color,
                fillColor: color,
                fillOpacity: isEvidence ? 0.85 : 0.3,
                opacity: isEvidence ? 1 : 0.45,
                weight: isSelected ? 3 : isEvidence ? 1.5 : 1,
              }}
              eventHandlers={{
                click: () => selectRow(p.kind, p.id, p.title, { origin: 'read-geo-lens' }),
              }}
            >
              <Tooltip>{p.title}</Tooltip>
            </CircleMarker>
          )
        })}
      </MapContainer>
    </div>
  )
}
