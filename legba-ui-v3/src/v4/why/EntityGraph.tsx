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
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
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
import { selectRow, useSelection, type Selection } from '@/state/selection'
import { cn } from '@/lib/cn'
import { ArrowRight, Clock, Route, Share2 } from 'lucide-react'
import { extractCitations, type Citation } from '@/lib/citationsModel'
import {
  signalGeoPoints,
  type GeoPoint,
  type SignalGeoRow,
} from '@/lib/geoPoints'
import {
  findingPoints,
  signalPoints,
  situationPoints,
  type TimelinePoint,
  type TLFinding,
  type TLSignal,
  type TLSituation,
} from '@/lib/timelinePoints'
import { useWorldState } from '@/v4/world/worldState'
import { ReadGeoLens } from '@/v4/world/LeafletWorldMap'
import { ReadTimelineLens } from '@/v4/world/TimeScrubber'

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

// ===========================================================================
// P1-T7 — Selection-scoped LENSES over a country / finding READ.
//
// Three lenses over the SAME read, all brushing the SAME global selection:
//   • Temporal — a timeline (`ReadTimelineLens`) + geo (`ReadGeoLens`) of the
//     read's evidence (the country's signals/findings/situations), with the
//     read's directly-cited signals emphasised.
//   • Node graph — the read's claim graph (`ReadGraphLens` below): the read at
//     the centre, the country it assesses, and its cited-evidence signals.
//
// Clicking any point/node `selectRow`s the underlying row → every room brushes
// and the Inspector re-opens that row (its evidence). The lens stays anchored
// to the last country/finding read even when a signal point is then selected,
// so a drill into evidence doesn't tear the lens off its read.
// ===========================================================================

/** Per-kind node fill for the claim graph. */
const LENS_NODE_COLOR: Record<string, string> = {
  finding: '#fbbf24', // amber-400 — analyst output (the read)
  signal: '#60a5fa', // blue-400 — cited evidence
  target: '#34d399', // emerald-400 — the country assessed
}

/** The read a lens is anchored to. */
interface LensRead {
  kind: string
  id: string
  label: string
  targetId: string | null
}

// ---------------------------------------------------------------------------
// P1-T9 — the ONE graph verb: "is there a path A→B / who's the broker?".
//
// GET /api/v1/graph/path?source=&target= returns the shortest relationship path
// between two actors + the highest-betweenness broker ON that path. The AGE path
// engine is lazily imported on the backend and SOFT-FAILS to `found:false` + a
// `detail` string ('graph-path engine unavailable in this build (...)') until
// the backend decouple deploys — rendered HONESTLY, never a fabricated path or
// an invented score. The server clamps len/budget; nothing is capped here.
// ---------------------------------------------------------------------------

/** One hop on the path — a REAL AGE relationship edge. Mirrors `PathEdge`
 *  (graph_structure_api.py); endpoints are entity ids (canonical names). */
interface GraphPathEdge {
  source: string
  target: string
  label: string
}

/** The highest-betweenness node ON the path. Mirrors `PathBroker`. */
interface GraphPathBroker {
  node: string
  betweenness: number
}

/** GET /graph/path body. Mirrors `GraphPath` (graph_structure_api.py). */
interface GraphPathResp {
  found: boolean
  source: string
  target: string
  path: string[]
  edges: GraphPathEdge[]
  length: number | null
  broker: GraphPathBroker | null
  max_len: number
  detail: string
}

/**
 * The compact find-path / broker verb that lives atop the node-graph lens. Two
 * actor inputs + a Find-path button → GET /graph/path; renders the ordered node
 * + REAL-AGE-edge chain (every node + edge clickable → `selectRow` to its entity
 * row) and names the broker + its betweenness. 'no path' and 'engine
 * unavailable' degrade to the response's honest `detail` string — there is no
 * client-side fabrication of a path or a score, and len/budget is the server's.
 */
