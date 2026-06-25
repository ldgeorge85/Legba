/**
 * The Why — `why:lineage` (the provenance DAG view).
 *
 * Renders the derivation graph for one row by walking the lineage API
 * (`GET /lineage/{kind}/{id}?direction=both&depth=4`) and projecting the
 * `LineageReport` into cytoscape elements via `@/lib/graphModel`. This is the
 * "why does the system believe this" surface: a node is a produced row
 * (signal → finding → situation …), an edge is a *real* parent→child
 * derivation tuple (NOT synthesized).
 *
 * Reuse: projection + palette live in `@/lib/graphModel` (`projectGraph`,
 * `kindColor`, `GraphNodeData` / `GraphEdgeData`). The cytoscape stylesheet +
 * layout mirror the Tier-B Target Graph panel (`src/panels/target/Graph.tsx`)
 * so this reads as native, swapping that panel's cross-window `legba:*` event
 * for the v4 shared selection store (`@/state/selection`).
 */

import { useQuery } from '@tanstack/react-query'
import type { Core, ElementDefinition, LayoutOptions, StylesheetStyle } from 'cytoscape'
import { Loader2, GitBranch } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef } from 'react'
import CytoscapeComponent from 'react-cytoscapejs'
import { apiGet, ApiError } from '@/lib/api'
import { cn } from '@/lib/cn'
import { attachFitOnResize } from '@/lib/cytoscapeFit'
import { projectGraph, type LineageReport } from '@/lib/graphModel'
import { useSelection, type SelectionKind } from '@/state/selection'

interface LineageGraphProps {
  /** Lineage row kind for the walk root (e.g. 'finding', 'situation', 'signal'). */
  kind: string
  /** Row id for the walk root. */
  id: string
}

/** The v4 selection store is a closed union; lineage row_kinds are broader. Only
 *  `finding`/`situation` round-trip; everything else selects as a `finding`
 *  (the nearest lineage-walkable family — mirrors `SELECTION_TO_ROW_KIND`). */
const ROW_KIND_TO_SELECTION: Record<string, SelectionKind> = {
  finding: 'finding',
  meta_finding: 'finding',
  situation: 'situation',
  signal: 'finding',
}
function toSelectionKind(rowKind: string): SelectionKind {
  return ROW_KIND_TO_SELECTION[rowKind] ?? 'finding'
}

/**
 * Cytoscape stylesheet — dark canvas, nodes colored by `row_kind` (the
 * projection bakes the hex into `data(color)`), edges colored by produced
 * `rel`. Mirrors `panels/target/Graph.tsx`: slate node labels with an outline
 * so they read over any node color, the root enlarged + amber-ringed, and a
 * selected node emerald-ringed. Kept module-level (stable identity).
 */
const STYLESHEET: StylesheetStyle[] = [
  {
    selector: 'node',
    style: {
      'background-color': 'data(color)',
      label: 'data(label)',
      color: '#e2e8f0', // slate-200
      'font-size': 10,
      'text-valign': 'bottom',
      'text-halign': 'center',
      'text-margin-y': 4,
      'text-outline-color': '#0f1115',
      'text-outline-width': 2,
      'text-max-width': '120px',
      'text-wrap': 'ellipsis',
      width: 18,
      height: 18,
      'border-width': 1,
      'border-color': '#1e293b', // slate-800
    },
  },
  {
    selector: 'node[?is_root]',
    style: {
      width: 28,
      height: 28,
      'border-width': 2,
      'border-color': '#f59e0b', // amber-500
      'font-size': 12,
      'font-weight': 700,
    },
  },
  {
    selector: 'edge',
    style: {
      width: 1.5,
      'line-color': '#475569', // slate-600
      'target-arrow-color': 'data(color)',
      'target-arrow-shape': 'triangle',
      'curve-style': 'bezier',
      'arrow-scale': 0.8,
      opacity: 0.75,
    },
  },
  {
    selector: 'node:selected',
    style: { 'border-color': '#10b981', 'border-width': 3 }, // emerald-500
  },
]

/** Breadthfirst rooted at the report root reads the provenance DAG top-down;
 *  cytoscape falls back gracefully when the root id is absent. */
function layoutFor(rootId: string | undefined): LayoutOptions {
  return {
    name: 'breadthfirst',
    directed: true,
    roots: rootId ? [rootId] : undefined,
    spacingFactor: 1.1,
    padding: 24,
    animate: false,
    fit: true,
  } as LayoutOptions
}

