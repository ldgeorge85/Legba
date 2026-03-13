import { useRef, useEffect, useCallback } from 'react'
import maplibregl from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import { useGeoData } from '@/api/hooks'
import { useSelectionStore } from '@/stores/selection'
import { useWorkspaceStore } from '@/stores/workspace'
import type { GeoNode } from '@/api/types'

/** Map marker colors by entity type — consistent with entityTypeColor in utils */
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

export function MapPanel() {
  const containerRef = useRef<HTMLDivElement>(null)
  const mapRef = useRef<maplibregl.Map | null>(null)
  const popupRef = useRef<maplibregl.Popup | null>(null)

  const { data, isLoading } = useGeoData()
  const selected = useSelectionStore((s) => s.selected)
  const select = useSelectionStore((s) => s.select)
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
      // Add empty source — will be populated when data arrives
      map.addSource('entities', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      })

      // Circle layer for entity markers
      map.addLayer({
        id: 'entity-circles',
        type: 'circle',
        source: 'entities',
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

      // Label layer
      map.addLayer({
        id: 'entity-labels',
        type: 'symbol',
        source: 'entities',
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

      // Hover interaction
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

      // Click interaction
      map.on('click', 'entity-circles', (e) => {
        if (e.features && e.features.length > 0) {
          const feature = e.features[0]
          const entityId = feature.properties?.entity_id
          const label = feature.properties?.label
          if (entityId && label) {
            handleMarkerClick(entityId, label)
          }
        }
      })
    })

    return () => {
      popupRef.current?.remove()
      popupRef.current = null
      map.remove()
      mapRef.current = null
    }
  }, [handleMarkerClick])

  // Update source data when geo data changes
  useEffect(() => {
    const map = mapRef.current
    if (!map || !data?.nodes) return

    const updateSource = () => {
      const source = map.getSource('entities') as maplibregl.GeoJSONSource | undefined
      if (source) {
        source.setData(nodesToGeoJSON(data.nodes))
      }
    }

    if (map.isStyleLoaded()) {
      updateSource()
    } else {
      map.once('load', updateSource)
    }
  }, [data])

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

  return (
    <div className="flex flex-col h-full">
      {/* Header bar */}
      <div className="flex items-center gap-2 p-2 border-b border-border shrink-0 text-xs text-muted-foreground">
        <span>
          Geo map
          {data && ` \u2014 ${data.nodes.length} entities`}
        </span>
      </div>

      {/* Map container */}
      <div className="flex-1 relative">
        {isLoading && !data && (
          <div className="absolute inset-0 flex items-center justify-center z-10 text-sm text-muted-foreground bg-background/80">
            Loading geo data...
          </div>
        )}
        <div ref={containerRef} className="absolute inset-0" />

        {/* Legend overlay */}
        {legend.length > 0 && (
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
