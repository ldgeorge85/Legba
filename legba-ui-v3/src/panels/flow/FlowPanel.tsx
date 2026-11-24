/**
 * Flow (`system.flow`) — a READ-ONLY canvas over the descriptors.
 *
 * The descriptor is the source of truth; this panel only renders what
 * `GET /v3/flow/graph` derives from the registry. Every edge is a declared
 * relation and names its basis (hover an edge); a relation the registry cannot
 * express is not drawn, it is listed in the node's "Undeclared" tab and in the
 * report. Layout is computed (ELK, in a Web Worker) and never persisted.
 *
 * N-Q (incident 2026-11-13: opening v0 locked the browser — ELK on the main
 * thread over the whole 336-node / 4,434-edge graph):
 *
 *   - INERT WHILE HIDDEN: nothing is fetched until the tile has been shown,
 *     and nothing is laid out while it is not visible;
 *   - THE OVERVIEW by default: one composite node per view group with
 *     aggregated, counted edges (`flowView.ts`); expansion is explicit;
 *   - THE BUDGET: a view over 150 nodes / 600 edges is not drawn — the counts
 *     are shown and the reader narrows (collapse, family, target chain,
 *     neighbourhood, search);
 *   - layout off the main thread, cancellable, memoised, with a 5 s timeout
 *     that falls back to columns and says so.
 */
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Edge,
  type Node,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { cn } from '@/lib/cn'
import { useDockviewTileRedraw } from '@/components/useTileRedraw'
import { useSeenOnce, useTileVisible } from '@/components/useTileVisible'
import type { PanelProps } from '@/types'
import { edgeLabel, scopeOptions, searchNodes } from './flowProjection'
import { hashKey, structureKey, type LayoutEngine } from './flowLayout'
import { initialExpanded, MAX_VIEW_EDGES, MAX_VIEW_NODES, projectView, scopePinned, viewCountsLine, viewGroups } from './flowView'
import { FLOW_NODE_TYPES } from './FlowCards'
import { FlowInspector } from './FlowInspector'
import { FlowSidebar } from './FlowSidebar'
import { useFlowGraph, useFlowNode } from './useFlowData'
import { useFlowLayout } from './useFlowLayout'

const btn = 'rounded-control border border-line px-1.5 py-0.5 text-ink-2 hover:text-ink-1 disabled:opacity-40'

