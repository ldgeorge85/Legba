/**
 * The Why — entity relationship ego-graph (`why:entity`).
 *
 * Ego-centers the entity knowledge graph on a single entity and renders its
 * immediate relationship neighbourhood with Cytoscape. The `center` entity is
 * emphasised (larger, accent-coloured); each edge is labelled by its
 * relationship type and weighted by confidence.
 *
 * Data source — GET /api/v1/entities/graph?center=<name>&limit=60, served by
 * `legba.data.registry.entities_api.build_entities_router`. The response is the
 * `EntityGraph` pydantic model:
 *
 *   nodes: EntityNode[]   — { id, canonical_name, entity_class, entity_type,
 *                             mentions, geo_lat, geo_lon, geo_country,
 *                             completeness_score }
 *   edges: GraphEdge[]    — { source, target, relationship_type, confidence }
 *
 * NOTE the edge endpoints (`source` / `target`) are entity *canonical names*
 * (the SQL `source_entity` / `target_entity` columns are mapped into the model's
 * `source` / `target` fields), and the ego `center` is matched against a node's
 * `canonical_name`. Cytoscape node ids are therefore the canonical names so the
 * edges resolve. `kindColor` (from @/lib/graphModel) tints neighbour nodes by
 * entity_class for cross-room styling consistency.
 *
 * This endpoint may not be wired in every deployment; a 404 / any error degrades
 * to a graceful centered empty state rather than crashing the room.
 */
import { useQuery } from '@tanstack/react-query'
import { useCallback, useMemo, useRef } from 'react'
import type cytoscape from 'cytoscape'
import type { Core, ElementDefinition, StylesheetStyle } from 'cytoscape'
import CytoscapeComponent from 'react-cytoscapejs'
import { apiGet, ApiError } from '@/lib/api'
import { kindColor } from '@/lib/graphModel'
import { attachFitOnResize } from '@/lib/cytoscapeFit'
import { useSelection } from '@/state/selection'
import { cn } from '@/lib/cn'

/** Edge as served — mirrors `GraphEdge` (entities_api.py). */
interface GraphEdge {
  source: string
  target: string
  relationship_type: string
  confidence: number
}

/** Node as served — mirrors `EntityNode` (entities_api.py). The graph viz only
 *  needs identity + class; the geo/score fields are carried but unused here. */
interface EntityNode {
  id: string
  canonical_name: string
  entity_class: string
  entity_type: string
  mentions: number
}

/** Response body of GET /entities/graph — mirrors `EntityGraph`. */
interface EntityGraph {
  nodes: EntityNode[]
  edges: GraphEdge[]
}

/** Accent for the emphasised ego centre — Tailwind `accent.info` (#3b82f6).
 *  Cytoscape's stylesheet bypasses PostCSS so the token is inlined as hex. */
const CENTER_COLOR = '#3b82f6'
const NEIGHBOUR_DEFAULT = '#94a3b8' // slate-400 (kindColor fallback)

/** Map confidence (0..1) onto an edge width of ~1–4px. */
function edgeWidth(confidence: number): number {
  const c = Number.isFinite(confidence) ? Math.min(Math.max(confidence, 0), 1) : 0
  return 1 + c * 3
}

function truncate(s: string, n: number): string {
  return s.length <= n ? s : `${s.slice(0, n - 1)}…`
}

const STYLESHEET: StylesheetStyle[] = [
  {
    selector: 'node',
    style: {
      'background-color': 'data(color)',
      label: 'data(label)',
      color: '#e2e8f0', // slate-200 labels
      'font-size': 10,
      'text-valign': 'bottom',
      'text-halign': 'center',
      'text-margin-y': 3,
      'text-outline-color': '#0f1115', // surface-200
      'text-outline-width': 2,
      width: 18,
      height: 18,
      'border-width': 1,
      'border-color': '#1e293b', // slate-800
    },
  },
  {
    // The ego centre — larger + accent-coloured + emphasised border.
    selector: 'node[?is_center]',
    style: {
      'background-color': CENTER_COLOR,
      width: 34,
      height: 34,
      'border-width': 2,
      'border-color': '#bfdbfe', // blue-200
      'font-size': 12,
      'font-weight': 700,
      color: '#e2e8f0',
    },
  },
  {
    selector: 'edge',
    style: {
      width: 'data(w)',
      label: 'data(label)',
      'line-color': '#475569', // slate-600
      'curve-style': 'bezier',
      'font-size': 8,
      color: '#94a3b8', // slate-400 edge labels
      'text-rotation': 'autorotate',
      'text-outline-color': '#0f1115',
      'text-outline-width': 2,
      opacity: 0.85,
    },
  },
  {
    selector: 'node:selected',
    style: { 'border-color': '#e2e8f0', 'border-width': 3 },
  },
]

