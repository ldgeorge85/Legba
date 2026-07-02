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
 * Reuse: projection + palette live in `@/lib/graphModel`
 * (`buildLineageElements` for the depth/kind-filtered projection, `kindColor`,
 * `presentRowKinds`, `relationshipTypes`). The cytoscape stylesheet + layout
 * mirror the Tier-B Target Graph panel (`src/panels/target/Graph.tsx`) so this
 * reads as native, swapping that panel's cross-window `legba:*` event for the
 * v4 shared selection store (`@/state/selection`). A lighter `GraphControls`
 * overlay supplies row-kind filter chips + a derivation-step legend + zoom.
 */

import { useQuery } from '@tanstack/react-query'
import type { Core, ElementDefinition, LayoutOptions, StylesheetStyle } from 'cytoscape'
import { Loader2, GitBranch, ShieldCheck, ExternalLink, Plus, Minus } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import CytoscapeComponent from 'react-cytoscapejs'
import { apiGet, ApiError } from '@/lib/api'
import { cn } from '@/lib/cn'
import { attachFitOnResize, useVisibleSize } from '@/lib/cytoscapeFit'
import { GraphControls } from '@/components/GraphControls'
import {
  buildPrimaryTrail,
  buildProgressiveLineageElements,
  kindColor,
  presentRowKinds,
  relationshipTypes,
  RECEIPT_BADGE,
  type LineageNode,
  type LineageReport,
} from '@/lib/graphModel'
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

