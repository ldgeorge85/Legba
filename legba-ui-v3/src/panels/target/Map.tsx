/**
 * T7. Target Map (`target.map`) — UI-3 (Tier B) real geo overlay.
 *
 * Per-target geo distribution of signals AND analyst-output findings.
 * Pulls 500 signals + 200 findings, extracts the geocode-filter populated
 * `data.geo.{lat,lon}` payload (see `legba.data.filters.geocode`), and
 * plots them on a clustered MapLibre layer.
 *
 * UI-3 additions over the prior pass:
 *  - **Analyst-output overlay**: finding markers are colored by severity
 *    (critical/high/medium/low) — the per-target analyst-output overlay the
 *    v2 design scored Map a keep-4 for.
 *  - **Layer toggle**: show/hide signals vs findings independently.
 *  - **Per-country breakdown**: a count box (signals/findings by country)
 *    derived from `data.geo.country_iso2` (see `countByCountry`).
 *  - **Provenance-on-hover**: a MapLibre popup on each unclustered marker
 *    carrying kind + source_id (signal) / severity (finding) + country.
 *  - Markers carry `geo` extracted either from the finding's own
 *    `data.geo` or (fallback) an upstream signal's geo — findings inherit
 *    both the geo AND the source_id of that upstream signal.
 *
 * Click a marker → dispatches `legba:open-lineage` with the row id so the
 * Lineage panel picks it up (cross-panel pattern).
 *
 * Free OSM-style tile source: `https://demotiles.maplibre.org/style.json`
 * (the maplibre reference style; no API key required).
 */

import maplibregl from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet } from '@/lib/api'
import { useMapResize } from '@/lib/useMapResize'
import type { PanelProps } from '@/types'
import { selectRow, useSelection } from '@/state/selection'
import {
  buildEntityGeoPoints,
  buildGeoPoints,
  countByCountry,
  type GeoEntity,
  type GeoPoint,
} from '@/lib/geoPoints'

interface SignalRow {
  id: string
  title: string
  category: string
  source_id: string | null
  data: Record<string, unknown>
  produced_at: string
}

interface FindingRow {
  id: string
  title: string
  severity: string | null
  source_id?: string | null
  data?: Record<string, unknown> | null
  derived_from: string[]
  produced_at: string
}

interface SignalsResponse {
  data: SignalRow[]
  next_cursor: string | null
}

interface FindingsResponse {
  data: FindingRow[]
  next_cursor: string | null
}

interface EntitiesResponse {
  data: GeoEntity[]
  total: number
}

const MAP_STYLE = 'https://demotiles.maplibre.org/style.json'
const SOURCE_ID = 'legba-points'
const CLUSTER_LAYER = 'legba-clusters'
const CLUSTER_COUNT_LAYER = 'legba-cluster-count'
const POINT_LAYER = 'legba-points-unclustered'

/** Severity → marker color (analyst-output overlay). */
const SEVERITY_COLOR: Record<string, string> = {
  critical: '#ef4444', // accent.critical
  high: '#f59e0b', // accent.warning
  medium: '#3b82f6', // accent.info
  low: '#10b981', // accent.ok
}
const SIGNAL_COLOR = '#60a5fa' // blue-400
const FINDING_DEFAULT = '#fbbf24' // amber-400
const ENTITY_COLOR = '#10b981' // emerald-500 (location-class entities)

function openLineage(kind: 'signal' | 'finding', id: string, title: string) {
  // Redesign Move 2: unified selection store → opens the Inspector + brushes
  // every room (was a legacy window event firing into the void).
  selectRow(kind, id, title, { origin: 'target-map' })
}

