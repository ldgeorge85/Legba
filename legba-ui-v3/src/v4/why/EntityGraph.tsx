/**
 * The Why — entity relationship ego-graph (`why:entity`).
 *
 * Ego-centers the entity knowledge graph on a single entity and renders its
 * immediate relationship neighbourhood with Cytoscape. The `center` entity is
 * emphasised (larger, accent-coloured); each edge is coloured + labelled by its
 * relationship type. Entity-type FILTER CHIPS toggle neighbour classes, a
 * relationship colour LEGEND keys the edges, and zoom +/−/fit buttons sit over
 * the canvas — the shared old-KG controls (screenshot 19), via `GraphControls`.
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
 * edges resolve. The shaping (class filter, degree size, ego centre always
 * kept) lives in the pure `buildEntityGraphElements` (@/lib/graphModel).
 *
 * This endpoint may not be wired in every deployment; a 404 / any error degrades
 * to a graceful centered empty state rather than crashing the room.
 *
 * The cytoscape mount is unchanged from the #90 crash fix: a stable no-op preset
 * mount layout + the real `concentric` run from the resize observer
 * (attachFitOnResize) once sized, gated on `useVisibleSize`.
 */
import { useQuery } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type cytoscape from 'cytoscape'
import type { Core, ElementDefinition, StylesheetStyle } from 'cytoscape'
import CytoscapeComponent from 'react-cytoscapejs'
import { apiGet, ApiError } from '@/lib/api'
import {
  buildEntityGraphElements,
  entityClassColor,
  presentEntityClasses,
  presentRelationshipTypes,
  relationshipColor,
  type EntityGraphEdge,
  type EntityGraphNode,
} from '@/lib/graphModel'
import { GraphControls } from '@/components/GraphControls'
import { attachFitOnResize, useVisibleSize } from '@/lib/cytoscapeFit'
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
      // Low threshold so the ego neighbourhood's labels render at fit-zoom.
      'min-zoomed-font-size': 4,
      width: 'data(size)',
      height: 'data(size)',
      'border-width': 1,
      'border-color': '#1e293b', // slate-800
    },
  },
  {
    // The ego centre — accent-coloured + emphasised border (size set in data).
    selector: 'node[?is_center]',
    style: {
      'background-color': CENTER_COLOR,
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
      'line-color': 'data(color)',
      'curve-style': 'bezier',
      'font-size': 9,
      color: '#cbd5e1', // slate-300 edge labels
      'text-rotation': 'autorotate',
      'text-outline-color': '#0f1115',
      'text-outline-width': 2,
      'min-zoomed-font-size': 4,
      opacity: 0.85,
    },
  },
  {
    selector: 'node:selected',
    style: { 'border-color': '#e2e8f0', 'border-width': 3 },
  },
]

// #90 — stable no-op mount layout (fit:false so it can't dereference the bounding
// box at 0×0 → no `reading 'h'` crash). Module constant so react-cytoscapejs's
// `patchLayout` doesn't re-run it every render. The real `concentric` runs from
// the resize observer (attachFitOnResize) once the container has a size.
const PRESET_NOOP = { name: 'preset', fit: false, animate: false } as cytoscape.LayoutOptions
const CONCENTRIC_LAYOUT = {
  name: 'concentric',
  animate: false,
  // Pull the ego centre into the middle ring; neighbours fan out.
  concentric: (node: cytoscape.NodeSingular) => (node.data('is_center') ? 2 : 1),
  levelWidth: () => 1,
  minNodeSpacing: 40,
  padding: 24,
} as cytoscape.LayoutOptions