// #90 — stable no-op mount layout (fit:false so it can't dereference the bounding
// box at 0×0 → no `reading 'h'` crash). Module constant so react-cytoscapejs's
// `patchLayout` doesn't re-run it every render. The real `breadthfirst` runs from
// the resize observer (attachFitOnResize) + the size-guarded re-root effect.
const PRESET_NOOP = { name: 'preset', fit: false, animate: false } as LayoutOptions

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
  // Lineage row-kind filter (the lighter chip row): hidden row_kinds. The root
  // always renders; hiding an intermediate kind re-parents its children to the
  // nearest visible ancestor (buildLineageElements) so the DAG stays connected.
  const [hiddenKinds, setHiddenKinds] = useState<Set<string>>(() => new Set())
  // P1-T5 — progressive one-hop-at-a-time reveal: how many derivation rings (by
  // `depth`) are currently shown. Starts at the FIRST hop (root + its immediate
  // parents) and grows one ring per "expand", so the DAG is walkable a step at a
  // time down to the source rather than dumping the full graph at once.
  const [revealedDepth, setRevealedDepth] = useState(1)
  // #90 — defer constructing cytoscape until this surface is on-screen + sized;
  // a fresh mount in a hidden 0×0 Dockview tab crashes on the auto-layout.
  const { ref: canvasRef, visible } = useVisibleSize<HTMLDivElement>()

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

  // The FULL row-kind set (drives the chip row) is read from the report so a
  // kind never vanishes from the chips just because it's toggled off.
  const allKinds = useMemo(() => presentRowKinds(report), [report])
  const visibleKinds = useMemo(
    () => new Set(allKinds.filter((k) => !hiddenKinds.has(k))),
    [allKinds, hiddenKinds],
  )

  // P1-T5 — the progressive projection: the DAG bounded to `revealedDepth` rings.
  // Reuses the depth-gated, orphan-safe `buildLineageElements`, so hiding a kind
  // still re-parents onto the nearest visible ancestor and the root always shows.
  const progressive = useMemo(
    () =>
      buildProgressiveLineageElements(report, revealedDepth, {
        visibleKinds: hiddenKinds.size > 0 ? visibleKinds : undefined,
      }),
    [report, revealedDepth, hiddenKinds, visibleKinds],
  )
  const graph = progressive.elements

  // A NEW walk root restarts the reveal at the first hop.
  useEffect(() => {
    setRevealedDepth(1)
  }, [report?.root.id])

  // The honest one-line walk (root → source) for the side HUD: each revealed hop
  // shows its receipt badge; the deepest (signal) hop opens its real source URL.
  const trail = useMemo(() => buildPrimaryTrail(report), [report])
  const revealedTrail = useMemo(
    // oldest→newest from the helper; keep only revealed hops, show root-first.
    () => trail.filter((n) => n.depth <= progressive.revealedDepth).reverse(),
    [trail, progressive.revealedDepth],
  )
  const canExpand = progressive.revealedDepth < progressive.maxDepth

  const elements = useMemo<ElementDefinition[]>(
    () => [
      ...graph.nodes.map((n) => ({ group: 'nodes' as const, data: n.data })),
      ...graph.edges.map((e) => ({ group: 'edges' as const, data: e.data })),
    ],
    [graph],
  )

  // The legend keys to lineage edge kinds (a derivation edge's type = the child
  // row_kind that was produced), filtered to what's actually present.
  const presentRels = useMemo(() => relationshipTypes(report), [report])

  const toggleKind = (k: string) =>
    setHiddenKinds((prev) => {
      const next = new Set(prev)
      if (next.has(k)) next.delete(k)
      else next.add(k)
      return next
    })

  const layout = useMemo(() => layoutFor(report?.root.id), [report?.root.id])
  // `onCyReady` is a stable callback ([select]); keep a ref to the current layout
  // so the resize observer's one-time first run uses the live (current-root) one.
  const layoutRef = useRef<LayoutOptions>(layout)
  layoutRef.current = layout

  // CytoscapeComponent only applies `layout` on mount; re-run it whenever the
  // element set changes so a re-rooted walk relays out + fits. Guarded on a
  // non-zero container size: running a measuring layout at 0×0 (Dockview tab not
  // laid out yet) crashes on the undefined bounding box (#90). On the first
  // sized tick the resize observer runs the layout, so this is the re-root path.
  useEffect(() => {
    const cy = cyRef.current
    if (!cy || elements.length === 0) return
    const el = cy.container()
    if (!el || el.clientWidth === 0 || el.clientHeight === 0) return
    cy.layout(layout).run()
    cy.fit(undefined, 24)
  }, [elements, layout])

  // #90 — disconnect the resize observer on unmount so a pending tick never
  // touches a destroyed cy (the canvas unmounts when there are no elements).
  useEffect(() => () => fitCleanup.current?.(), [])

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
      // #90 — size/fit to the Dockview tab + run the real `breadthfirst` layout
      // once sized (running it at 0×0 crashes on the undefined bounding box). The
      // re-root path is the guarded useEffect above; `layoutRef` keeps that and
      // the resize-observer's first run pointed at the same (current-root) layout.
      fitCleanup.current?.()
      fitCleanup.current = attachFitOnResize(cy, { layout: layoutRef.current, padding: 24 })
    },
    [select],
  )

  const hasElements = elements.length > 0

  return (
    <div ref={canvasRef} className="relative h-full w-full min-h-[300px] bg-surface-300 border border-slate-800 rounded overflow-hidden">
      {/* #90 — construct cytoscape only once the tab is visible+sized; a fresh
          mount in a hidden 0×0 Dockview tab auto-runs cytoscape's default layout
          against a detached box → `reading 'h'`. The overlays below sit on top. */}
      {/* Lighter old-KG controls for the DAG: the "chips" are lineage row-kinds
          and the legend keys to lineage edge kinds (the produced row_kind). */}
      {hasElements && (
        <GraphControls
          variant="light"
          chipsLabel="Kinds"
          legendLabel="Steps"
          chips={allKinds.map((k) => ({ id: k, color: kindColor(k) }))}
          activeChips={visibleKinds}
          onToggleChip={toggleKind}
          onSelectAllChips={() => setHiddenKinds(new Set())}
          onClearChips={() => setHiddenKinds(new Set(allKinds))}
          legend={presentRels.map((r) => ({ id: r, color: kindColor(r) }))}
          onZoomIn={() => cyRef.current?.zoom(cyRef.current.zoom() * 1.3)}
          onZoomOut={() => cyRef.current?.zoom(cyRef.current.zoom() / 1.3)}
          onFit={() => cyRef.current?.fit(undefined, 24)}
        />
      )}

      {/* P1-T5 — the one-hop-at-a-time provenance walk (top-right, clear of the
          GraphControls chips top-left + the zoom cluster bottom-right). Each
          revealed hop shows its honest receipt badge; the source hop opens its
          real URL; "Expand" reveals the next hop down to the source. */}
      {hasElements && (
        <div className="pointer-events-auto absolute right-2 top-2 z-10 flex max-h-[72%] w-56 max-w-[72%] flex-col rounded-md border border-slate-800 bg-surface-300/95 p-2 text-xs backdrop-blur">
          <div className="mb-1.5 flex items-center justify-between gap-2">
            <span className="font-medium text-slate-300">Provenance walk</span>
            <span className="text-[10px] tabular-nums text-slate-500">
              {progressive.maxDepth === 0
                ? 'root only'
                : `hop ${progressive.revealedDepth} / ${progressive.maxDepth}`}
            </span>
          </div>
          <ol className="min-h-0 flex-1 space-y-1 overflow-y-auto pr-0.5">
            {revealedTrail.map((n) => (
              <li key={n.id} className="flex flex-col gap-0.5 border-l border-slate-800 pl-2">
                <button
                  type="button"
                  title={n.title ?? n.row_kind}
                  onClick={() =>
                    select({
                      kind: toSelectionKind(n.row_kind),
                      id: n.id,
                      label: n.title ?? n.id,
                    })
                  }
                  className="flex items-center gap-1.5 text-left hover:text-slate-100"
                >
                  <span
                    aria-hidden
                    className="h-2 w-2 shrink-0 rounded-full"
                    style={{ backgroundColor: kindColor(n.row_kind) }}
                  />
                  <span className="truncate text-slate-200">{n.title ?? n.row_kind}</span>
                </button>
                <ReceiptBadge node={n} />
              </li>
            ))}
          </ol>
          <div className="mt-2 flex items-center gap-1.5">
            <button
              type="button"
              disabled={!canExpand}
              onClick={() => setRevealedDepth((d) => d + 1)}
              className={cn(
                'inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-medium transition-colors',
                canExpand
                  ? 'border-slate-600 text-slate-200 hover:bg-surface-100'
                  : 'cursor-default border-slate-800 text-slate-600',
              )}
            >
              <Plus size={11} aria-hidden />
              {canExpand ? 'Expand next hop' : 'reached the source'}
            </button>
            {progressive.revealedDepth > 1 && (
              <button
                type="button"
                onClick={() => setRevealedDepth((d) => Math.max(1, d - 1))}
                className="inline-flex items-center gap-1 rounded border border-slate-700 px-1.5 py-0.5 text-[10px] text-slate-300 transition-colors hover:bg-surface-100"
              >
                <Minus size={11} aria-hidden />
                Collapse
              </button>
            )}
          </div>
        </div>
      )}

      {visible && hasElements && (
        <CytoscapeComponent
          cy={onCyReady}
          elements={elements}
          stylesheet={STYLESHEET}
          // #90 — stable no-op mount layout (fit:false); the real `breadthfirst`
          // runs once from the resize observer, after the tab sizes.
          layout={PRESET_NOOP}
          style={{ position: 'absolute', top: 0, right: 0, bottom: 0, left: 0 }}
          userZoomingEnabled
          userPanningEnabled
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
          one hop at a time — expand the walk to the source · root ringed amber
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

/**
 * The honest per-hop receipt indicator + source link for the provenance walk.
 *
 * Analyst hops carry a `receipt` — a SHA-256 chain *consistency* check the
 * backend RE-COMPUTES per node (NOT a signature). We render that node's own
 * `badge` verbatim (`'chain-consistent (single-node)'`) only when the re-hash
 * matched; a mismatch DEGRADES to "chain inconsistent" rather than fabricating a
 * green badge. Signal / source hops carry no receipt (`receipt=null`) and show
 * no badge — instead they open their real `canonical_url`, so the walk ends at a
 * clickable source and never dead-ends.
 */
function ReceiptBadge({ node }: { node: LineageNode }) {
  const r = node.receipt
  if (!r && !node.canonical_url) return null
  return (
    <div className="flex flex-wrap items-center gap-1 pl-3.5">
      {r &&
        (r.chain_consistent ? (
          <span
            className="inline-flex items-center gap-1 rounded bg-accent-ok/15 px-1 py-0.5 text-[9px] leading-none text-accent-ok"
            title={`receipt ${r.receipt_hash.slice(0, 12)}… · re-hash matched`}
          >
            <ShieldCheck size={9} aria-hidden />
            {r.badge || RECEIPT_BADGE}
          </span>
        ) : (
          <span
            className="inline-flex items-center gap-1 rounded bg-accent-critical/15 px-1 py-0.5 text-[9px] leading-none text-accent-critical"
            title={`receipt ${r.receipt_hash.slice(0, 12)}… · re-hash MISMATCH`}
          >
            <ShieldCheck size={9} aria-hidden />
            chain inconsistent
          </span>
        ))}
      {node.canonical_url && (
        <a
          href={node.canonical_url}
          target="_blank"
          rel="noopener noreferrer"
          title={node.canonical_url}
          className="inline-flex items-center gap-0.5 text-[9px] text-accent-info hover:underline"
        >
          <ExternalLink size={9} aria-hidden />
          open source
        </a>
      )}
    </div>
  )
}
