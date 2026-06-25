/**
 * T8. Target Graph (`target.graph`) — UI-3 (Tier B) entity / relationship graph.
 *
 * Scoped per target: the panel binds to `scope.target_id`, loads that
 * target's recent findings (`GET /api/v1/findings?target_id=…`) as the
 * candidate graph roots, and walks the lineage graph for the selected root
 * (`GET /api/v1/lineage/{kind}/{id}?direction=both&depth=4`). The lineage
 * report carries real `edges` (parent→child derivation) — we do NOT
 * synthesize them.
 *
 * v2 parity (Graph scored inspire-new) + UI-3 depth:
 *   - **depth slider (1..4)** — the walk fetches to depth 4 once, then the
 *     slider prunes client-side (no refetch on shrink);
 *   - **row-kind filter chips** — only kinds present in the walk render;
 *     toggling one off hides those nodes and *re-parents* their edges to the
 *     nearest visible ancestor so the graph stays one connected component
 *     (orphan-safe), the v2 "edge-type checkboxes" idea on the kind axis;
 *   - provenance-on-hover (row_kind / title / analyst_id / produced_at);
 *   - click a node → `legba:open-lineage` (drives the Lineage panel and
 *     re-roots this graph);
 *   - nodes + edges colored by row_kind.
 *
 * All projection lives in `@/lib/graphModel` (pure + unit-tested:
 * `buildLineageElements` / `presentRowKinds` / `toRowKind`). This component
 * is the cytoscape + react-query shell.
 */

import { useQuery } from '@tanstack/react-query'
import type cytoscape from 'cytoscape'
import type { Core, ElementDefinition, StylesheetStyle } from 'cytoscape'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import CytoscapeComponent from 'react-cytoscapejs'
import { PanelChrome } from '@/components/PanelChrome'
import { ScopePicker } from '@/components/ScopePicker'
import { apiGet, ApiError } from '@/lib/api'
import type { PanelProps } from '@/types'
import { selectRow, useSelection } from '@/state/selection'
import {
  KIND_COLORS,
  buildLineageElements,
  kindColor,
  presentRowKinds,
  toRowKind,
  type LineageReport,
  type RowKind,
} from '@/lib/graphModel'

/** Lineage walk depth bounds for the slider. */
const MIN_DEPTH = 1
const MAX_DEPTH = 4

interface FindingRow {
  id: string
  title: string
  severity: string | null
  produced_at: string
}
interface Page<T> {
  data: T[]
  next_cursor: string | null
}

interface TooltipState {
  x: number
  y: number
  title: string
  row_kind: string
  analyst_id: string | null
  produced_at: string
}