export default function EntityGraph({ center }: { center: string }) {
  const select = useSelection((s) => s.select)
  const cyRef = useRef<Core | null>(null)
  const fitCleanup = useRef<(() => void) | null>(null)

  const graphQ = useQuery<EntityGraph>({
    enabled: !!center,
    queryKey: ['why-entity-graph', center],
    queryFn: async () => {
      try {
        return await apiGet<EntityGraph>(
          `/entities/graph?center=${encodeURIComponent(center)}&limit=60`,
        )
      } catch (e) {
        // The endpoint may not be wired — degrade to empty rather than crash.
        if (e instanceof ApiError && e.status === 404) {
          return { nodes: [], edges: [] }
        }
        throw e
      }
    },
    retry: false,
  })

  const elements = useMemo<ElementDefinition[]>(() => {
    const g = graphQ.data
    if (!g) return []
    const centerLc = center.trim().toLowerCase()

    const nodeEls: ElementDefinition[] = g.nodes.map((n) => {
      const isCenter = n.canonical_name.toLowerCase() === centerLc
      return {
        group: 'nodes' as const,
        data: {
          id: n.canonical_name,
          label: truncate(n.canonical_name, 24),
          entity_class: n.entity_class,
          is_center: isCenter,
          color: isCenter ? CENTER_COLOR : kindColor(n.entity_class) || NEIGHBOUR_DEFAULT,
        },
      }
    })

    // Only keep edges whose endpoints both resolved to a rendered node, and
    // drop self-loops. Edge ids are positional (edge endpoints aren't unique).
    const ids = new Set(g.nodes.map((n) => n.canonical_name))
    const edgeEls: ElementDefinition[] = g.edges
      .filter((e) => ids.has(e.source) && ids.has(e.target) && e.source !== e.target)
      .map((e, i) => ({
        group: 'edges' as const,
        data: {
          id: `e${i}`,
          source: e.source,
          target: e.target,
          label: e.relationship_type,
          w: edgeWidth(e.confidence),
        },
      }))

    return [...nodeEls, ...edgeEls]
  }, [graphQ.data, center])

  const onCyReady = useCallback(
    (cy: Core) => {
      cyRef.current = cy
      cy.removeAllListeners()
      cy.on('tap', 'node', (evt) => {
        const node = evt.target
        select({
          kind: 'entity',
          id: node.data('id'),
          label: node.data('label'),
        })
      })
      // #90 — keep the canvas sized/fitted to its Dockview tab (fixes blank graph).
      fitCleanup.current?.()
      fitCleanup.current = attachFitOnResize(cy)
    },
    [select],
  )

  // --- States -------------------------------------------------------------
  // Failure other than a caught 404 (e.g. 500, network) ⇒ unavailable.
  if (graphQ.isError) {
    return <Centered>Entity graph unavailable</Centered>
  }

  if (graphQ.isLoading) {
    return <Centered>Loading entity graph…</Centered>
  }

  if (elements.length === 0) {
    return (
      <Centered>
        <div>No relationships for this entity</div>
        <div className="mt-1 text-slate-500 text-xs font-mono">{center}</div>
      </Centered>
    )
  }

  return (
    <div className="h-full w-full bg-surface-200" data-testid="why-entity-graph">
      <CytoscapeComponent
        cy={onCyReady}
        elements={elements}
        stylesheet={STYLESHEET}
        layout={
          {
            name: 'concentric',
            animate: false,
            // Pull the ego centre into the middle ring; neighbours fan out.
            concentric: (node: cytoscape.NodeSingular) => (node.data('is_center') ? 2 : 1),
            levelWidth: () => 1,
            minNodeSpacing: 40,
            padding: 24,
          } as cytoscape.LayoutOptions
        }
        style={{ width: '100%', height: '100%' }}
        minZoom={0.2}
        maxZoom={3}
      />
    </div>
  )
}

/** Centered chrome for loading / empty / unavailable states. Fills its
 *  container (h-full) so cytoscape's container has a real height too. */
function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div
      className={cn(
        'h-full w-full flex flex-col items-center justify-center text-center',
        'bg-surface-200 text-slate-400 text-sm px-4',
      )}
      data-testid="why-entity-graph-state"
    >
      {children}
    </div>
  )
}
