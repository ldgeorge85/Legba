/**
 * The Flow — canvas (F.B).
 *
 * Renders the registry projection (F.A) as a ReactFlow graph of custom nodes.
 * The raw registry wiring is a 1000+-edge hairball, so the canvas is built to
 * turn it into a readable diagnostic with two operator controls:
 *
 *   - EDGE-KIND filter   → show/hide each wiring kind (subscription /
 *                          analyst_target / grant) independently, so the
 *                          operator can isolate one kind. The dense predicate
 *                          fan-out (analyst_target) is hidden by default; in the
 *                          unfocused view, hiding a kind also drops the nodes it
 *                          orphans so the canvas actually thins out.
 *   - FOCUS              → double-click ANY node to scope the canvas to that
 *                          node plus its directly-connected neighbours; a chip
 *                          surfaces + clears it.
 *
 * It also owns the rest of the canvas-level interactions:
 *
 *   - click a node       → select it (shared selection store → drives the
 *                          Inspector and cross-room links)
 *   - search box         → dims non-matching nodes (by label / descriptorId)
 *
 * Nodes/edges are seeded from the projection prop into local state and re-sync
 * whenever the projection changes (so F.A re-projections after an edit land
 * here). Selection/focus/edge-kind visibility live in the shared flow store,
 * telemetry is painted by the node itself, so this file stays a thin shell.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  ReactFlowProvider,
  type Node,
  type NodeMouseHandler,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { Search } from 'lucide-react'
import { cn } from '@/lib/cn'
import { useSelection, type SelectionKind } from '@/state/selection'
import { FlowNode } from './FlowNode'
import { useFlowState, EDGE_KINDS } from './flowState'
import type {
  GraphProjection,
  FlowNode as FlowNodeType,
  FlowEdge,
  FlowEdgeKind,
  FlowNodeKind,
} from './types'

const nodeTypes = { legba: FlowNode }

/** Per-edge-kind stroke color (matches the toggle swatches) + a short label. */
const EDGE_KIND_STYLE: Record<FlowEdgeKind, { color: string; label: string }> = {
  subscription: { color: '#3b82f6', label: 'subscription' }, // source → target (info blue)
  analyst_target: { color: '#a78bfa', label: 'analyst → target' }, // violet
  grant: { color: '#f59e0b', label: 'grant' }, // pack → analyst/target (warning amber)
}

/** Map a flow node kind to the shared selection vocabulary. 'pack' has no
 *  first-class selection kind in the cross-room grammar, so packs select as a
 *  generic 'entity'. */
const SELECTION_KIND: Record<FlowNodeKind, SelectionKind> = {
  source: 'source',
  target: 'target',
  analyst: 'analyst',
  pack: 'entity',
}

/** MiniMap dot color per node kind. */
const MINIMAP_KIND_COLOR: Record<string, string> = {
  source: '#3b82f6',
  target: '#10b981',
  analyst: '#a78bfa',
  pack: '#f59e0b',
}

interface FlowCanvasProps {
  projection: GraphProjection
}