export default function TargetGraphPanel({ registration, scope }: PanelProps) {
  // The graph is target-scoped. It seeds from the panel binding
  // (`scope.target_id` / the descriptor it's pinned to) but the operator can
  // re-scope to ANY registered target via the ScopePicker — no UUID pasting.
  const [target_id, setTargetId] = useState(scope.target_id ?? registration.descriptor_id)

  // The active graph root (kind + id). Defaults to the target's most-recent
  // finding once findings load; a cross-panel open event or a node click
  // re-roots it.
  const [root, setRoot] = useState<{ kind: RowKind; id: string } | null>(null)
  // Row-kinds the operator has hidden (chips). Hidden intermediates get
  // their edges re-parented across the gap in `buildLineageElements`.
  const [hiddenKinds, setHiddenKinds] = useState<ReadonlySet<string>>(new Set())
  // Depth bound for the lineage walk + render (1..4).
  const [depth, setDepth] = useState<number>(MAX_DEPTH)
  const [tooltip, setTooltip] = useState<TooltipState | null>(null)

  const cyRef = useRef<Core | null>(null)
  const containerRef = useRef<HTMLDivElement | null>(null)

  // --- Candidate roots: this target's recent findings. -------------------
  const findingsQ = useQuery<Page<FindingRow>>({
    enabled: !!target_id,
    queryKey: ['target-graph-findings', target_id],
    queryFn: async () => {
      try {
        return await apiGet<Page<FindingRow>>(
          `/findings?target_id=${encodeURIComponent(target_id)}&limit=50`,
        )
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return { data: [], next_cursor: null }
        throw e
      }
    },
  })

  const candidateFindings = findingsQ.data?.data ?? []

  // Auto-root on the newest finding once they arrive (unless already rooted).
  useEffect(() => {
    if (!root && candidateFindings.length > 0) {
      setRoot({ kind: 'finding', id: candidateFindings[0].id })
    }
  }, [root, candidateFindings])

  // Re-scoping to a different target drops the stale root so the new target's
  // newest finding auto-roots (via the effect above). Skips the initial mount
  // (root already null) so a cross-panel open isn't clobbered on first render.
  const prevTargetRef = useRef(target_id)
  useEffect(() => {
    if (prevTargetRef.current !== target_id) {
      prevTargetRef.current = target_id
      setRoot(null)
      setHiddenKinds(new Set())
    }
  }, [target_id])

  // Redesign Move 2: re-root the graph from the shared selection store
  // (replaces the legacy `legba:open-lineage` window listener). `instanceKey`
  // carries the true substrate kind when the cross-room kind was coerced.
  const selection = useSelection((s) => s.selection)
  useEffect(() => {
    if (!selection) return
    const rawKind = selection.instanceKey ?? selection.kind
    setRoot({ kind: toRowKind(rawKind), id: selection.id })
  }, [selection])

  // --- Lineage walk for the active root. ---------------------------------
  // Walk to MAX_DEPTH once; the depth slider then prunes client-side so
  // shrinking the slider doesn't refetch.
  const lineageQ = useQuery<LineageReport>({
    enabled: !!root,
    queryKey: ['target-graph-lineage', root?.kind, root?.id],
    queryFn: () =>
      apiGet<LineageReport>(
        `/lineage/${root!.kind}/${encodeURIComponent(root!.id)}?direction=both&depth=${MAX_DEPTH}`,
      ),
  })

  const report = lineageQ.data
  const allKinds = useMemo(() => presentRowKinds(report), [report])
  const visibleKinds = useMemo(
    () => new Set(allKinds.filter((k) => !hiddenKinds.has(k))),
    [allKinds, hiddenKinds],
  )

  const elements = useMemo<ElementDefinition[]>(() => {
    const g = buildLineageElements(report, { maxDepth: depth, visibleKinds })
    return [
      ...g.nodes.map((n) => ({ group: 'nodes' as const, data: n.data })),
      ...g.edges.map((e) => ({ group: 'edges' as const, data: e.data })),
    ]
  }, [report, visibleKinds, depth])

  const stylesheet = useMemo<StylesheetStyle[]>(
    () => [
      {
        selector: 'node',
        style: {
          'background-color': 'data(color)',
          label: 'data(label)',
          color: '#cbd5e1',
          'font-size': 10,
          'text-valign': 'bottom',
          'text-halign': 'center',
          'text-margin-y': 4,
          'text-outline-color': '#0f1115',
          'text-outline-width': 2,
          width: 18,
          height: 18,
          'border-width': 1,
          'border-color': '#1e293b',
        },
      },
      {
        selector: 'node[?is_root]',
        style: {
          width: 28,
          height: 28,
          'border-width': 2,
          'border-color': '#f59e0b',
          'font-size': 12,
          'font-weight': 700,
        },
      },
      {
        selector: 'edge',
        style: {
          width: 1.5,
          'line-color': 'data(color)',
          'target-arrow-color': 'data(color)',
          'target-arrow-shape': 'triangle',
          'curve-style': 'bezier',
          'arrow-scale': 0.8,
          opacity: 0.7,
        },
      },
      {
        selector: 'node:selected',
        style: { 'border-color': '#10b981', 'border-width': 3 },
      },
    ],
    [],
  )

  // Re-run layout when the element set changes (CytoscapeComponent only
  // applies `layout` on mount).
  useEffect(() => {
    const cy = cyRef.current
    if (!cy || elements.length === 0) return
    cy.layout({
      name: 'cose',
      animate: false,
      idealEdgeLength: 80,
      nodeRepulsion: 4000,
      padding: 20,
      fit: true,
    } as cytoscape.LayoutOptions).run()
  }, [elements])

  const onCyReady = useCallback((cy: Core) => {
    cyRef.current = cy
    cy.removeAllListeners()
    cy.on('tap', 'node', (evt) => {
      const node = evt.target
      // Redesign Move 2: unified selection store (opens the Inspector).
      selectRow(node.data('row_kind'), node.data('id'), node.data('title'), {
        origin: 'target-graph',
      })
      setTooltip(null)
    })
    cy.on('mouseover', 'node', (evt) => {
      const node = evt.target
      const rp = node.renderedPosition()
      setTooltip({
        x: rp.x,
        y: rp.y,
        title: node.data('title') ?? '',
        row_kind: node.data('row_kind') ?? '',
        analyst_id: node.data('analyst_id') ?? null,
        produced_at: node.data('produced_at') ?? '',
      })
    })
    cy.on('mouseout', 'node', () => setTooltip(null))
  }, [])

  function toggleKind(kind: string) {
    setHiddenKinds((prev) => {
      const next = new Set(prev)
      if (next.has(kind)) next.delete(kind)
      else next.add(kind)
      return next
    })
  }

  // Rendered node count reflects the depth + kind filters (root always 1).
  const shownNodes = elements.filter((el) => el.group === 'nodes').length
  const rootFinding = candidateFindings.find((f) => f.id === root?.id)
  const subtitle = !root
    ? `target ${target_id} · no root selected`
    : report
      ? `${shownNodes} nodes · depth ≤ ${depth} · root ${root.kind} · target ${target_id}`
      : lineageQ.isFetching
        ? 'loading graph…'
        : `target ${target_id}`

  return (
    <PanelChrome
      registration={registration}
      subtitle={subtitle}
      onRefresh={() => {
        findingsQ.refetch()
        lineageQ.refetch()
      }}
    >
      <div className="flex flex-col h-full min-h-[320px]">
        {/* Target picker — re-scope the whole walk to any registered target. */}
        <div className="flex items-center gap-2 mb-2 text-xs flex-wrap">
          <label className="text-slate-400">target</label>
          <ScopePicker
            family="target"
            value={target_id}
            onChange={setTargetId}
            allowEmpty={false}
            placeholder="select target…"
            testId="target-graph-target-select"
          />
        </div>

        {/* Root picker — this target's recent findings. */}
        <div className="flex items-center gap-2 mb-2 text-xs flex-wrap">
          <label className="text-slate-400">root finding</label>
          <select
            className="flex-1 min-w-[180px] bg-surface-200 border border-slate-700 rounded p-1 px-2"
            value={root?.kind === 'finding' ? root.id : ''}
            onChange={(e) => e.target.value && setRoot({ kind: 'finding', id: e.target.value })}
            disabled={candidateFindings.length === 0}
            data-testid="target-graph-root-select"
          >
            {candidateFindings.length === 0 && (
              <option value="">(no findings for this target)</option>
            )}
            {rootFinding === undefined && root && (
              <option value="">
                {root.kind}:{root.id.slice(0, 8)}…
              </option>
            )}
            {candidateFindings.map((f) => (
              <option key={f.id} value={f.id}>
                {f.title.slice(0, 48)}
              </option>
            ))}
          </select>
          {lineageQ.isFetching && <span className="text-slate-500">loading…</span>}
        </div>

        {/* Depth slider — bounds the lineage hops rendered (1..4). */}
        {root && (
          <div className="flex items-center gap-2 mb-2 text-[11px]">
            <label htmlFor="target-graph-depth" className="text-slate-500 uppercase tracking-wider">
              depth
            </label>
            <input
              id="target-graph-depth"
              type="range"
              min={MIN_DEPTH}
              max={MAX_DEPTH}
              step={1}
              value={depth}
              onChange={(e) => setDepth(Number(e.target.value))}
              className="flex-1 max-w-[200px] accent-amber-500"
              data-testid="target-graph-depth"
            />
            <span className="text-slate-300 font-mono tabular-nums w-4">{depth}</span>
            <span className="text-slate-500">
              hidden intermediates are re-parented to keep the graph connected
            </span>
          </div>
        )}

        {/* Row-kind filter chips — only kinds present in this walk. */}
        {allKinds.length > 0 && (
          <div
            className="flex items-center gap-3 mb-2 text-[11px] flex-wrap"
            data-testid="target-graph-rel-filters"
          >
            <span className="text-slate-500 uppercase tracking-wider">row kinds</span>
            {allKinds.map((kind) => (
              <label key={kind} className="flex items-center gap-1 cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={!hiddenKinds.has(kind)}
                  onChange={() => toggleKind(kind)}
                  data-testid={`target-graph-rel-${kind}`}
                />
                <span
                  className="inline-block w-2.5 h-2.5 rounded-full"
                  style={{ backgroundColor: kindColor(kind) }}
                />
                <span className="text-slate-300">{kind}</span>
              </label>
            ))}
          </div>
        )}

        {findingsQ.isLoading && !root && (
          <div className="text-slate-500 text-sm py-4 text-center">loading target findings…</div>
        )}
        {!findingsQ.isLoading && candidateFindings.length === 0 && !root && (
          <div className="text-slate-500 text-sm py-4 text-center" data-testid="target-graph-empty">
            no findings for this target yet — the relationship graph populates as analysts emit
            findings, or click a row in another panel to walk its lineage
          </div>
        )}
        {lineageQ.error instanceof Error && (
          <div className="text-rose-400 text-sm py-2">error: {lineageQ.error.message}</div>
        )}

        {root && (
          <div
            ref={containerRef}
            className="relative flex-1 min-h-[240px] bg-surface-200 border border-slate-800 rounded"
            data-testid="target-graph-canvas"
          >
            {elements.length > 0 ? (
              <CytoscapeComponent
                cy={onCyReady}
                elements={elements}
                stylesheet={stylesheet}
                layout={{ name: 'cose', animate: false }}
                style={{ width: '100%', height: '100%' }}
                minZoom={0.25}
                maxZoom={3}
              />
            ) : (
              <div className="absolute inset-0 flex items-center justify-center text-slate-500 text-sm">
                {lineageQ.isFetching ? 'loading graph…' : 'no related rows (depth ≤ 3)'}
              </div>
            )}
            {tooltip && (
              <div
                className="pointer-events-none absolute z-10 bg-surface-50 border border-slate-700 rounded p-2 text-[11px] text-slate-200 shadow-lg max-w-[260px]"
                style={{
                  left: Math.min(tooltip.x + 12, (containerRef.current?.clientWidth ?? 0) - 270),
                  top: Math.max(tooltip.y - 8, 0),
                }}
                data-testid="target-graph-tooltip"
              >
                <div className="text-slate-500 text-[10px] uppercase tracking-wide">
                  {tooltip.row_kind}
                </div>
                <div className="font-medium">{tooltip.title}</div>
                {tooltip.analyst_id && (
                  <div className="text-slate-400 mt-1">analyst: {tooltip.analyst_id}</div>
                )}
                {tooltip.produced_at && (
                  <div className="text-slate-500 mt-0.5">
                    {new Date(tooltip.produced_at).toLocaleString()}
                  </div>
                )}
              </div>
            )}
            <Legend />
          </div>
        )}

        <div className="text-[10px] text-slate-500 mt-2 px-1">
          hover a node for provenance (kind · analyst · time) · click to open lineage · filter by
          row kind + depth above
        </div>
      </div>
    </PanelChrome>
  )
}

function Legend() {
  return (
    <div className="absolute bottom-2 left-2 bg-surface-50/90 border border-slate-700 rounded p-2 text-[10px] text-slate-300 space-y-1 max-h-[60%] overflow-auto">
      {(Object.keys(KIND_COLORS) as Array<keyof typeof KIND_COLORS>).map((k) => (
        <div key={k} className="flex items-center gap-1.5">
          <span
            className="inline-block w-2.5 h-2.5 rounded-full"
            style={{ backgroundColor: KIND_COLORS[k] }}
          />
          <span>{k}</span>
        </div>
      ))}
    </div>
  )
}