function FindPathControl() {
  const [source, setSource] = useState('')
  const [target, setTarget] = useState('')
  const [submitted, setSubmitted] = useState<{ source: string; target: string } | null>(null)

  const pathQ = useQuery<GraphPathResp>({
    enabled: !!submitted,
    queryKey: ['graph-path', submitted?.source ?? '', submitted?.target ?? ''],
    queryFn: async () => {
      const sub = submitted
      // Unreachable: the query is gated by `enabled: !!submitted`.
      if (!sub) throw new Error('no actors submitted')
      return apiGet<GraphPathResp>(
        `/graph/path?source=${encodeURIComponent(sub.source)}&target=${encodeURIComponent(sub.target)}`,
      )
    },
    retry: false,
  })

  const submit = () => {
    const s = source.trim()
    const t = target.trim()
    if (!s || !t) return
    setSubmitted({ source: s, target: t })
  }

  const data = pathQ.data ?? null
  const broker = data?.broker ?? null
  const brokerNode = broker?.node ?? null

  return (
    <div
      className="shrink-0 border-b border-slate-800 bg-surface-300 px-3 py-2"
      data-testid="find-path-control"
    >
      <div className="flex items-center gap-1.5">
        <Route className="h-3.5 w-3.5 shrink-0 text-slate-400" aria-hidden />
        <input
          value={source}
          onChange={(e) => setSource(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') submit()
          }}
          placeholder="source actor"
          aria-label="path source actor"
          className="min-w-0 flex-1 rounded border border-slate-700 bg-surface-200 px-2 py-1 text-xs text-slate-100 placeholder:text-slate-500 focus:border-slate-500 focus:outline-none"
        />
        <ArrowRight className="h-3.5 w-3.5 shrink-0 text-slate-500" aria-hidden />
        <input
          value={target}
          onChange={(e) => setTarget(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') submit()
          }}
          placeholder="target actor"
          aria-label="path target actor"
          className="min-w-0 flex-1 rounded border border-slate-700 bg-surface-200 px-2 py-1 text-xs text-slate-100 placeholder:text-slate-500 focus:border-slate-500 focus:outline-none"
        />
        <button
          type="button"
          onClick={submit}
          disabled={!source.trim() || !target.trim() || pathQ.isFetching}
          className="shrink-0 rounded border border-slate-600 bg-surface-100 px-2.5 py-1 text-xs font-medium text-slate-100 hover:bg-surface-200 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {pathQ.isFetching ? 'Finding…' : 'Find path'}
        </button>
      </div>

      {submitted && (
        <div className="mt-2 text-xs" data-testid="find-path-result">
          {pathQ.isFetching ? (
            <span className="text-slate-500">Searching the relationship graph…</span>
          ) : pathQ.isError ? (
            <span className="text-amber-300">
              Path lookup failed
              {pathQ.error instanceof ApiError ? ` (${pathQ.error.status})` : ''}.
            </span>
          ) : data && data.found ? (
            <div>
              <div className="flex flex-wrap items-center gap-1">
                {data.path.map((node, i) => {
                  const edge = i < data.path.length - 1 ? data.edges[i] : undefined
                  const isBroker = brokerNode != null && node === brokerNode
                  return (
                    <Fragment key={`${node}-${i}`}>
                      <button
                        type="button"
                        onClick={() => selectRow('entity', node, node, { origin: 'find-path' })}
                        title={isBroker ? 'broker on this path — select entity' : 'select entity'}
                        className={cn(
                          'rounded border px-1.5 py-0.5 font-mono hover:bg-surface-100',
                          isBroker
                            ? 'border-amber-500 text-amber-200'
                            : 'border-slate-700 text-slate-200',
                        )}
                      >
                        {node}
                      </button>
                      {edge && (
                        <button
                          type="button"
                          onClick={() =>
                            selectRow('entity', edge.target, edge.target, {
                              origin: 'find-path-edge',
                            })
                          }
                          title={`${edge.label || 'related'} → ${edge.target} (select entity)`}
                          className="inline-flex items-center gap-0.5 text-slate-400 hover:text-slate-200"
                        >
                          <ArrowRight className="h-3 w-3" aria-hidden />
                          <span className="font-mono">{edge.label || 'rel'}</span>
                        </button>
                      )}
                    </Fragment>
                  )
                })}
              </div>
              <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-slate-400">
                <span>
                  {data.length != null
                    ? `${data.length} hop${data.length === 1 ? '' : 's'}`
                    : `${data.path.length} nodes`}
                </span>
                {broker ? (
                  <span>
                    broker{' '}
                    <button
                      type="button"
                      onClick={() =>
                        selectRow('entity', broker.node, broker.node, {
                          origin: 'find-path-broker',
                        })
                      }
                      className="font-mono text-amber-200 hover:underline"
                    >
                      {broker.node}
                    </button>{' '}
                    · betweenness {broker.betweenness.toFixed(3)}
                  </span>
                ) : (
                  <span className="text-slate-500">no broker on this path</span>
                )}
              </div>
            </div>
          ) : (
            <span className="text-slate-400">
              {data?.detail?.trim() || 'no path between these actors'}
            </span>
          )}
        </div>
      )}
    </div>
  )
}

