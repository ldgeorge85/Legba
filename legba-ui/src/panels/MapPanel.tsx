import { useRef, useEffect, useCallback, useState, useMemo } from 'react'
import maplibregl from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import { useGeoData, useEventGeoData, useSignalGeoData } from '@/api/hooks'
import { useSelectionStore } from '@/stores/selection'
import { useWorkspaceStore } from '@/stores/workspace'
import type { GeoNode } from '@/api/types'

/** Map marker colors by entity type -- consistent with entityTypeColor in utils */
const TYPE_COLORS: Record<string, string> = {
  person: '#3b82f6',       // blue-500
  organization: '#a855f7', // purple-500
  location: '#22c55e',     // green-500
  country: '#10b981',      // emerald-500
  event: '#f59e0b',        // amber-500
  concept: '#06b6d4',      // cyan-500
  weapon: '#ef4444',       // red-500
  military_unit: '#f43f5e',// rose-500
  infrastructure: '#64748b',// slate-500
}

const DEFAULT_COLOR = '#6b7280' // gray-500

type MapMode = 'entities' | 'events' | 'signals'

/** Severity color mapping for derived events */
const SEVERITY_COLORS: Record<string, string> = {
  critical: '#ef4444', // red-500
  high: '#f97316',     // orange-500
  medium: '#eab308',   // yellow-500
  low: '#3b82f6',      // blue-500
}

function colorForType(type: string): string {
  return TYPE_COLORS[type.toLowerCase()] ?? DEFAULT_COLOR
}

/** Dark raster style using CartoDB Dark Matter tiles */
const MAP_STYLE: maplibregl.StyleSpecification = {
  version: 8,
  glyphs: 'https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf',
  sources: {
    'carto-dark': {
      type: 'raster',
      tiles: [
        'https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png',
        'https://b.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png',
        'https://c.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png',
      ],
      tileSize: 256,
      attribution: '&copy; <a href="https://carto.com/">CARTO</a> &copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>',
    },
  },
  layers: [
    {
      id: 'carto-dark-layer',
      type: 'raster',
      source: 'carto-dark',
      minzoom: 0,
      maxzoom: 19,
    },
  ],
}

/** Build a GeoJSON FeatureCollection from geo nodes */
function nodesToGeoJSON(nodes: GeoNode[]): GeoJSON.FeatureCollection {
  return {
    type: 'FeatureCollection',
    features: nodes.map((n) => ({
      type: 'Feature' as const,
      geometry: {
        type: 'Point' as const,
        coordinates: [n.lon, n.lat],
      },
      properties: {
        id: n.id,
        entity_id: n.entity_id,
        label: n.label,
        entityType: n.type,
        color: colorForType(n.type),
      },
    })),
  }
}

/** Build a unique set of [type, color] pairs for the legend */
function legendEntries(nodes: GeoNode[]): Array<{ type: string; color: string }> {
  const seen = new Set<string>()
  const entries: Array<{ type: string; color: string }> = []
  for (const n of nodes) {
    const t = n.type.toLowerCase()
    if (!seen.has(t)) {
      seen.add(t)
      entries.push({ type: t, color: colorForType(t) })
    }
  }
  return entries.sort((a, b) => a.type.localeCompare(b.type))
}

/** IDs of all layers by mode (for toggling visibility) */
const ENTITY_LAYER_IDS = ['cluster-circles', 'cluster-count', 'entity-circles', 'entity-labels']
const EVENT_LAYER_IDS = ['event-circles', 'event-labels']
const SIGNAL_LAYER_IDS = ['signal-heat', 'signal-heat-points', 'signal-click-circles']