export default function LineageGraph({ kind, id }: LineageGraphProps) {
  const select = useSelection((s) => s.select)
  const cyRef = useRef<Core | null>(null)
  const fitCleanup = useRef<(() => void) | null>(null)

  const lineageQ = useQuery<LineageReport | undefined>({
    enabled: !!kind && !!id,
    queryKey: ['why-lineage', kind, id],
    queryFn: async () => {
      try {
        return await apiGet<LineageReport>(
          `/lineage/${encodeURIComponent(kind)}/${encodeURIComponent(id)}?direction=both&depth=4`,
        )
      } catch (e) {
        // A finding/row with no lineage-graph node yet (e.g. no derived_from
        // edges) legitimately 404s — render an empty graph ("no lineage yet")
        // rather than throwing a console error.
        if (e instanceof ApiError && e.status === 404) return undefined
        throw e
      }
    },
  })

  const report = lineageQ.data

  const elements = useMemo<ElementDefinition[]>(() => {
    const g = projectGraph(report)
    return [
      ...g.nodes.map((n) => ({ group: 'nodes' as const, data: n.data })),
      ...g.edges.map((e) => ({ group: 'edges' as const, data: e.data })),
    ]
  }, [report])

  const layout = useMemo(() => layoutFor(report?.root.id), [report?.root.id])

  // CytoscapeComponent only applies `layout` on mount; re-run it whenever the
  // element set changes so a re-rooted walk relays out + fits.
  useEffect(() => {
    const cy = cyRef.current
    if (!cy || elements.length === 0) return
    cy.layout(layout).run()
  }, [elements, layout])

  const onCyReady = useCallback(
    (cy: Core) => {
      cyRef.current = cy
      cy.removeAllListeners()
      cy.on('tap', 'node', (evt) => {
        const node = evt.target
        const rowKind = String(node.data('row_kind') ?? 'finding')
        select({
          kind: toSelectionKind(rowKind),
          id: String(node.data('id')),
          label: String(node.data('title') ?? node.data('label') ?? node.data('id')),
        })
      })
      // #90 — keep the canvas sized/fitted to its Dockview tab (fixes blank graph).
      fitCleanup.current?.()
      fitCleanup.current = attachFitOnResize(cy)
    },
    [select],
  )

  const hasElements = elements.length > 0

  return (
    <div className="relative h-full w-full bg-surface-300 border border-slate-800 rounded overflow-hidden">
      {/* Canvas — always mounted so cytoscape has a sized container; the
          overlays below sit on top for loading / empty / error. */}
      {hasElements && (
        <CytoscapeComponent
          cy={onCyReady}
          elements={elements}
          stylesheet={STYLESHEET}
          layout={layout}
          style={{ width: '100%', height: '100%' }}
          minZoom={0.2}
          maxZoom={3}
        />
      )}

      {lineageQ.isLoading && (
        <Overlay>
          <Loader2 size={18} className="animate-spin text-slate-500" />
          <span className="text-slate-400 text-sm">walking lineage…</span>
        </Overlay>
      )}

      {!lineageQ.isLoading && lineageQ.error instanceof Error && (
        <Overlay>
          <span className="text-rose-400 text-sm">lineage error: {lineageQ.error.message}</span>
          <button
            type="button"
            onClick={() => lineageQ.refetch()}
            className="text-xs text-slate-400 hover:text-slate-200 underline underline-offset-2"
          >
            retry
          </button>
        </Overlay>
      )}

      {!lineageQ.isLoading && !lineageQ.error && !hasElements && (
        <Overlay>
          <GitBranch size={18} className="text-slate-600" />
          <span className="text-slate-500 text-sm">no lineage</span>
          <span className="text-slate-600 text-xs">
            this {kind} has no recorded derivations
          </span>
        </Overlay>
      )}

      {/* Provenance hint — only while the graph is live. */}
      {hasElements && (
        <div
          className={cn(
            'pointer-events-none absolute bottom-2 left-2 z-10',
            'text-[10px] text-slate-500',
          )}
        >
          click a node to select it across rooms · root ringed amber
        </div>
      )}
    </div>
  )
}

/** Centered overlay for non-graph states (loading / empty / error). */
function Overlay({ children }: { children: React.ReactNode }) {
  return (
    <div className="absolute inset-0 z-20 flex flex-col items-center justify-center gap-2 bg-surface-300/60">
      {children}
    </div>
  )
}