function FlowCanvasInner({ projection }: FlowCanvasProps) {
  const focusNodeId = useFlowState((s) => s.focusNodeId)
  const setFocusNodeId = useFlowState((s) => s.setFocusNodeId)
  const hiddenEdgeKinds = useFlowState((s) => s.hiddenEdgeKinds)
  const toggleEdgeKind = useFlowState((s) => s.toggleEdgeKind)
  const resetEdgeKinds = useFlowState((s) => s.resetEdgeKinds)
  const select = useSelection((s) => s.select)
  // Highlight is derived from the SHARED selection (Move 2). A node is selected
  // when the selection's instanceKey matches the node id (preferred) or the
  // selection's id matches the node's descriptorId.
  const selection = useSelection((s) => s.selection)

  // Local canvas state seeded from the projection prop; re-synced on change so
  // F.A re-projections (e.g. after a lifecycle edit) flow through.
  const [nodes, setNodes] = useState<FlowNodeType[]>(projection.nodes)
  const [edges, setEdges] = useState<FlowEdge[]>(projection.edges)
  const [search, setSearch] = useState('')

  useEffect(() => {
    setNodes(projection.nodes)
  }, [projection.nodes])
  useEffect(() => {
    setEdges(projection.edges)
  }, [projection.edges])

  // ---- edge-kind counts (drive the toggle chip labels) ----
  const edgeCounts = useMemo<Record<FlowEdgeKind, number>>(() => {
    const counts = { subscription: 0, analyst_target: 0, grant: 0 } as Record<FlowEdgeKind, number>
    for (const e of edges) {
      const k = e.data?.kind
      if (k) counts[k] += 1
    }
    return counts
  }, [edges])

  const hidden = useMemo(() => new Set<FlowEdgeKind>(hiddenEdgeKinds), [hiddenEdgeKinds])

  // Edges of a hidden wiring kind are dropped before anything else, so both
  // the focus walk and the orphan trim below operate on the visible kinds only.
  const kindFilteredEdges = useMemo<FlowEdge[]>(
    () => edges.filter((e) => !e.data?.kind || !hidden.has(e.data.kind)),
    [edges, hidden],
  )

  // The focused node's flow id (or null) — focus is keyed by node id now, so it
  // works for ANY kind, not just targets.
  const focusedNodeId = useMemo(
    () => (focusNodeId && nodes.some((n) => n.id === focusNodeId) ? focusNodeId : null),
    [focusNodeId, nodes],
  )
  const focusedLabel = useMemo(
    () => (focusedNodeId ? nodes.find((n) => n.id === focusedNodeId)?.data.label ?? null : null),
    [focusedNodeId, nodes],
  )

  // ---- visible set --------------------------------------------------------
  // FOCUS mode: scope to the focused node + its directly-connected neighbours
  // (over the kind-filtered edges).
  // UNFOCUSED: show the kind-filtered edges and DROP nodes that no visible edge
  // touches — so hiding a wiring kind genuinely thins the hairball rather than
  // leaving a field of disconnected cards. Lone source/target/analyst/pack
  // descriptors with zero wiring of any visible kind are intentionally hidden;
  // FOCUS or re-enabling the kind brings them back.
  const { visibleNodes, visibleEdges } = useMemo(() => {
    if (focusedNodeId) {
      const keep = new Set<string>([focusedNodeId])
      const keptEdges: FlowEdge[] = []
      for (const e of kindFilteredEdges) {
        if (e.source === focusedNodeId || e.target === focusedNodeId) {
          keep.add(e.source)
          keep.add(e.target)
          keptEdges.push(e)
        }
      }
      return {
        visibleNodes: nodes.filter((n) => keep.has(n.id)),
        visibleEdges: keptEdges,
      }
    }
    const connected = new Set<string>()
    for (const e of kindFilteredEdges) {
      connected.add(e.source)
      connected.add(e.target)
    }
    return {
      visibleNodes: nodes.filter((n) => connected.has(n.id)),
      visibleEdges: kindFilteredEdges,
    }
  }, [focusedNodeId, nodes, kindFilteredEdges])

  // Paint each visible edge with its kind's color so the canvas reads as a
  // legend-backed diagnostic (the toggle swatches use the same colors).
  const styledEdges = useMemo<FlowEdge[]>(
    () =>
      visibleEdges.map((e) => {
        const color = e.data?.kind ? EDGE_KIND_STYLE[e.data.kind].color : '#475569'
        return { ...e, style: { ...e.style, stroke: color, strokeWidth: 1.5, opacity: 0.7 } }
      }),
    [visibleEdges],
  )

  // The node id currently highlighted, derived from the shared selection.
  const selectedNodeId = useMemo<string | null>(() => {
    if (!selection) return null
    const byInstance = selection.instanceKey
      ? nodes.find((n) => n.id === selection.instanceKey)
      : undefined
    if (byInstance) return byInstance.id
    return nodes.find((n) => n.data.descriptorId === selection.id)?.id ?? null
  }, [selection, nodes])

  // ---- search: dim non-matching nodes (does not remove them) ----
  const query = search.trim().toLowerCase()
  const renderedNodes = useMemo<FlowNodeType[]>(() => {
    return visibleNodes.map((n) => {
      const typed: FlowNodeType = { ...n, type: 'legba', selected: n.id === selectedNodeId }
      if (!query) return { ...typed, style: { ...typed.style, opacity: 1 } }
      const hay = `${n.data.label} ${n.data.descriptorId}`.toLowerCase()
      const match = hay.includes(query)
      return { ...typed, style: { ...typed.style, opacity: match ? 1 : 0.2 } }
    })
  }, [visibleNodes, query, selectedNodeId])

  const onNodeClick = useCallback<NodeMouseHandler<FlowNodeType>>(
    (_evt, n) => {
      // Single, unified select — brushes every room AND opens the Inspector.
      //
      // `instanceKey: n.id` is the canonical P-A15 disambiguation case: the
      // shared selection keys on `descriptorId`, but the Flow graph can carry
      // two nodes with the same descriptorId (e.g. a `pack` granted into both
      // an analyst and a target lane). Without instanceKey the highlight
      // (`selectedNodeId` above) matches the FIRST node with that descriptorId
      // and lights up the wrong instance; with it, we highlight exactly the
      // clicked node. Any consumer panel that can emit duplicate-descriptor
      // rows should likewise pass `selectRow(..., { instanceKey })`.
      select({
        kind: SELECTION_KIND[n.data.kind],
        id: n.data.descriptorId,
        label: n.data.label,
        instanceKey: n.id,
        origin: 'flow',
      })
    },
    [select],
  )

  const onNodeDoubleClick = useCallback<NodeMouseHandler<FlowNodeType>>(
    (_evt, n) => {
      // Toggle subgraph FOCUS on the double-clicked node (any kind), keyed by
      // node id so duplicate-descriptor instances focus the exact node.
      setFocusNodeId(focusNodeId === n.id ? null : n.id)
    },
    [focusNodeId, setFocusNodeId],
  )

  const anyHidden = hiddenEdgeKinds.length > 0

  return (
    <div className="relative h-full w-full bg-surface-300">
      {/* Toolbar — search + edge-kind filter + focus chip */}
      <div className="absolute left-3 top-3 z-10 flex flex-col items-start gap-1.5">
        {/* Search — dims non-matching nodes */}
        <div className="flex items-center gap-1.5 rounded-md border border-slate-800 bg-surface-200/90 px-2 py-1 shadow-lg backdrop-blur">
          <Search className="h-3.5 w-3.5 text-slate-500" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="search nodes…"
            spellCheck={false}
            className="w-44 bg-transparent text-[12px] text-slate-200 placeholder:text-slate-600 focus:outline-none"
            data-testid="flow-search"
          />
        </div>

        {/* Edge-kind toggles — isolate one wiring kind from the hairball */}
        <div
          className="flex items-center gap-1 rounded-md border border-slate-800 bg-surface-200/90 px-1.5 py-1 shadow-lg backdrop-blur"
          data-testid="flow-edge-kinds"
        >
          <span className="px-0.5 text-[10px] uppercase tracking-wide text-slate-500">wiring</span>
          {EDGE_KINDS.map((kind) => {
            const { color, label } = EDGE_KIND_STYLE[kind]
            const on = !hidden.has(kind)
            const count = edgeCounts[kind]
            return (
              <button
                key={kind}
                type="button"
                onClick={() => toggleEdgeKind(kind)}
                aria-pressed={on}
                title={`${on ? 'Hide' : 'Show'} ${label} edges (${count})`}
                data-testid={`flow-edge-toggle-${kind}`}
                className={cn(
                  'flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] transition-colors',
                  on
                    ? 'bg-surface-50 text-slate-200'
                    : 'bg-transparent text-slate-600 hover:text-slate-400',
                )}
              >
                <span
                  className="h-2 w-2 rounded-full shrink-0"
                  style={{ backgroundColor: on ? color : 'transparent', border: `1px solid ${color}` }}
                />
                {label}
                <span className="tabular-nums text-slate-500">{count}</span>
              </button>
            )
          })}
          {anyHidden && (
            <button
              type="button"
              onClick={resetEdgeKinds}
              title="Reset edge-kind filter to the default view"
              data-testid="flow-edge-reset"
              className="ml-0.5 rounded bg-surface-50 px-1.5 py-0.5 text-[10px] text-slate-400 hover:text-slate-200"
            >
              reset
            </button>
          )}
        </div>

        {/* Focus chip — scope to one node's subgraph (double-click a node) */}
        {focusedNodeId && (
          <div
            className="flex items-center gap-1.5 rounded-md border border-accent-info/40 bg-surface-200/90 px-2 py-1 text-[11px] text-slate-200 shadow-lg backdrop-blur"
            data-testid="flow-focus-chip"
          >
            <span className="text-slate-500">focus</span>
            <span className="max-w-[180px] truncate font-medium">{focusedLabel}</span>
            <button
              type="button"
              onClick={() => setFocusNodeId(null)}
              className="ml-0.5 rounded bg-surface-50 px-1.5 py-0.5 text-[10px] text-slate-400 hover:text-slate-200"
              data-testid="flow-clear-focus"
            >
              clear
            </button>
          </div>
        )}
      </div>

      <ReactFlow
        nodeTypes={nodeTypes}
        nodes={renderedNodes}
        edges={styledEdges}
        onNodeClick={onNodeClick}
        onNodeDoubleClick={onNodeDoubleClick}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable
        fitView
        proOptions={{ hideAttribution: true }}
        className="bg-surface-300"
      >
        <Background color="#1f2937" gap={20} />
        <Controls className="!border-slate-800 !bg-surface-100" showInteractive={false} />
        <MiniMap
          pannable
          zoomable
          maskColor="rgba(10,12,16,0.7)"
          className="!border !border-slate-800 !bg-surface-200"
          nodeColor={(n: Node) =>
            MINIMAP_KIND_COLOR[(n.data as { kind?: string }).kind ?? ''] ?? '#475569'
          }
        />
      </ReactFlow>
    </div>
  )
}

export default function FlowCanvas({ projection }: FlowCanvasProps) {
  return (
    <ReactFlowProvider>
      <FlowCanvasInner projection={projection} />
    </ReactFlowProvider>
  )
}
