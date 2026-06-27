/**
 * Entity Graph (`system.entity_graph`) — the entity knowledge-graph viz.
 *
 * Source-first analogue of v2's Sigma.js knowledge graph. Renders
 * GET /api/v1/entities/graph (entity_profiles nodes + proposed_edges
 * co-occurrence relationships) with Cytoscape:
 *   - nodes colored by entity_class, sized by mention count
 *   - top-N densest subgraph by default; click a node to ego-center on it
 *   - listens for `legba:open-entity-graph` (from the Entities panel) to center
 */
import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState } from 'react'
import type cytoscape from 'cytoscape'
import type { Core, ElementDefinition, StylesheetStyle } from 'cytoscape'
import CytoscapeComponent from 'react-cytoscapejs'
import { PanelChrome } from '@/components/PanelChrome'
import { apiGet } from '@/lib/api'
import { attachFitOnResize, useVisibleSize } from '@/lib/cytoscapeFit'
import { isCountry } from '@/lib/countryGeo'
import type { PanelProps } from '@/types'
import { useSelection } from '@/state/selection'

interface GNode {
  id: string
  canonical_name: string
  entity_class: string
  mentions: number
}
interface GEdge {
  source: string
  target: string
  relationship_type: string
  confidence: number
}
interface GraphResp {
  nodes: GNode[]
  edges: GEdge[]
}

const CLASS_COLOR: Record<string, string> = {
  person: '#f59e0b',
  organization: '#60a5fa',
  location: '#10b981',
  event: '#a78bfa',
  entity: '#94a3b8',
}

const STYLESHEET: StylesheetStyle[] = [
  {
    selector: 'node',
    style: {
      'background-color': 'data(color)',
      label: 'data(label)',
      'font-size': 9,
      color: '#cbd5e1',
      'text-valign': 'bottom',
      'text-margin-y': 2,
      width: 'data(size)',
      height: 'data(size)',
    },
  },
  {
    selector: 'edge',
    style: {
      width: 'data(w)',
      'line-color': '#334155',
      'curve-style': 'haystack',
      opacity: 0.6,
    },
  },
  { selector: 'node:selected', style: { 'border-width': 2, 'border-color': '#e2e8f0' } },
]

// #90 — a STABLE no-op mount layout. `preset` with the default `fit:true` calls
// `cy.fit()` which dereferences the (undefined) bounding box at 0×0 → the same
// `reading 'h'` crash; `fit:false` makes it inert. Must be a module constant so
// react-cytoscapejs's `patchLayout` doesn't re-run it on every render (a fresh
// inline object literal diffs as "changed" each time). The REAL `cose` runs from
// the resize observer (attachFitOnResize) once the container has a size.
const PRESET_NOOP = { name: 'preset', fit: false, animate: false } as cytoscape.LayoutOptions
const COSE_LAYOUT = { name: 'cose', animate: false, idealEdgeLength: 80 } as cytoscape.LayoutOptions