export default function EntityGraph({ center }: { center: string }) {
  const select = useSelection((s) => s.select)
  const cyRef = useRef<Core | null>(null)
  const fitCleanup = useRef<(() => void) | null>(null)
  const [hiddenClasses, setHiddenClasses] = useState<Set<string>>(() => new Set())
  // #90 — defer constructing cytoscape until this surface is on-screen + sized
  // (the Why panel is often a hidden background tab brushed by a selection made
  // elsewhere); a fresh mount in a 0×0 container crashes on the auto-layout.
  const { ref: canvasRef, visible } = useVisibleSize<HTMLDivElement>()

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

  const centerLc = center.trim().toLowerCase()
  // Resolve the actual centre node id (canonical name) so the projector can keep
  // + emphasise it; matched case-insensitively against the served nodes.
  const centerId = useMemo(() => {
    const hit = graphQ.data?.nodes.find((n) => n.canonical_name.toLowerCase() === centerLc)
    return hit?.canonical_name ?? center
  }, [graphQ.data, centerLc, center])

  const { rawNodes, rawEdges } = useMemo(() => {
    const g = graphQ.data
    if (!g) return { rawNodes: [] as EntityGraphNode[], rawEdges: [] as EntityGraphEdge[] }
    const rawNodes: EntityGraphNode[] = g.nodes.map((n) => ({
      id: n.canonical_name,
      label: n.canonical_name,
      entity_class: n.entity_class,
      mentions: n.mentions,
    }))
    const rawEdges: EntityGraphEdge[] = g.edges.map((e) => ({
      source: e.source,
      target: e.target,
      relationship_type: e.relationship_type,
      confidence: e.confidence,
    }))
    return { rawNodes, rawEdges }
  }, [graphQ.data])

  const allClasses = useMemo(() => presentEntityClasses(rawNodes), [rawNodes])
  const visibleClasses = useMemo(
    () => new Set(allClasses.filter((c) => !hiddenClasses.has(c))),
    [allClasses, hiddenClasses],
  )

  const graph = useMemo(
    () =>
      buildEntityGraphElements(rawNodes, rawEdges, {
        visibleClasses: hiddenClasses.size > 0 ? visibleClasses : undefined,
        // The ego graph keeps everything served (the neighbourhood is already
        // scoped) — but always keeps + emphasises the centre.
        showOrphans: true,
        centerId,
      }),
    [rawNodes, rawEdges, hiddenClasses, visibleClasses, centerId],
  )

  const elements = useMemo<ElementDefinition[]>(
    () => [
      ...graph.nodes.map((n) => ({
        group: 'nodes' as const,
        data: {
          ...n.data,
          // In the focused ego view every neighbour is labelled (the set is small).
          label: n.data.label,
          is_center: n.data.id === centerId,
          color: n.data.id === centerId ? CENTER_COLOR : n.data.color,
        },
      })),
      ...graph.edges.map((e) => ({
        group: 'edges' as const,
        data: { ...e.data, label: e.data.relationship_type },
      })),
    ],
    [graph, centerId],
  )

  const presentRels = useMemo(
    () => presentRelationshipTypes(graph.edges.map((e) => e.data)),
    [graph.edges],
  )

  const toggleClass = (id: string) =>
    setHiddenClasses((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const onCyReady = useCallback(
    (cy: Core) => {
      cyRef.current = cy
      cy.removeAllListeners()
      cy.on('tap', 'node', (evt) => {
        const node = evt.target
        select({
          kind: 'entity',
          id: node.data('id'),
          label: node.data('name') ?? node.data('label'),
        })
      })
      // #90 — size/fit to the Dockview tab + run the real layout once sized. The
      // measuring `concentric` layout can't run at 0×0 (it would crash on the
      // undefined bounding box), so it runs from the resize observer, not at mount.
      fitCleanup.current?.()
      fitCleanup.current = attachFitOnResize(cy, { layout: CONCENTRIC_LAYOUT, padding: 24 })
    },
    [select],
  )

  // #90 — disconnect the resize observer on unmount so a pending tick never
  // touches a destroyed cy (the canvas unmounts when the selection clears/changes).
  useEffect(() => () => fitCleanup.current?.(), [])

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
    <div ref={canvasRef} className="relative h-full w-full min-h-[300px] bg-surface-200" data-testid="why-entity-graph">
      <GraphControls
        chipsLabel="Types"
        legendLabel="Edges"
        variant="light"
        chips={allClasses.map((c) => ({ id: c, color: entityClassColor(c) }))}
        activeChips={visibleClasses}
        onToggleChip={toggleClass}
        onSelectAllChips={() => setHiddenClasses(new Set())}
        onClearChips={() => setHiddenClasses(new Set(allClasses))}
        legend={presentRels.map((r) => ({ id: r, color: relationshipColor(r) }))}
        onZoomIn={() => cyRef.current?.zoom(cyRef.current.zoom() * 1.3)}
        onZoomOut={() => cyRef.current?.zoom(cyRef.current.zoom() / 1.3)}
        onFit={() => cyRef.current?.fit(undefined, 24)}
      />
      {visible && (
        <CytoscapeComponent
          cy={onCyReady}
          elements={elements}
          stylesheet={STYLESHEET}
          // #90 — stable no-op mount layout (fit:false); the real `concentric` runs
          // once from the resize observer, after the tab is sized.
          layout={PRESET_NOOP}
          style={{ position: 'absolute', top: 0, right: 0, bottom: 0, left: 0 }}
          userZoomingEnabled
          userPanningEnabled
          minZoom={0.2}
          maxZoom={3}
        />
      )}
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