export default function TargetMapPanel({ registration, scope }: PanelProps) {
  const target_id = scope.target_id ?? registration.descriptor_id
  const [showSignals, setShowSignals] = useState(true)
  const [showFindings, setShowFindings] = useState(true)
  // Entity-geo overlay: country mentions the NER pipeline left geo-less, placed
  // via the lib/countryGeo gazetteer. Off by default — opt-in overlay.
  const [showEntities, setShowEntities] = useState(false)

  const signalsQ = useQuery<SignalsResponse>({
    enabled: !!target_id,
    queryKey: ['target-map-signals', target_id],
    queryFn: () =>
      apiGet<SignalsResponse>(
        `/signals?target_id=${encodeURIComponent(target_id)}&limit=500`,
      ),
    refetchInterval: 60_000,
  })

  const findingsQ = useQuery<FindingsResponse>({
    enabled: !!target_id,
    queryKey: ['target-map-findings', target_id],
    queryFn: () =>
      apiGet<FindingsResponse>(
        `/findings?target_id=${encodeURIComponent(target_id)}&limit=200`,
      ),
    refetchInterval: 60_000,
  })

  // Entity-geo overlay (gazetteer-resolved country mentions). Only fetched when
  // the toggle is on; geo-resolution + country-name fallback live in geoPoints.
  const entitiesQ = useQuery<EntitiesResponse>({
    enabled: showEntities,
    queryKey: ['target-map-entities'],
    queryFn: () => apiGet<EntitiesResponse>('/entities?limit=200'),
    refetchInterval: 120_000,
  })

  const allPoints: GeoPoint[] = useMemo(() => {
    const base = buildGeoPoints(signalsQ.data?.data ?? [], findingsQ.data?.data ?? [])
    const ents = buildEntityGeoPoints(entitiesQ.data?.data ?? [])
    return [...base, ...ents]
  }, [signalsQ.data, findingsQ.data, entitiesQ.data])

  // Apply the layer toggles.
  const points = useMemo(
    () =>
      allPoints.filter((p) =>
        p.kind === 'signal' ? showSignals : p.kind === 'finding' ? showFindings : showEntities,
      ),
    [allPoints, showSignals, showFindings, showEntities],
  )

  // Per-country breakdown for the count box (honours the layer toggles).
  const countries = useMemo(() => countByCountry(points), [points])

  const mapContainerRef = useRef<HTMLDivElement | null>(null)
  const mapRef = useRef<maplibregl.Map | null>(null)
  const popupRef = useRef<maplibregl.Popup | null>(null)

  // Fill the tile even when it initialises at 0 height in a Dockview/flex
  // container — THE blank-map root cause (UI_V4_PLAN W0.7).
  useMapResize(mapContainerRef, () => mapRef.current)

  // Initialise the map once on mount.
  useEffect(() => {
    if (!mapContainerRef.current || mapRef.current) return
    const map = new maplibregl.Map({
      container: mapContainerRef.current,
      style: MAP_STYLE,
      center: [0, 20],
      zoom: 1.2,
      attributionControl: { compact: true },
    })
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right')

    // Provenance-on-hover popup (one reused instance, no marker pin).
    const popup = new maplibregl.Popup({
      closeButton: false,
      closeOnClick: false,
      offset: 8,
      className: 'legba-map-popup',
    })
    popupRef.current = popup

    map.on('load', () => {
      map.addSource(SOURCE_ID, {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
        cluster: true,
        clusterMaxZoom: 12,
        clusterRadius: 40,
      })
      map.addLayer({
        id: CLUSTER_LAYER,
        type: 'circle',
        source: SOURCE_ID,
        filter: ['has', 'point_count'],
        paint: {
          'circle-color': [
            'step',
            ['get', 'point_count'],
            '#34d399',
            10,
            '#fbbf24',
            50,
            '#fb7185',
          ],
          'circle-radius': ['step', ['get', 'point_count'], 14, 10, 18, 50, 24],
          'circle-opacity': 0.85,
        },
      })
      map.addLayer({
        id: CLUSTER_COUNT_LAYER,
        type: 'symbol',
        source: SOURCE_ID,
        filter: ['has', 'point_count'],
        layout: { 'text-field': ['get', 'point_count_abbreviated'], 'text-size': 11 },
        paint: { 'text-color': '#0f172a' },
      })
      map.addLayer({
        id: POINT_LAYER,
        type: 'circle',
        source: SOURCE_ID,
        filter: ['!', ['has', 'point_count']],
        paint: {
          // Per-feature color drives the analyst-output severity overlay.
          'circle-color': ['get', 'color'],
          'circle-radius': ['case', ['==', ['get', 'kind'], 'finding'], 6, 5],
          'circle-stroke-width': 1,
          'circle-stroke-color': '#0f172a',
        },
      })

      map.on('click', CLUSTER_LAYER, (e) => {
        const feature = e.features?.[0]
        if (!feature) return
        const clusterId = feature.properties?.cluster_id as number | undefined
        if (clusterId === undefined) return
        const source = map.getSource(SOURCE_ID) as maplibregl.GeoJSONSource
        source
          .getClusterExpansionZoom(clusterId)
          .then((zoom) => {
            const geom = feature.geometry
            if (geom.type !== 'Point') return
            map.easeTo({ center: geom.coordinates as [number, number], zoom })
          })
          .catch(() => undefined)
      })

      map.on('click', POINT_LAYER, (e) => {
        const feature = e.features?.[0]
        if (!feature) return
        const props = feature.properties ?? {}
        const id = String(props.id ?? '')
        const title = String(props.title ?? '(no title)')
        // Entity markers hand off to the entity graph (centered on the entity);
        // signals/findings hand off to the provenance lineage panel.
        if (props.kind === 'entity') {
          // Redesign Move 2: select the entity (brushes the entity graph +
          // opens the Inspector) instead of a legacy window event.
          useSelection.getState().select({ kind: 'entity', id: title, label: title, origin: 'target-map' })
          return
        }
        const kind = (props.kind === 'finding' ? 'finding' : 'signal') as
          | 'signal'
          | 'finding'
        if (id) openLineage(kind, id, title)
      })

      // Provenance-on-hover: kind + source_id/severity + country.
      map.on('mousemove', POINT_LAYER, (e) => {
        const feature = e.features?.[0]
        if (!feature || feature.geometry.type !== 'Point') {
          popup.remove()
          return
        }
        const props = feature.properties ?? {}
        popup
          .setLngLat(feature.geometry.coordinates as [number, number])
          .setHTML(popupHtml(props))
          .addTo(map)
      })
      map.on('mouseleave', POINT_LAYER, () => popup.remove())

      for (const layer of [POINT_LAYER, CLUSTER_LAYER]) {
        map.on('mouseenter', layer, () => {
          map.getCanvas().style.cursor = 'pointer'
        })
        map.on('mouseleave', layer, () => {
          map.getCanvas().style.cursor = ''
        })
      }
    })

    mapRef.current = map
    return () => {
      popupRef.current?.remove()
      popupRef.current = null
      map.remove()
      mapRef.current = null
    }
  }, [])

  // Push points into the map source whenever they change.
  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    const apply = () => {
      const source = map.getSource(SOURCE_ID) as maplibregl.GeoJSONSource | undefined
      if (!source) return
      source.setData({
        type: 'FeatureCollection',
        features: points.map((p) => ({
          type: 'Feature',
          properties: {
            id: p.id,
            title: p.title,
            kind: p.kind,
            color: markerColor(p),
            // Provenance-on-hover payload (strings only — geojson props).
            source_id: p.source_id ?? '',
            severity: p.severity ?? '',
            country: p.country ?? '',
          },
          geometry: { type: 'Point', coordinates: [p.lon, p.lat] },
        })),
      })
      if (points.length > 0) {
        const bounds = new maplibregl.LngLatBounds()
        for (const p of points) bounds.extend([p.lon, p.lat])
        map.fitBounds(bounds, { padding: 40, maxZoom: 8, duration: 0 })
      }
    }
    if (map.isStyleLoaded()) apply()
    else map.once('load', apply)
  }, [points])

  const geoCount = points.length
  const totalSignals = signalsQ.data?.data.length ?? 0
  const totalFindings = findingsQ.data?.data.length ?? 0

  return (
    <PanelChrome
      registration={registration}
      subtitle={`${geoCount} geocoded · ${totalSignals} signals · ${totalFindings} findings · target ${target_id}`}
      actions={
        <div className="flex items-center gap-2 text-[10px]">
          <label className="inline-flex items-center gap-1 text-slate-400">
            <input
              type="checkbox"
              checked={showSignals}
              onChange={(e) => setShowSignals(e.target.checked)}
              data-testid="target-map-toggle-signals"
            />
            signals
          </label>
          <label className="inline-flex items-center gap-1 text-slate-400">
            <input
              type="checkbox"
              checked={showFindings}
              onChange={(e) => setShowFindings(e.target.checked)}
              data-testid="target-map-toggle-findings"
            />
            findings
          </label>
          <label className="inline-flex items-center gap-1 text-slate-400" title="country entities the NER pipeline left geo-less, placed via the gazetteer">
            <input
              type="checkbox"
              checked={showEntities}
              onChange={(e) => setShowEntities(e.target.checked)}
              data-testid="target-map-toggle-entities"
            />
            entities
          </label>
        </div>
      }
      onRefresh={() => {
        signalsQ.refetch()
        findingsQ.refetch()
        if (showEntities) entitiesQ.refetch()
      }}
    >
      <div className="relative flex-1 h-full w-full min-h-[300px]">
        <div
          ref={mapContainerRef}
          className="absolute inset-0 rounded border border-slate-800"
          data-testid="target-map-canvas"
        />
        {/* Severity legend for the analyst-output overlay. */}
        <div className="absolute bottom-2 left-2 z-10 bg-surface-100/90 border border-slate-700 rounded px-2 py-1 text-[10px] text-slate-400 flex flex-col gap-0.5">
          <span className="flex items-center gap-1">
            <Dot color={SIGNAL_COLOR} /> signal
          </span>
          <span className="flex items-center gap-1">
            <Dot color={SEVERITY_COLOR.critical} /> finding (sev color)
          </span>
          {showEntities && (
            <span className="flex items-center gap-1">
              <Dot color={ENTITY_COLOR} /> entity (country)
            </span>
          )}
        </div>

        {/* Per-country count breakdown box. */}
        {countries.length > 0 && (
          <div
            className="absolute top-2 right-12 z-10 bg-surface-100/90 border border-slate-700 rounded text-[10px] text-slate-300 max-h-[60%] overflow-auto w-[168px]"
            data-testid="target-map-country-breakdown"
          >
            <div className="px-2 py-1 border-b border-slate-700 text-slate-400 uppercase tracking-wider sticky top-0 bg-surface-100/95">
              by country
            </div>
            {countries.slice(0, 24).map((c) => (
              <div
                key={c.iso2}
                className="flex items-center justify-between gap-2 px-2 py-0.5 hover:bg-surface-200/60"
                title={`${c.name}: ${c.signals} signals · ${c.findings} findings`}
              >
                <span className="truncate text-slate-300">{c.name}</span>
                <span className="font-mono tabular-nums text-slate-400 shrink-0">
                  <span style={{ color: SIGNAL_COLOR }}>{c.signals}</span>
                  <span className="text-slate-600">/</span>
                  <span style={{ color: FINDING_DEFAULT }}>{c.findings}</span>
                </span>
              </div>
            ))}
            {countries.length > 24 && (
              <div className="px-2 py-0.5 text-slate-500">+{countries.length - 24} more…</div>
            )}
          </div>
        )}
        {geoCount === 0 && !signalsQ.isLoading && !findingsQ.isLoading && (
          <div className="absolute top-2 left-2 z-10 bg-surface-100/90 border border-slate-700 rounded px-2 py-1 text-[11px] text-slate-400">
            no geocoded {!showSignals && !showFindings ? 'layers enabled' : 'rows — geocode filter may not be wired on this descriptor'}
          </div>
        )}
      </div>
    </PanelChrome>
  )
}