export default function EntityGraphPanel({ registration }: PanelProps) {
  const [center, setCenter] = useState<string | null>(null)
  const fitCleanup = useRef<(() => void) | null>(null)
  // #90 — don't construct cytoscape until this Dockview tab is on-screen + sized,
  // else its auto-run default layout reads a detached 0×0 box → `reading 'h'`.
  const { ref: canvasRef, visible } = useVisibleSize<HTMLDivElement>()

  // Redesign Move 2: center on the shared selection when it's an entity
  // (replaces the legacy `legba:open-entity-graph` window listener).
  const selection = useSelection((s) => s.selection)
  useEffect(() => {
    if (selection?.kind === 'entity') setCenter(selection.id)
  }, [selection])

  // #90 — disconnect the resize observer on unmount (the cytoscape canvas is
  // unmounted whenever the element set empties during a re-center re-query, and
  // when the panel tab closes), so a pending tick never touches a destroyed cy.
  useEffect(() => () => fitCleanup.current?.(), [])

  const graphQ = useQuery<GraphResp>({
    queryKey: ['entity-graph', center],
    queryFn: () =>
      apiGet<GraphResp>(`/entities/graph?limit=80${center ? `&center=${encodeURIComponent(center)}` : ''}`),
    refetchInterval: 120_000,
  })

  const elements = useMemo<ElementDefinition[]>(() => {
    const g = graphQ.data
    if (!g) return []
    const ids = new Set(g.nodes.map((n) => n.canonical_name))
    const nodeEls: ElementDefinition[] = g.nodes.map((n) => {
      // NER classes country mentions as generic `entity`; recolor recognized
      // countries as `location` (lib/countryGeo) so the graph isn't all grey.
      const cls = n.entity_class === 'entity' && isCountry(n.canonical_name) ? 'location' : n.entity_class
      return {
        data: {
          id: n.canonical_name,
          label: n.canonical_name.length > 22 ? n.canonical_name.slice(0, 21) + '…' : n.canonical_name,
          color: CLASS_COLOR[cls] ?? '#94a3b8',
          size: Math.min(46, 14 + Math.sqrt(n.mentions) * 4),
        },
      }
    })
    const edgeEls: ElementDefinition[] = g.edges
      .filter((e) => ids.has(e.source) && ids.has(e.target) && e.source !== e.target)
      .map((e, i) => ({
        data: { id: `e${i}`, source: e.source, target: e.target, w: 1 + e.confidence * 3 },
      }))
    return [...nodeEls, ...edgeEls]
  }, [graphQ.data])

  return (
    <PanelChrome
      registration={registration}
      subtitle={
        center
          ? `ego: ${center} · ${elements.filter((e) => !('source' in (e.data as object))).length} nodes`
          : `top subgraph · ${graphQ.data?.nodes.length ?? 0} nodes / ${graphQ.data?.edges.length ?? 0} edges`
      }
      onRefresh={() => graphQ.refetch()}
      actions={
        center ? (
          <button
            onClick={() => setCenter(null)}
            className="text-[10px] border border-slate-700 rounded px-1.5 py-0.5 text-slate-300 hover:bg-surface-200"
            data-testid="entity-graph-reset"
          >
            ← whole graph
          </button>
        ) : undefined
      }
    >
      <div ref={canvasRef} className="relative flex-1 min-h-[300px]" data-testid="entity-graph-canvas">
        {graphQ.isLoading && (
          <div className="absolute inset-0 z-10 flex items-center justify-center text-slate-500 text-sm">loading graph…</div>
        )}
        {!graphQ.isLoading && elements.length === 0 && (
          <div className="absolute inset-0 z-10 flex items-center justify-center text-slate-500 text-sm" data-testid="entity-graph-empty">
            no entity relationships yet
          </div>
        )}
        {/* #90 — construct cytoscape only once the tab is visible+sized (useVisibleSize);
            a fresh mount in a hidden 0×0 Dockview tab auto-runs cytoscape's default
            layout against a detached box → `reading 'h'`. Once mounted, the real
            `cose` runs from the resize observer (attachFitOnResize). */}
        {visible && elements.length > 0 && (
          <CytoscapeComponent
            elements={elements}
            stylesheet={STYLESHEET}
            // Stable no-op mount layout (fit:false so it can't touch the bounding box
            // at 0×0); the real `cose` runs from the resize observer once sized.
            layout={PRESET_NOOP}
            style={{ position: 'absolute', top: 0, right: 0, bottom: 0, left: 0 }}
            minZoom={0.2}
            maxZoom={3}
            cy={(cy: Core) => {
              cy.removeListener('tap', 'node')
              cy.on('tap', 'node', (evt) => setCenter(evt.target.id()))
              // #90 — size/fit to the Dockview tab + run the real layout once sized.
              fitCleanup.current?.()
              fitCleanup.current = attachFitOnResize(cy, { layout: COSE_LAYOUT, padding: 30 })
            }}
          />
        )}
      </div>
    </PanelChrome>
  )
}