function FlowCanvasArea({ engine }: { engine?: LayoutEngine }) {
  const visible = useTileVisible()
  const seen = useSeenOnce(visible)
  const [scope, setScope] = useState('all')
  const allQ = useFlowGraph('all', seen)
  const scopedQ = useFlowGraph(scope, seen && scope !== 'all')
  const q = scope === 'all' ? allQ : scopedQ
  const payload = q.data

  // The expansion belongs to ONE payload: a new payload starts from its own
  // initial set in the same render (never a render of the wrong set).
  const payloadKey = payload ? `${scope}@${payload.generated_at ? payload.generated_at : ''}#${payload.nodes.length}/${payload.edges.length}` : ''
  const index = useMemo(() => (payload ? viewGroups(payload) : null), [payload])
  const pinned = useMemo(() => (payload ? scopePinned(payload, scope) : new Set<string>()), [payload, scope])
  const initial = useMemo(
    () => (payload && index ? initialExpanded(payload, scope, pinned, index) : new Set<string>()),
    [payload, scope, pinned, index],
  )
  const [expandedState, setExpandedState] = useState<{ key: string; set: Set<string>; last: string | null }>({ key: '', set: new Set(), last: null })
  const expanded = expandedState.key === payloadKey ? expandedState.set : initial
  const lastExpanded = expandedState.key === payloadKey ? expandedState.last : null

  const [selected, setSelected] = useState<string | null>(null)
  const [runId, setRunId] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [hoverEdge, setHoverEdge] = useState<string | null>(null)
  const [showReport, setShowReport] = useState(false)
  const rf = useReactFlow()

  const matched = useMemo(() => (payload ? searchNodes(payload, search) : new Set<string>()), [payload, search])
  const hits = useMemo(
    () => (payload && matched.size ? payload.nodes.filter((n) => matched.has(n.id)).slice(0, 20) : []),
    [payload, matched],
  )
  const selectedDetail = useFlowNode(selected && !selected.startsWith('group:') ? selected : null)
  const runStatus = useMemo<Record<string, string>>(() => {
    if (!selected || !runId) return {}
    const r = selectedDetail.data?.recent_runs?.find((x) => x.run_id === runId)
    return r?.status ? { [selected]: r.status } : {}
  }, [selected, runId, selectedDetail.data])

  const view = useMemo(
    () => (payload && index
      ? projectView(payload, { expanded, pinned, focus: scope.startsWith('node:'), matched, runStatus, selected }, index)
      : null),
    [payload, index, expanded, pinned, scope, matched, runStatus, selected],
  )

  const toggle = useCallback(
    (group: string) =>
      setExpandedState(() => {
        const next = new Set(expanded)
        const opening = !next.has(group)
        if (opening) next.add(group)
        else next.delete(group)
        return { key: payloadKey, set: next, last: opening ? group : null }
      }),
    [expanded, payloadKey],
  )

  // Layout depends on structure only (ids + edges), not on highlight state.
  const memoKey = useMemo(
    () => (view && payload ? `${scope}|${hashKey([...expanded].sort().join(','))}|${hashKey(payloadKey)}|${structureKey(view.nodes, view.edges)}` : ''),
    [view, payload, scope, expanded, payloadKey],
  )
  const { laid, pending } = useFlowLayout(view ? view.nodes : [], view ? view.edges : [], memoKey, visible && !!view && !view.overBudget, engine)
  const fit = useCallback(() => requestAnimationFrame(() => rf.fitView({ padding: 0.1 })), [rf])
  const laidKey = laid ? laid.key : ''
  useEffect(() => {
    if (laidKey) fit()
  }, [laidKey, fit])

  const nodes = useMemo<Node[]>(() => {
    if (!view || !laid) return []
    const pos = new Map(laid.result.nodes.map((n) => [n.id, n.position]))
    const out: Node[] = []
    for (const n of view.nodes) {
      const p = pos.get(n.id)
      if (p) out.push({ ...n, position: p } as Node)
    }
    return out
  }, [view, laid])

  const edges = useMemo<Edge[]>(() => {
    if (!view || !laid) return []
    return view.edges.map((e) => {
      const lit = e.id === hoverEdge || (selected !== null && (e.source === selected || e.target === selected))
      return {
        id: e.id,
        source: e.source,
        target: e.target,
        data: e.data,
        label: lit && e.data ? edgeLabel(e.data) : undefined,
        className: cn(!lit && (e.data?.relation === 'binds' || e.data?.relation === 'feeds') ? 'opacity-40' : undefined),
      }
    })
  }, [view, laid, hoverEdge, selected])

  useDockviewTileRedraw(() => rf.fitView({ padding: 0.1 }))

  const onNodeClick = useCallback((_: unknown, n: Node) => {
    if (n.type === 'flowGroup') {
      toggle((n.data as { group: string }).group)
      return
    }
    setRunId(null)
    setSelected((cur) => (cur === n.id ? null : n.id))
  }, [toggle])

  const goScope = (s: string) => {
    setScope(s)
    setSelected(null)
    setRunId(null)
  }

  const jump = () => {
    if (hits.length === 1) return goScope(`node:${hits[0].id}`)
    const ids = [...matched].filter((id) => nodes.some((n) => n.id === id))
    if (ids.length) rf.fitView({ nodes: ids.map((id) => ({ id })), padding: 0.3, maxZoom: 1.2 })
  }

  const options = useMemo(() => scopeOptions(allQ.data), [allQ.data])
  const scopeKnown = options.some((o) => o.value === scope)

  let body: ReactNode
  if (!seen) body = <p className="p-3 text-body text-ink-3">The flow graph loads when this tab is shown.</p>
  else if (q.isError) body = <p className="p-3 text-body text-accent-critical">Could not load the flow graph.</p>
  else if (!payload || !view) body = <p className="p-3 text-body text-ink-3">Deriving the graph from the registry…</p>
  else if (view.overBudget) {
    body = (
      <div className="space-y-2 p-3 text-body text-ink-2" data-testid="flow-over-budget">
        <p>
          This view would draw {view.overBudget.nodes} nodes and {view.overBudget.edges} edges, over the
          budget of {MAX_VIEW_NODES} nodes / {MAX_VIEW_EDGES} edges, so it is not drawn.
        </p>
        <p className="text-ink-3">
          Narrow it: collapse a group, choose a family or a target chain in scope, or search and open one node&apos;s neighbourhood.
        </p>
        {lastExpanded && (
          <button type="button" className={btn} onClick={() => toggle(lastExpanded)} data-testid="flow-undo-expand">
            collapse {lastExpanded} again
          </button>
        )}
      </div>
    )
  } else if (pending || !laid) {
    body = <p className="p-3 text-body text-ink-3">Laying out {view.nodes.length} nodes…</p>
  } else {
    body = (
      <>
        {laid.result.fallback && (
          <div className="absolute left-2 top-2 z-10 rounded-control border border-line bg-surf-1 px-2 py-1 text-[11px] text-ink-2" data-testid="flow-layout-fallback">
            Automatic layout {laid.result.fallback === 'timeout' ? 'timed out' : 'failed'}; showing a simple column layout.
          </div>
        )}
        <ReactFlow
          nodeTypes={FLOW_NODE_TYPES}
          nodes={nodes}
          edges={edges}
          onNodeClick={onNodeClick}
          onEdgeMouseEnter={(_, e) => setHoverEdge(e.id)}
          onEdgeMouseLeave={() => setHoverEdge(null)}
          nodesDraggable={false}
          nodesConnectable={false}
          edgesFocusable={false}
          elementsSelectable
          minZoom={0.05}
          proOptions={{ hideAttribution: true }}
          onlyRenderVisibleElements
        >
          <Background gap={24} />
          <Controls showInteractive={false} />
          <MiniMap pannable zoomable />
        </ReactFlow>
      </>
    )
  }

  return (
    <div className="flex h-full min-h-0 w-full bg-surf-base" data-testid="flow-panel">
      <div className="relative flex min-w-0 flex-1 flex-col">
        <div className="flex flex-wrap items-center gap-2 border-b border-line px-2 py-1.5 text-[11px]">
          <label className="flex items-center gap-1 text-ink-3">
            scope
            <select
              className="max-w-[260px] rounded-control border border-line bg-surf-1 px-1 py-0.5 text-ink-1"
              value={scope}
              onChange={(e) => goScope(e.target.value)}
              data-testid="flow-scope"
            >
              {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              {!scopeKnown && <option value={scope}>{scope}</option>}
            </select>
          </label>
          <button
            type="button"
            className={btn}
            disabled={!selected || selected.startsWith('group:')}
            onClick={() => selected && goScope(`node:${selected}`)}
          >
            neighbourhood of selection
          </button>
          <input
            className="w-44 rounded-control border border-line bg-surf-1 px-1.5 py-0.5 text-ink-1 placeholder:text-ink-3"
            placeholder="search name, kind, model, tool"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && jump()}
            data-testid="flow-search"
          />
          {search && <span className="text-ink-3">{matched.size} found</span>}
          <button type="button" className={btn} onClick={() => rf.fitView({ padding: 0.1 })}>fit</button>
          <button type="button" className={btn} onClick={() => setShowReport((v) => !v)}>report</button>
          {view && (
            <span className="text-ink-3" data-testid="flow-counts">
              {viewCountsLine(view)}
            </span>
          )}
          <span className="ml-auto text-ink-3">read-only · layout computed, not saved</span>
        </div>
        <div className="flex min-h-0 flex-1">
          {view && (
            <FlowSidebar
              view={view}
              onToggle={toggle}
              hits={hits}
              onHit={(id) => goScope(`node:${id}`)}
              report={showReport && allQ.data ? allQ.data : null}
            />
          )}
          <div className="relative min-w-0 flex-1">{body}</div>
        </div>
      </div>
      {selected && !selected.startsWith('group:') && (
        <div className="w-[440px] shrink-0 border-l border-line bg-surf-1">
          <FlowInspector nodeId={selected} runId={runId} onRun={setRunId} />
        </div>
      )}
    </div>
  )
}

export default function FlowPanel(props: Partial<PanelProps> & { layoutEngine?: LayoutEngine }) {
  return (
    <ReactFlowProvider>
      <FlowCanvasArea engine={props.layoutEngine} />
    </ReactFlowProvider>
  )
}