/** Marker color — signals blue; entities emerald; findings by severity. */
function markerColor(p: GeoPoint): string {
  if (p.kind === 'signal') return SIGNAL_COLOR
  if (p.kind === 'entity') return ENTITY_COLOR
  if (p.severity && SEVERITY_COLOR[p.severity]) return SEVERITY_COLOR[p.severity]
  return FINDING_DEFAULT
}

/** Minimal HTML escape for the popup (props are operator/source data). */
function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/**
 * Provenance-on-hover popup body: kind + (source_id for signals /
 * severity for findings) + country. Reads the stringified geojson props.
 */
function popupHtml(props: Record<string, unknown>): string {
  const kind = String(props.kind ?? 'signal')
  const title = String(props.title ?? '(no title)')
  const sourceId = String(props.source_id ?? '')
  const severity = String(props.severity ?? '')
  const country = String(props.country ?? '')
  const rows: string[] = []
  if (kind === 'finding' && severity) {
    rows.push(`<div>severity: <b>${escapeHtml(severity)}</b></div>`)
  }
  if (sourceId) rows.push(`<div>source: ${escapeHtml(sourceId)}</div>`)
  if (country) rows.push(`<div>country: ${escapeHtml(country)}</div>`)
  return [
    `<div style="font-size:10px;line-height:1.4;max-width:240px">`,
    `<div style="text-transform:uppercase;letter-spacing:0.05em;color:#94a3b8">${escapeHtml(kind)}</div>`,
    `<div style="font-weight:600;color:#e2e8f0">${escapeHtml(title)}</div>`,
    ...rows.map((r) => `<div style="color:#94a3b8">${r}</div>`),
    `</div>`,
  ].join('')
}

function Dot({ color }: { color: string }) {
  return <span className="w-2 h-2 rounded-full inline-block" style={{ background: color }} />
}
