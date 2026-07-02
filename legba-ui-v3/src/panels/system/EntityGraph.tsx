/**
 * Entity Graph (`system.entity_graph`) — the entity knowledge-graph viz.
 *
 * Source-first analogue of v2's Sigma.js knowledge graph (the old "Knowledge
 * Graph" — recovered screenshot 19). Renders GET /api/v1/entities/graph
 * (entity_profiles nodes + proposed_edges relationships) with Cytoscape:
 *   - NODES coloured by entity_class, sized by degree; labels gated to hubs
 *   - EDGES coloured by relationship_type, with a matching colour legend
 *   - entity-type FILTER CHIPS toggle node classes (and their dangling edges)
 *   - degree-0 orphans dropped by default (toggle to show) so it reads as a
 *     connected network, not confetti
 *   - top-N densest subgraph by default; click a node to ego-center on it
 *   - listens for the shared selection (entity) to center
 *
 * The shaping (orphan drop, degree sizing, class/rel filtering) lives in the
 * pure `buildEntityGraphElements` in `@/lib/graphModel`; the chips/legend/zoom
 * chrome is the shared `GraphControls`. The cytoscape mount itself is unchanged
 * from the #90 crash fix: a stable no-op preset mount layout + the real `cose`
 * run from the resize observer (attachFitOnResize) once the container is sized,
 * gated on `useVisibleSize`.
 */
import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState } from 'react'
import type cytoscape from 'cytoscape'
import type { Core, ElementDefinition, StylesheetStyle } from 'cytoscape'
import CytoscapeComponent from 'react-cytoscapejs'
import { PanelChrome } from '@/components/PanelChrome'
import { GraphControls } from '@/components/GraphControls'
import { apiGet } from '@/lib/api'
import { attachFitOnResize, useVisibleSize } from '@/lib/cytoscapeFit'
import { isCountry } from '@/lib/countryGeo'
import {
  buildEntityGraphElements,
  entityClassColor,
  presentEntityClasses,
  presentRelationshipTypes,
  relationshipColor,
  type EntityGraphEdge,
  type EntityGraphNode,
} from '@/lib/graphModel'
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