export function MapPanel() {
  const containerRef = useRef<HTMLDivElement>(null)
  const mapRef = useRef<maplibregl.Map | null>(null)
  const popupRef = useRef<maplibregl.Popup | null>(null)
  const [mode, setMode] = useState<MapMode>('entities')
  const [mapReady, setMapReady] = useState(false)
  const [categoryFilter, setCategoryFilter] = useState<Set<string>>(new Set())

  const { data, isLoading } = useGeoData()
  const { data: eventGeo } = useEventGeoData()
  const { data: signalGeo } = useSignalGeoData()
  const selected = useSelectionStore((s) => s.selected)
  const select = useSelectionStore((s) => s.select)
  const timeWindow = useSelectionStore((s) => s.timeWindow)
  const setTimeWindow = useSelectionStore((s) => s.setTimeWindow)
  const openPanel = useWorkspaceStore((s) => s.openPanel)

  const handleMarkerClick = useCallback(
    (id: string, label: string) => {
      select({ type: 'entity', id, name: label })
      openPanel('entity-detail', { id })
    },
    [select, openPanel],
  )

  // Initialize map
  useEffect(() => {
    if (!containerRef.current) return

    const map = new maplibregl.Map({
      container: containerRef.current,
      style: MAP_STYLE,
      center: [30, 30], // Middle East focus
      zoom: 2.5,
      attributionControl: false,
    })

    map.addControl(new maplibregl.NavigationControl(), 'top-right')

    mapRef.current = map

    // Create a reusable popup for hover
    popupRef.current = new maplibregl.Popup({
      closeButton: false,
      closeOnClick: false,
      offset: 12,
      className: 'legba-map-popup',
    })

    map.on('load', () => {
      // ── Entity source with clustering ──
      map.addSource('entities', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
        cluster: true,
        clusterMaxZoom: 14,
        clusterRadius: 50,
      })

      // Cluster circle layer -- sized and colored by point_count
      map.addLayer({
        id: 'cluster-circles',
        type: 'circle',
        source: 'entities',
        filter: ['has', 'point_count'],
        paint: {
          'circle-radius': [
            'step', ['get', 'point_count'],
            15,   // default radius
            10, 20,  // >= 10 points
            50, 25,  // >= 50 points
            100, 30, // >= 100 points
          ],
          'circle-color': [
            'step', ['get', 'point_count'],
            '#3b82f6',  // blue-500 (small clusters)
            10, '#8b5cf6', // violet-500
            50, '#f59e0b', // amber-500
            100, '#ef4444', // red-500 (large clusters)
          ],
          'circle-opacity': 0.75,
          'circle-stroke-color': '#ffffff',
          'circle-stroke-width': 2,
          'circle-stroke-opacity': 0.4,
        },
      })

      // Cluster count label
      map.addLayer({
        id: 'cluster-count',
        type: 'symbol',
        source: 'entities',
        filter: ['has', 'point_count'],
        layout: {
          'text-field': ['get', 'point_count_abbreviated'],
          'text-font': ['Open Sans Semibold'],
          'text-size': 12,
          'text-allow-overlap': true,
        },
        paint: {
          'text-color': '#ffffff',
        },
      })

      // Individual (unclustered) entity circles
      map.addLayer({
        id: 'entity-circles',
        type: 'circle',
        source: 'entities',
        filter: ['!', ['has', 'point_count']],
        paint: {
          'circle-radius': [
            'interpolate', ['linear'], ['zoom'],
            2, 5,
            6, 7,
            10, 10,
          ],
          'circle-color': ['get', 'color'],
          'circle-opacity': 0.85,
          'circle-stroke-color': '#ffffff',
          'circle-stroke-width': 1.5,
          'circle-stroke-opacity': 0.6,
        },
      })

      // Label layer (only for unclustered points)
      map.addLayer({
        id: 'entity-labels',
        type: 'symbol',
        source: 'entities',
        filter: ['!', ['has', 'point_count']],
        layout: {
          'text-field': ['get', 'label'],
          'text-size': 11,
          'text-offset': [0, 1.4],
          'text-anchor': 'top',
          'text-max-width': 12,
          'text-allow-overlap': false,
        },
        paint: {
          'text-color': '#e2e8f0',
          'text-halo-color': '#0f172a',
          'text-halo-width': 1.5,
        },
        minzoom: 4,
      })

      // ── Signals heatmap source (high-volume raw signals) ──
      map.addSource('signals-geo', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      })

      // Signal heatmap layer
      map.addLayer({
        id: 'signal-heat',
        type: 'heatmap',
        source: 'signals-geo',
        paint: {
          'heatmap-weight': ['interpolate', ['linear'], ['get', 'confidence'], 0, 0.3, 1, 1],
          'heatmap-intensity': ['interpolate', ['linear'], ['zoom'], 0, 1, 9, 3],
          'heatmap-color': [
            'interpolate', ['linear'], ['heatmap-density'],
            0, 'rgba(0,0,0,0)',
            0.2, '#2563eb',
            0.4, '#7c3aed',
            0.6, '#dc2626',
            0.8, '#f59e0b',
            1, '#ffffff',
          ],
          'heatmap-radius': ['interpolate', ['linear'], ['zoom'], 0, 4, 9, 30],
          'heatmap-opacity': 0.7,
        },
      })

      // Signal point layer (shows at higher zoom when heatmap fades)
      map.addLayer({
        id: 'signal-heat-points',
        type: 'circle',
        source: 'signals-geo',
        minzoom: 8,
        paint: {
          'circle-radius': ['interpolate', ['linear'], ['zoom'], 8, 3, 14, 6],
          'circle-color': '#f59e0b',
          'circle-opacity': 0.6,
          'circle-stroke-color': '#ffffff',
          'circle-stroke-width': 0.5,
        },
      })

      // Clickable signal circles (on top of heatmap)
      map.addLayer({
        id: 'signal-click-circles',
        type: 'circle',
        source: 'signals-geo',
        minzoom: 5,
        paint: {
          'circle-radius': ['interpolate', ['linear'], ['zoom'], 5, 3, 10, 5, 14, 8],
          'circle-color': '#f59e0b',
          'circle-opacity': 0.8,
          'circle-stroke-color': '#ffffff',
          'circle-stroke-width': 1,
        },
      })

      // ── Derived event source (lower volume, severity-coded) ──
      map.addSource('events-geo', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      })

      // Event circles -- sized by signal_count, colored by severity, always on top
      map.addLayer({
        id: 'event-circles',
        type: 'circle',
        source: 'events-geo',
        paint: {
          'circle-radius': [
            'interpolate', ['linear'],
            ['coalesce', ['get', 'signal_count'], 1],
            1, 7,
            5, 10,
            10, 14,
            25, 18,
            50, 24,
          ],
          'circle-color': [
            'match', ['get', 'severity'],
            'critical', '#ef4444',
            'high', '#f97316',
            'medium', '#eab308',
            'low', '#3b82f6',
            '#6b7280',
          ],
          'circle-opacity': 0.9,
          'circle-stroke-color': '#ffffff',
          'circle-stroke-width': 2,
          'circle-stroke-opacity': 0.8,
        },
      })

      // Event labels
      map.addLayer({
        id: 'event-labels',
        type: 'symbol',
        source: 'events-geo',
        layout: {
          'text-field': ['get', 'title'],
          'text-size': 10,
          'text-offset': [0, 1.4],
          'text-anchor': 'top',
          'text-max-width': 14,
          'text-allow-overlap': false,
        },
        paint: {
          'text-color': '#e2e8f0',
          'text-halo-color': '#0f172a',
          'text-halo-width': 1.5,
        },
        minzoom: 6,
      })

      // Start with events, signals layers hidden
      for (const id of [...EVENT_LAYER_IDS, ...SIGNAL_LAYER_IDS]) {
        map.setLayoutProperty(id, 'visibility', 'none')
      }

      // ── Interactions ──

      // Cluster click -> zoom to expand
      map.on('click', 'cluster-circles', async (e) => {
        const features = map.queryRenderedFeatures(e.point, { layers: ['cluster-circles'] })
        if (!features.length) return
        const clusterId = features[0].properties?.cluster_id
        if (clusterId == null) return
        const src = map.getSource('entities') as maplibregl.GeoJSONSource
        try {
          const zoom = await src.getClusterExpansionZoom(clusterId)
          const coords = (features[0].geometry as GeoJSON.Point).coordinates as [number, number]
          map.easeTo({ center: coords, zoom: (zoom ?? 2) + 0.5 })
        } catch {
          // Cluster may have been removed during zoom
        }
      })

      // Hover on clusters
      map.on('mouseenter', 'cluster-circles', () => {
        map.getCanvas().style.cursor = 'pointer'
      })
      map.on('mouseleave', 'cluster-circles', () => {
        map.getCanvas().style.cursor = ''
      })

      // Hover on unclustered entity points
      map.on('mouseenter', 'entity-circles', (e) => {
        map.getCanvas().style.cursor = 'pointer'
        if (e.features && e.features.length > 0) {
          const feature = e.features[0]
          const coords = (feature.geometry as GeoJSON.Point).coordinates.slice() as [number, number]
          const label = feature.properties?.label ?? ''
          const entityType = feature.properties?.entityType ?? ''
          const color = feature.properties?.color ?? DEFAULT_COLOR

          popupRef.current
            ?.setLngLat(coords)
            .setHTML(
              `<div style="font-family: ui-monospace, monospace; font-size: 12px; line-height: 1.4;">
                <strong style="color: ${color}">${escapeHtml(label)}</strong>
                <br/>
                <span style="color: #94a3b8; text-transform: capitalize;">${escapeHtml(entityType)}</span>
              </div>`,
            )
            .addTo(map)
        }
      })

      map.on('mouseleave', 'entity-circles', () => {
        map.getCanvas().style.cursor = ''
        popupRef.current?.remove()
      })

      // Hover on signal heatmap points
      map.on('mouseenter', 'signal-heat-points', (e) => {
        map.getCanvas().style.cursor = 'pointer'
        if (e.features && e.features.length > 0) {
          const feature = e.features[0]
          const coords = (feature.geometry as GeoJSON.Point).coordinates.slice() as [number, number]
          const title = feature.properties?.title ?? ''
          const category = feature.properties?.category ?? ''
          const locationName = feature.properties?.location_name ?? ''

          popupRef.current
            ?.setLngLat(coords)
            .setHTML(
              `<div style="font-family: ui-monospace, monospace; font-size: 12px; line-height: 1.4; max-width: 220px;">
                <strong style="color: #f59e0b">${escapeHtml(title)}</strong>
                <br/>
                <span style="color: #94a3b8; text-transform: capitalize;">${escapeHtml(category)}</span>
                ${locationName ? `<br/><span style="color: #64748b;">${escapeHtml(locationName)}</span>` : ''}
              </div>`,
            )
            .addTo(map)
        }
      })

      map.on('mouseleave', 'signal-heat-points', () => {
        map.getCanvas().style.cursor = ''
        popupRef.current?.remove()
      })

      // Click on signal circle -> show persistent popup with details
      map.on('click', 'signal-click-circles', (e) => {
        if (e.features && e.features.length > 0) {
          const feature = e.features[0]
          const coords = (feature.geometry as GeoJSON.Point).coordinates.slice() as [number, number]
          const title = feature.properties?.title ?? ''
          const category = feature.properties?.category ?? ''
          const timestamp = feature.properties?.timestamp ?? ''
          const locationName = feature.properties?.location_name ?? ''

          new maplibregl.Popup({
            closeButton: true,
            closeOnClick: true,
            offset: 12,
            className: 'legba-map-popup',
          })
            .setLngLat(coords)
            .setHTML(
              `<div style="font-family: ui-monospace, monospace; font-size: 12px; line-height: 1.5; max-width: 240px;">
                <strong style="color: #f59e0b">${escapeHtml(String(title).slice(0, 100))}</strong>
                <br/>
                <span style="color: #94a3b8; text-transform: capitalize;">${escapeHtml(String(category))}</span>
                ${locationName ? `<br/><span style="color: #64748b;">${escapeHtml(String(locationName))}</span>` : ''}
                ${timestamp ? `<br/><span style="color: #475569; font-size: 10px;">${escapeHtml(String(timestamp))}</span>` : ''}
              </div>`,
            )
            .addTo(map)
        }
      })

      map.on('mouseenter', 'signal-click-circles', () => {
        map.getCanvas().style.cursor = 'pointer'
      })
      map.on('mouseleave', 'signal-click-circles', () => {
        map.getCanvas().style.cursor = ''
      })

      // Hover on event circles
      map.on('mouseenter', 'event-circles', (e) => {
        map.getCanvas().style.cursor = 'pointer'
        if (e.features && e.features.length > 0) {
          const feature = e.features[0]
          const coords = (feature.geometry as GeoJSON.Point).coordinates.slice() as [number, number]
          const title = feature.properties?.title ?? ''
          const severity = feature.properties?.severity ?? 'medium'
          const category = feature.properties?.category ?? ''
          const signalCount = feature.properties?.signal_count ?? 0
          const timestamp = feature.properties?.timestamp ?? ''
          const sevColor = SEVERITY_COLORS[severity] ?? DEFAULT_COLOR

          popupRef.current
            ?.setLngLat(coords)
            .setHTML(
              `<div style="font-family: ui-monospace, monospace; font-size: 12px; line-height: 1.4; max-width: 260px;">
                <strong style="color: ${sevColor}">${escapeHtml(String(title).slice(0, 80))}</strong>
                <br/>
                <span style="color: #94a3b8; text-transform: capitalize;">${escapeHtml(String(category))}</span>
                <span style="color: ${sevColor}; margin-left: 6px; text-transform: uppercase; font-size: 10px;">${escapeHtml(String(severity))}</span>
                <br/>
                <span style="color: #64748b;">${Number(signalCount)} signal${Number(signalCount) !== 1 ? 's' : ''}</span>
                ${timestamp ? `<br/><span style="color: #475569; font-size: 10px;">${escapeHtml(String(timestamp))}</span>` : ''}
              </div>`,
            )
            .addTo(map)
        }
      })

      map.on('mouseleave', 'event-circles', () => {
        map.getCanvas().style.cursor = ''
        popupRef.current?.remove()
      })

      // Click on event marker -> show popup and open event detail panel
      map.on('click', 'event-circles', (e) => {
        if (e.features && e.features.length > 0) {
          const feature = e.features[0]
          const eventId = feature.properties?.id
          if (eventId) {
            select({ type: 'event', id: eventId, name: feature.properties?.title ?? '' })
            openPanel('event-detail', { id: eventId })

            // Also show a persistent popup with summary
            const coords = (feature.geometry as GeoJSON.Point).coordinates.slice() as [number, number]
            const title = feature.properties?.title ?? ''
            const severity = feature.properties?.severity ?? 'medium'
            const signalCount = feature.properties?.signal_count ?? 0
            const sevColor = SEVERITY_COLORS[severity] ?? DEFAULT_COLOR

            new maplibregl.Popup({
              closeButton: true,
              closeOnClick: true,
              offset: 12,
              className: 'legba-map-popup',
            })
              .setLngLat(coords)
              .setHTML(
                `<div style="font-family: ui-monospace, monospace; font-size: 12px; line-height: 1.5; max-width: 260px;">
                  <strong style="color: ${sevColor}">${escapeHtml(String(title).slice(0, 80))}</strong>
                  <br/>
                  <span style="color: ${sevColor}; text-transform: uppercase; font-size: 10px;">${escapeHtml(String(severity))}</span>
                  <span style="color: #64748b; margin-left: 6px;">${Number(signalCount)} signal${Number(signalCount) !== 1 ? 's' : ''}</span>
                </div>`,
              )
              .addTo(map)
          }
        }
      })

      // Click on unclustered entity — use name for lookup (graph UUIDs don't match entity_profiles)
      map.on('click', 'entity-circles', (e) => {
        if (e.features && e.features.length > 0) {
          const feature = e.features[0]
          const label = feature.properties?.label || feature.properties?.name
          if (label) {
            handleMarkerClick(label, label)
          }
        }
      })

      setMapReady(true)
    })

    return () => {
      popupRef.current?.remove()
      popupRef.current = null
      map.remove()
      mapRef.current = null
      setMapReady(false)
    }
  }, [handleMarkerClick])

  // Update entity source data when geo data changes
  useEffect(() => {
    const map = mapRef.current
    if (!map || !mapReady || !data?.nodes) return

    const source = map.getSource('entities') as maplibregl.GeoJSONSource | undefined
    if (source) {
      source.setData(nodesToGeoJSON(data.nodes))
    }
  }, [data, mapReady])

  // Update event source when event geo data changes
  // Apply category filter and time window filter to geo data before setting on sources
  const filteredEventGeo = useMemo(() => {
    if (!eventGeo) return eventGeo
    let features = eventGeo.features
    if (categoryFilter.size > 0) {
      features = features.filter((f: { properties?: { category?: string } }) =>
        categoryFilter.has(f.properties?.category ?? '')
      )
    }
    if (timeWindow) {
      const [start, end] = timeWindow
      const startMs = new Date(start).getTime()
      const endMs = new Date(end).getTime()
      features = features.filter((f: { properties?: { timestamp?: string | null } }) => {
        const ts = f.properties?.timestamp
        if (!ts) return false
        const t = new Date(ts).getTime()
        return t >= startMs && t <= endMs
      })
    }
    if (features === eventGeo.features) return eventGeo
    return { ...eventGeo, features }
  }, [eventGeo, categoryFilter, timeWindow])

  const filteredSignalGeo = useMemo(() => {
    if (!signalGeo) return signalGeo
    let features = signalGeo.features
    if (categoryFilter.size > 0) {
      features = features.filter((f: { properties?: { category?: string } }) =>
        categoryFilter.has(f.properties?.category ?? '')
      )
    }
    if (timeWindow) {
      const [start, end] = timeWindow
      const startMs = new Date(start).getTime()
      const endMs = new Date(end).getTime()
      features = features.filter((f: { properties?: { timestamp?: string | null } }) => {
        const ts = f.properties?.timestamp
        if (!ts) return false
        const t = new Date(ts).getTime()
        return t >= startMs && t <= endMs
      })
    }
    if (features === signalGeo.features) return signalGeo
    return { ...signalGeo, features }
  }, [signalGeo, categoryFilter, timeWindow])

  useEffect(() => {
    const map = mapRef.current
    if (!map || !mapReady || !filteredEventGeo) return

    const source = map.getSource('events-geo') as maplibregl.GeoJSONSource | undefined
    if (source) {
      source.setData(filteredEventGeo)
    }
  }, [filteredEventGeo, mapReady])

  // Update signal heatmap source when signal geo data changes
  useEffect(() => {
    const map = mapRef.current
    if (!map || !mapReady || !filteredSignalGeo) return

    const source = map.getSource('signals-geo') as maplibregl.GeoJSONSource | undefined
    if (source) {
      source.setData(filteredSignalGeo)
    }
  }, [filteredSignalGeo, mapReady])

  // Toggle layer visibility when mode changes
  // Events mode: show both signal heatmap (underneath) and event markers (on top)
  // Signals mode: show only signal heatmap
  // Entities mode: show only entity markers
  useEffect(() => {
    const map = mapRef.current
    if (!map || !mapReady) return

    const entityVis = mode === 'entities' ? 'visible' : 'none'
    const eventVis = (mode === 'events') ? 'visible' : 'none'
    // Signal heatmap visible in both 'signals' mode AND 'events' mode (as underlay)
    const signalVis = (mode === 'signals' || mode === 'events') ? 'visible' : 'none'

    for (const id of ENTITY_LAYER_IDS) {
      if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', entityVis)
    }
    for (const id of EVENT_LAYER_IDS) {
      if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', eventVis)
    }
    for (const id of SIGNAL_LAYER_IDS) {
      if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', signalVis)
    }

    // When events mode, reduce heatmap opacity so events stand out
    if (map.getLayer('signal-heat')) {
      map.setPaintProperty('signal-heat', 'heatmap-opacity', mode === 'events' ? 0.45 : 0.7)
    }
  }, [mode, mapReady])

  // Center on selected entity if it has geo coordinates
  useEffect(() => {
    const map = mapRef.current
    if (!map || !data?.nodes || !selected) return
    if (selected.type !== 'entity') return

    const node = data.nodes.find(
      (n) => n.entity_id === selected.id || n.id === selected.id || n.label.toLowerCase() === selected.name.toLowerCase(),
    )
    if (node) {
      map.flyTo({
        center: [node.lon, node.lat],
        zoom: Math.max(map.getZoom(), 5),
        duration: 1200,
      })
    }
  }, [selected, data])

  const legend = data?.nodes ? legendEntries(data.nodes) : []
  const eventCount = eventGeo?.features?.length ?? 0
  const signalCount = signalGeo?.features?.length ?? 0

  return (
    <div className="flex flex-col h-full">
      {/* Header bar */}
      <div className="flex items-center gap-2 p-2 border-b border-border shrink-0 text-xs text-muted-foreground">
        <span>
          Geo map
          {mode === 'entities' && data && ` \u2014 ${data.nodes.length} entities`}
          {mode === 'events' && ` \u2014 ${eventCount} events, ${signalCount} signals`}
          {mode === 'signals' && ` \u2014 ${signalCount} signals`}
        </span>

        {/* Mode toggle */}
        <div className="ml-auto flex items-center gap-0.5 bg-muted rounded-md p-0.5">
          <button
            onClick={() => setMode('entities')}
            className={`px-2 py-0.5 rounded text-xs transition-colors ${
              mode === 'entities'
                ? 'bg-background text-foreground shadow-sm'
                : 'text-muted-foreground hover:text-foreground'
            }`}
          >
            Entities
          </button>
          <button
            onClick={() => setMode('events')}
            className={`px-2 py-0.5 rounded text-xs transition-colors ${
              mode === 'events'
                ? 'bg-background text-foreground shadow-sm'
                : 'text-muted-foreground hover:text-foreground'
            }`}
          >
            Events
          </button>
          <button
            onClick={() => setMode('signals')}
            className={`px-2 py-0.5 rounded text-xs transition-colors ${
              mode === 'signals'
                ? 'bg-background text-foreground shadow-sm'
                : 'text-muted-foreground hover:text-foreground'
            }`}
          >
            Signals
          </button>
        </div>
      </div>

      {/* Time window banner */}
      {timeWindow && (
        <div className="flex items-center gap-2 px-2 py-1 border-b border-border shrink-0 bg-amber-500/10 text-xs">
          <span className="text-amber-400">
            Showing: {new Date(timeWindow[0]).toLocaleDateString()} to {new Date(timeWindow[1]).toLocaleDateString()}
          </span>
          <button
            onClick={() => setTimeWindow(null)}
            className="ml-auto px-2 py-0.5 text-[10px] rounded border border-amber-500/30 text-amber-400 hover:bg-amber-500/20 transition-colors"
          >
            Clear
          </button>
        </div>
      )}

      {/* Category filter pills — for events and signals modes */}
      {(mode === 'events' || mode === 'signals') && (
        <div className="flex items-center gap-1 px-2 py-1 border-b border-border shrink-0 overflow-x-auto">
          <span className="text-[10px] text-muted-foreground mr-0.5">Filter:</span>
          {['conflict', 'political', 'economic', 'disaster', 'health', 'social', 'technology', 'environment'].map((cat) => (
            <button
              key={cat}
              onClick={() => {
                setCategoryFilter((prev) => {
                  const next = new Set(prev)
                  if (next.has(cat)) next.delete(cat)
                  else next.add(cat)
                  return next
                })
              }}
              className={`h-5 px-2 text-[10px] rounded-full border transition-colors capitalize ${
                categoryFilter.has(cat)
                  ? 'bg-primary text-primary-foreground border-primary'
                  : 'bg-muted text-muted-foreground border-border hover:bg-accent'
              }`}
            >
              {cat}
            </button>
          ))}
          {categoryFilter.size > 0 && (
            <button
              onClick={() => setCategoryFilter(new Set())}
              className="h-5 px-2 text-[10px] rounded-full border border-border text-muted-foreground hover:bg-accent"
            >
              Clear
            </button>
          )}
        </div>
      )}

      {/* Map container */}
      <div className="flex-1 relative">
        {isLoading && !data && (
          <div className="absolute inset-0 flex items-center justify-center z-10 text-sm text-muted-foreground bg-background/80">
            Loading geo data...
          </div>
        )}
        <div ref={containerRef} className="absolute inset-0" />

        {/* Signals: insufficient data notice */}
        {mode === 'signals' && signalCount === 0 && (
          <div className="absolute top-12 left-1/2 -translate-x-1/2 z-10 bg-background/90 backdrop-blur-sm border border-border rounded-md px-4 py-2 text-xs text-muted-foreground">
            No geo-coded signals available for heatmap.
          </div>
        )}

        {/* Entity legend overlay (entities mode) */}
        {mode === 'entities' && legend.length > 0 && (
          <div className="absolute bottom-3 left-3 z-10 bg-background/85 backdrop-blur-sm border border-border rounded-md px-3 py-2 text-xs">
            <div className="text-muted-foreground mb-1 font-medium">Entity Types</div>
            <div className="flex flex-col gap-0.5">
              {legend.map((e) => (
                <div key={e.type} className="flex items-center gap-1.5">
                  <span
                    className="inline-block w-2.5 h-2.5 rounded-full shrink-0"
                    style={{ backgroundColor: e.color }}
                  />
                  <span className="text-foreground/80 capitalize">{e.type.replace('_', ' ')}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Event severity legend (events mode) */}
        {mode === 'events' && eventCount > 0 && (
          <div className="absolute bottom-3 left-3 z-10 bg-background/85 backdrop-blur-sm border border-border rounded-md px-3 py-2 text-xs">
            <div className="text-muted-foreground mb-1 font-medium">Event Severity</div>
            <div className="flex flex-col gap-0.5">
              {Object.entries(SEVERITY_COLORS).map(([sev, color]) => (
                <div key={sev} className="flex items-center gap-1.5">
                  <span className="inline-block w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: color }} />
                  <span className="text-foreground/80 capitalize">{sev}</span>
                </div>
              ))}
            </div>
            <div className="text-muted-foreground mt-1.5 pt-1 border-t border-border/50">
              Circle size = signal count
            </div>
            <div className="text-muted-foreground mt-0.5">
              Heatmap = raw signal density
            </div>
          </div>
        )}

        {/* Signals heatmap legend overlay (signals mode) */}
        {mode === 'signals' && signalCount > 0 && (
          <div className="absolute bottom-3 left-3 z-10 bg-background/85 backdrop-blur-sm border border-border rounded-md px-3 py-2 text-xs">
            <div className="text-muted-foreground mb-1 font-medium">Signal Density</div>
            <div className="flex items-center gap-1">
              <span className="text-muted-foreground">Low</span>
              <div
                className="w-24 h-2.5 rounded-sm"
                style={{
                  background: 'linear-gradient(to right, #2563eb, #7c3aed, #dc2626, #f59e0b, #ffffff)',
                }}
              />
              <span className="text-muted-foreground">High</span>
            </div>
            <div className="text-muted-foreground mt-1">{signalCount} geo-coded signals</div>
          </div>
        )}
      </div>

      {/* Inject popup styles */}
      <style>{`
        .legba-map-popup .maplibregl-popup-content {
          background: #1e293b;
          border: 1px solid #334155;
          border-radius: 6px;
          padding: 6px 10px;
          box-shadow: 0 4px 12px rgba(0,0,0,0.4);
        }
        .legba-map-popup .maplibregl-popup-tip {
          border-top-color: #1e293b;
        }
      `}</style>
    </div>
  )
}

/** Escape HTML to prevent XSS in popup content */
function escapeHtml(str: string): string {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}