/**
 * The node-graph lens — a small claim graph of one read: the read at the
 * centre, the country it assesses (a distinct node only for a finding read —
 * a country read IS the country), and a node per cited-evidence signal. Reuses
 * the ego-graph's crash-safe cytoscape mount (#90).
 */
export function ReadGraphLens({ read, citations }: { read: LensRead; citations: Citation[] }) {
  const cyRef = useRef<Core | null>(null)
  const fitCleanup = useRef<(() => void) | null>(null)
  const { ref: canvasRef, visible } = useVisibleSize<HTMLDivElement>()

  const elements = useMemo<ElementDefinition[]>(() => {
    const clip = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}…` : s)
    const centerColor = read.kind === 'finding' ? LENS_NODE_COLOR.finding : LENS_NODE_COLOR.target
    const nodes: ElementDefinition[] = [
      {
        group: 'nodes',
        data: {
          id: `read:${read.id}`,
          label: clip(read.label, 40),
          color: centerColor,
          size: 48,
          is_center: true,
          selKind: read.kind,
          selId: read.id,
          name: read.label,
        },
      },
    ]
    const edges: ElementDefinition[] = []
    if (read.kind === 'finding' && read.targetId) {
      nodes.push({
        group: 'nodes',
        data: {
          id: `target:${read.targetId}`,
          label: read.targetId,
          color: LENS_NODE_COLOR.target,
          size: 30,
          selKind: 'target',
          selId: read.targetId,
          name: read.targetId,
        },
      })
      edges.push({
        group: 'edges',
        data: {
          id: 'e-target',
          source: `read:${read.id}`,
          target: `target:${read.targetId}`,
          w: 2,
          color: '#475569',
          label: 'assesses',
        },
      })
    }
    const seen = new Set<string>()
    for (const c of citations) {
      if (seen.has(c.refId)) continue
      seen.add(c.refId)
      const lbl = c.title && c.title.trim() ? c.title : c.marker
      // Kind-aware evidence node: a composition cites a FINDING (sub-claim), a unit
      // cites a SIGNAL. Drill to the right record kind — never a phantom signal for
      // a finding-ref (the id prefix + selKind follow c.refKind).
      const nodeId = `${c.refKind === 'finding' ? 'finding' : 'sig'}:${c.refId}`
      nodes.push({
        group: 'nodes',
        data: {
          id: nodeId,
          label: clip(lbl, 36),
          color: LENS_NODE_COLOR.signal,
          size: 24,
          selKind: c.refKind,
          selId: c.refId,
          name: c.title ?? c.refId,
        },
      })
      edges.push({
        group: 'edges',
        data: {
          id: `e-${c.refId}`,
          source: `read:${read.id}`,
          target: nodeId,
          w: 2,
          color: '#475569',
          label: 'cites',
        },
      })
    }
    return [...nodes, ...edges]
  }, [read, citations])

  const onCyReady = useCallback((cy: Core) => {
    cyRef.current = cy
    cy.removeAllListeners()
    cy.on('tap', 'node', (evt) => {
      const node = evt.target
      selectRow(
        String(node.data('selKind')),
        String(node.data('selId')),
        node.data('name') ?? node.data('label'),
        { origin: 'read-graph-lens' },
      )
    })
    fitCleanup.current?.()
    fitCleanup.current = attachFitOnResize(cy, { layout: CONCENTRIC_LAYOUT, padding: 24 })
  }, [])

  useEffect(() => () => fitCleanup.current?.(), [])

  // Centre-only (no target, no citations) ⇒ nothing to graph — but the find-path
  // verb works over the WHOLE relationship graph, so keep it mounted above an
  // honest empty state rather than blanking the lens.
  const graphRegion =
    elements.length <= 1 ? (
      <Centered>
        <div>No cited evidence to graph for this read</div>
        <div className="mt-1 font-mono text-xs text-slate-500">{read.label}</div>
      </Centered>
    ) : (
      <div
        ref={canvasRef}
        className="relative h-full w-full min-h-[300px] bg-surface-200"
        data-testid="read-graph-lens"
      >
        {visible && (
          <CytoscapeComponent
            cy={onCyReady}
            elements={elements}
            stylesheet={STYLESHEET}
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

  return (
    <div
      className="flex h-full w-full min-h-0 flex-col"
      data-testid="read-graph-lens-wrap"
    >
      <FindPathControl />
      <div className="min-h-0 flex-1">{graphRegion}</div>
    </div>
  )
}

// --- read resolution + evidence pool ---------------------------------------

interface ReadResolved {
  targetId: string | null
  citations: Citation[]
}

/** A `/findings` row as the resolver reads it (envelope under `data`). */
interface FindingEnvelopeRow {
  id: string
  analyst_id?: string | null
  produced_at?: string
  title?: string | null
  data?: unknown
}

/** The lineage-walk root the finding read resolves through (shared endpoint —
 *  a minimal local shape so this stays decoupled from the lineage graph model). */
interface LineageRootResp {
  root?: {
    body?: Record<string, unknown> | null
    target_id?: string | null
    title?: string | null
  }
}

/** A `/signals` pool row — the geo fields ({@link SignalGeoRow}) plus the
 *  timeline fields the lens adapts to {@link TLSignal}. */
interface PoolSignal extends SignalGeoRow {
  category?: string | null
  produced_at?: string
  event_timestamp?: string | null
}

interface EvidencePool {
  signals: PoolSignal[]
  findings: TLFinding[]
  situations: TLSituation[]
}

/** Resolve a read to its country (`target_id`) + cited-evidence citations. A
 *  country read pulls the newest `country_composition` finding (the P3 verified
 *  synthesis); a finding read walks the lineage root. Both degrade to an empty
 *  citation list, never throw. */
async function resolveRead(read: LensRead): Promise<ReadResolved> {
  if (read.kind === 'target') {
    try {
      const page = await apiGet<{ data: FindingEnvelopeRow[] }>(
        `/findings?analyst_id=country_composition&target_id=${encodeURIComponent(read.id)}&limit=5`,
      )
      let newest: FindingEnvelopeRow | null = null
      for (const r of page.data ?? []) {
        if (!newest || Date.parse(r.produced_at ?? '') > Date.parse(newest.produced_at ?? '')) {
          newest = r
        }
      }
      const body =
        newest && newest.data && typeof newest.data === 'object'
          ? (newest.data as Record<string, unknown>)
          : null
      return { targetId: read.id, citations: extractCitations(body) }
    } catch {
      return { targetId: read.id, citations: [] }
    }
  }
  try {
    const rep = await apiGet<LineageRootResp>(
      `/lineage/finding/${encodeURIComponent(read.id)}?direction=upstream&depth=6`,
    )
    const root = rep.root
    const body = root?.body && typeof root.body === 'object' ? root.body : null
    return { targetId: root?.target_id ?? null, citations: extractCitations(body) }
  } catch {
    return { targetId: null, citations: [] }
  }
}

/** Fetch a country's evidence pool (signals + findings + situations). Each leg
 *  degrades to empty so one missing read surface can't blank the lens. */
async function fetchEvidencePool(targetId: string): Promise<EvidencePool> {
  const enc = encodeURIComponent(targetId)
  const [sig, fin, sit] = await Promise.all([
    apiGet<{ data: PoolSignal[] }>(`/signals?target_id=${enc}&limit=200`).catch(() => ({
      data: [] as PoolSignal[],
    })),
    apiGet<{ data: TLFinding[] }>(`/findings?target_id=${enc}&limit=100`).catch(() => ({
      data: [] as TLFinding[],
    })),
    apiGet<{ data: TLSituation[] }>(`/situations?target_id=${enc}&limit=100`).catch(() => ({
      data: [] as TLSituation[],
    })),
  ])
  return { signals: sig.data ?? [], findings: fin.data ?? [], situations: sit.data ?? [] }
}

/** Adapt a pool signal row to the pure timeline-point input shape. */
function adaptSignalTL(s: PoolSignal): TLSignal {
  return {
    id: s.id,
    title: s.title ?? s.id,
    category: s.category ?? '',
    produced_at: s.produced_at ?? '',
    published_at: s.event_timestamp ?? null,
  }
}

function LensTab({
  active,
  onClick,
  icon: Icon,
  label,
}: {
  active: boolean
  onClick: () => void
  icon: typeof Clock
  label: string
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        'inline-flex items-center gap-1.5 rounded border px-2.5 py-1 text-xs font-medium',
        active
          ? 'border-slate-600 bg-surface-100 text-slate-100'
          : 'border-transparent text-slate-400 hover:bg-surface-200 hover:text-slate-200',
      )}
    >
      <Icon className="h-3.5 w-3.5" aria-hidden />
      {label}
    </button>
  )
}

function LensState({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-full w-full items-center justify-center bg-surface-300 px-4 text-center text-sm text-slate-500">
      {children}
    </div>
  )
}

/**
 * The lens SWITCHER over the current read — Temporal (timeline + geo) and Node
 * graph, both scoped to the read's evidence and brushing the same global
 * selection. Reads the global selection (or an optional `selection` prop) and
 * anchors to the last country/finding read so drilling into a cited signal
 * doesn't tear the lens off its read.
 *
 * WIRING: render `<ReadLenses />` in the Why room (it is self-contained — it
 * reads the selection store, fetches the read's evidence, and publishes the
 * `worldState.readScope` so the World map brushes the same read).
 */
export function ReadLenses({ selection }: { selection?: Selection | null }) {
  const storeSel = useSelection((s) => s.selection)
  const sel = selection !== undefined ? selection : storeSel
  const setReadScope = useWorldState((s) => s.setReadScope)
  const [read, setRead] = useState<LensRead | null>(null)
  const [lens, setLens] = useState<'temporal' | 'graph'>('temporal')

  // Anchor to the last country/finding selection (sticky read).
  useEffect(() => {
    if (sel && (sel.kind === 'target' || sel.kind === 'finding')) {
      setRead({ kind: sel.kind, id: sel.id, label: sel.label ?? sel.id, targetId: null })
    }
  }, [sel])

  const selectedId = sel?.id ?? null

  const resolveQ = useQuery<ReadResolved>({
    enabled: !!read,
    queryKey: ['read-lens-resolve', read?.kind ?? '', read?.id ?? ''],
    queryFn: () => resolveRead(read as LensRead),
    retry: false,
  })
  const resolved = resolveQ.data ?? null
  const targetId = resolved?.targetId ?? null
  const citations = useMemo(() => resolved?.citations ?? [], [resolved])
  const evidenceIds = useMemo(() => new Set(citations.map((c) => c.refId)), [citations])

  const poolQ = useQuery<EvidencePool>({
    enabled: !!targetId,
    queryKey: ['read-lens-pool', targetId ?? ''],
    queryFn: () => fetchEvidencePool(targetId as string),
    retry: false,
  })
  const pool = poolQ.data ?? null

  const timelinePts = useMemo<TimelinePoint[]>(() => {
    if (!pool) return []
    return [
      ...signalPoints(pool.signals.map(adaptSignalTL)),
      ...findingPoints(pool.findings),
      ...situationPoints(pool.situations),
    ]
  }, [pool])
  const geoPts = useMemo<GeoPoint[]>(() => (pool ? signalGeoPoints(pool.signals) : []), [pool])

  // Publish the active read + evidence so the World map brushes the same read.
  useEffect(() => {
    if (!read || !resolved) return
    setReadScope({
      kind: read.kind,
      id: read.id,
      targetId,
      label: read.label,
      signalIds: citations.map((c) => c.refId),
    })
    return () => setReadScope(null)
  }, [read, resolved, targetId, citations, setReadScope])

  if (!read) {
    return <LensState>Select a country or a finding to open its temporal &amp; graph lenses.</LensState>
  }

  const graphRead: LensRead = { ...read, targetId }

  return (
    <div className="flex h-full w-full min-h-0 flex-col bg-surface-300" data-testid="read-lenses">
      <div className="flex shrink-0 items-center gap-1 border-b border-slate-800 px-3 py-2">
        <LensTab active={lens === 'temporal'} onClick={() => setLens('temporal')} icon={Clock} label="Temporal" />
        <LensTab active={lens === 'graph'} onClick={() => setLens('graph')} icon={Share2} label="Node graph" />
        <div className="ml-auto min-w-0 truncate text-xs text-slate-500" title={read.label}>
          {citations.length} cited{targetId ? ` · ${targetId}` : ''}
        </div>
      </div>
      <div className="min-h-0 flex-1">
        {lens === 'temporal' ? (
          resolveQ.isLoading || poolQ.isLoading ? (
            <LensState>Loading the read&rsquo;s evidence…</LensState>
          ) : (
            <div className="flex h-full w-full min-h-0 flex-col">
              <div className="min-h-0 flex-1 border-b border-slate-800">
                <ReadGeoLens points={geoPts} evidenceIds={evidenceIds} selectedId={selectedId} />
              </div>
              <div className="h-[44%] min-h-[150px]">
                <ReadTimelineLens points={timelinePts} evidenceIds={evidenceIds} selectedId={selectedId} />
              </div>
            </div>
          )
        ) : resolveQ.isLoading ? (
          <LensState>Loading the read&rsquo;s evidence…</LensState>
        ) : (
          <ReadGraphLens read={graphRead} citations={citations} />
        )}
      </div>
    </div>
  )
}