const STYLESHEET: StylesheetStyle[] = [
  {
    selector: 'node',
    style: {
      'background-color': 'data(color)',
      // Every node carries its label; `min-zoomed-font-size` declutters by hiding
      // labels when the graph is zoomed out, rather than dropping them outright for
      // low-degree nodes (which left most of the graph unlabelled).
      label: 'data(label)',
      'font-size': 10,
      color: '#e2e8f0', // ink-1
      'text-valign': 'bottom',
      'text-halign': 'center',
      'text-margin-y': 3,
      'text-outline-color': '#0a0c10', // surf-base
      'text-outline-width': 2,
      // Low threshold so the hub labels (full graph) and every label (ego view)
      // actually render at the graph's fit-zoom instead of being culled.
      'min-zoomed-font-size': 4,
      width: 'data(size)',
      height: 'data(size)',
      'border-width': 1,
      'border-color': '#1e293b', // slate-800
    },
  },
  {
    selector: 'edge',
    style: {
      width: 'data(w)',
      // Relationship label (set per-edge: only in ego view). bezier, not haystack
      // — haystack edges have no midpoint so they can't carry a label.
      label: 'data(label)',
      'line-color': 'data(color)',
      'curve-style': 'bezier',
      'font-size': 9,
      color: '#cbd5e1', // slate-300 edge labels
      'text-rotation': 'autorotate',
      'text-outline-color': '#0a0c10', // surf-base
      'text-outline-width': 2,
      'min-zoomed-font-size': 4,
      opacity: 0.7,
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

// #6 — NER emits ARTIFACT nodes (a lone timestamp like "9:33AM AKDT" classed as
// TIME, bare quantities/ordinals, one-off mentions) that are noise in a
// knowledge graph. These filter AT THE FETCH — not a client-side hide — so every
// downstream count (subtitle, chips, legend) reflects the honest de-junked set.
// A node survives when its class is a MEANINGFUL entity class AND it clears a
// small mention-count floor; a recognized country or the centered ego node is
// always kept.
const ENTITY_CLASS_ALLOW = new Set([
  'person', 'org', 'organization', 'gpe', 'geo', 'geopolitical', 'country',
  'nation', 'location', 'loc', 'norp', 'nationality', 'fac', 'facility',
  'event', 'law', 'product', 'work_of_art', 'group', 'entity',
])
const JUNK_ENTITY_CLASS = new Set([
  'date', 'time', 'percent', 'money', 'quantity', 'ordinal', 'cardinal', 'number',
])
const MIN_MENTIONS = 2

/** Drop NER-artifact nodes + their dangling edges from a raw graph response. */
function filterGraphJunk(g: GraphResp, center: string | null): GraphResp {
  const keep = (n: GNode): boolean => {
    if (center && n.canonical_name === center) return true
    const cls = (n.entity_class ?? '').toLowerCase()
    if (JUNK_ENTITY_CLASS.has(cls)) return false
    if (isCountry(n.canonical_name)) return true // countries are meaningful at any count
    if (!ENTITY_CLASS_ALLOW.has(cls)) return false
    return (n.mentions ?? 0) >= MIN_MENTIONS
  }
  const nodes = g.nodes.filter(keep)
  // Keep an edge only when BOTH endpoints survived (match on either key the
  // backend might use — canonical_name or id — so we never over-drop).
  const alive = new Set<string>()
  for (const n of nodes) {
    alive.add(n.canonical_name)
    alive.add(n.id)
  }
  const edges = g.edges.filter((e) => alive.has(e.source) && alive.has(e.target))
  return { nodes, edges }
}

export default function EntityGraphPanel({ registration }: PanelProps) {
  const [center, setCenter] = useState<string | null>(null)
  // Filter state: hidden entity classes + show-orphans toggle. `null` ⇒ no class
  // is hidden (all chips active) — the default.
  const [hiddenClasses, setHiddenClasses] = useState<Set<string>>(() => new Set())
  const [showOrphans, setShowOrphans] = useState(false)
  const cyRef = useRef<Core | null>(null)
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
    queryFn: async () => {
      const g = await apiGet<GraphResp>(
        `/entities/graph?limit=80${center ? `&center=${encodeURIComponent(center)}` : ''}`,
      )
      // #6 — de-junk at the fetch so the counts downstream stay honest.
      return filterGraphJunk(g, center)
    },
    refetchInterval: 120_000,
  })

  // Normalise the raw response into the projector's structural node/edge shape.
  // NER classes country mentions as generic `entity`; recolor recognized
  // countries as `country` (lib/countryGeo) so the graph isn't all grey.
  const { rawNodes, rawEdges } = useMemo(() => {
    const g = graphQ.data
    if (!g) return { rawNodes: [] as EntityGraphNode[], rawEdges: [] as EntityGraphEdge[] }
    const rawNodes: EntityGraphNode[] = g.nodes.map((n) => ({
      id: n.canonical_name,
      label: n.canonical_name,
      entity_class:
        n.entity_class === 'entity' && isCountry(n.canonical_name) ? 'country' : n.entity_class,
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

  // Chips read the FULL (unfiltered) class set so a class never vanishes from the
  // chip row just because it's currently toggled off. The legend reads the
  // currently-rendered edges so it tracks what's on screen.
  const allClasses = useMemo(() => presentEntityClasses(rawNodes), [rawNodes])
  const visibleClasses = useMemo(
    () => new Set(allClasses.filter((c) => !hiddenClasses.has(c))),
    [allClasses, hiddenClasses],
  )

  const graph = useMemo(
    () =>
      buildEntityGraphElements(rawNodes, rawEdges, {
        visibleClasses: hiddenClasses.size > 0 ? visibleClasses : undefined,
        showOrphans,
      }),
    [rawNodes, rawEdges, hiddenClasses, visibleClasses, showOrphans],
  )

  const elements = useMemo<ElementDefinition[]>(
    () => [
      ...graph.nodes.map((n) => ({
        // Full graph: label hubs only (the overview stays legible). Ego view (a
        // node is centred): label EVERY node — it's the focused detail view the
        // operator drilled into. min-zoomed-font-size still gates legibility.
        data: { ...n.data, label: center || n.data.show_label ? n.data.label : '' },
      })),
      ...graph.edges.map((e) => ({
        // Relationship labels only in the focused ego view — labelling every edge
        // of the full top-subgraph would be an unreadable wall.
        data: { ...e.data, label: center ? e.data.relationship_type : '' },
      })),
    ],
    [graph, center],
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

  return (
    <PanelChrome
      registration={registration}
      subtitle={
        center
          ? `ego: ${center} · ${graph.nodes.length} nodes`
          : `top subgraph · ${graph.nodes.length} nodes / ${graph.edges.length} edges`
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
      <div ref={canvasRef} className="relative h-full w-full min-h-[300px]" data-testid="entity-graph-canvas">
        {graphQ.isLoading && (
          <div className="absolute inset-0 z-10 flex items-center justify-center text-slate-500 text-sm">loading graph…</div>
        )}
        {!graphQ.isLoading && elements.length === 0 && (
          <div className="absolute inset-0 z-10 flex items-center justify-center text-slate-500 text-sm" data-testid="entity-graph-empty">
            no entity relationships yet
          </div>
        )}
        {/* Chips + legend + zoom overlay — rendered whenever there's a graph. */}
        {!graphQ.isLoading && elements.length > 0 && (
          <GraphControls
            chipsLabel="Types"
            legendLabel="Edges"
            chips={allClasses.map((c) => ({ id: c, color: entityClassColor(c) }))}
            activeChips={visibleClasses}
            onToggleChip={toggleClass}
            onSelectAllChips={() => setHiddenClasses(new Set())}
            onClearChips={() => setHiddenClasses(new Set(allClasses))}
            legend={presentRels.map((r) => ({ id: r, color: relationshipColor(r) }))}
            showOrphans={showOrphans}
            onToggleOrphans={() => setShowOrphans((v) => !v)}
            onZoomIn={() => cyRef.current?.zoom(cyRef.current.zoom() * 1.3)}
            onZoomOut={() => cyRef.current?.zoom(cyRef.current.zoom() / 1.3)}
            onFit={() => cyRef.current?.fit(undefined, 30)}
          />
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
            userZoomingEnabled
            userPanningEnabled
            minZoom={0.2}
            maxZoom={3}
            cy={(cy: Core) => {
              cyRef.current = cy
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
