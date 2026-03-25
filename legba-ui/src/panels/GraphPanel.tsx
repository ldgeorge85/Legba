import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import Graph from 'graphology'
import Sigma from 'sigma'
import forceAtlas2 from 'graphology-layout-forceatlas2'
import { random } from 'graphology-layout'
import louvain from 'graphology-communities-louvain'
import { useEgoGraph, useGraph } from '@/api/hooks'
import type { GraphData, GraphNode } from '@/api/types'
import { useSelectionStore } from '@/stores/selection'
import { useWorkspaceStore } from '@/stores/workspace'

// ── Color map by entity type ──
const TYPE_COLORS: Record<string, string> = {
  person: '#3b82f6',
  organization: '#8b5cf6',
  location: '#22c55e',
  country: '#10b981',
  concept: '#06b6d4',
  weapon: '#ef4444',
  military_unit: '#f43f5e',
  armed_group: '#f97316',
}
const DEFAULT_COLOR = '#6b7280'

// ── Color map by relationship type (matches V1 palette) ──
const EDGE_COLORS: Record<string, string> = {
  LeaderOf: '#60a5fa',
  AlliedWith: '#4ade80',
  HostileTo: '#f87171',
  EconomicTie: '#fbbf24',
  MemberOf: '#818cf8',
  LocatedIn: '#2dd4bf',
  OperatesIn: '#2dd4bf',
  SuppliesWeaponsTo: '#f87171',
  SanctionedBy: '#fb923c',
  TradesWith: '#fbbf24',
  BordersWith: '#22d3ee',
  ParticipatesIn: '#a78bfa',
  HeadquarteredIn: '#2dd4bf',
  RelatedTo: '#64748b',
}
const DEFAULT_EDGE_COLOR = '#555555'

function getEdgeColor(relType: string): string {
  return EDGE_COLORS[relType] ?? DEFAULT_EDGE_COLOR
}

const EDGE_REL_TYPES = [
  'AlliedWith', 'HostileTo', 'LeaderOf', 'MemberOf', 'LocatedIn',
  'OperatesIn', 'SuppliesWeaponsTo', 'SanctionedBy', 'TradesWith',
  'PartOf', 'BordersWith',
] as const

// Top-5 relationship types for filter toolbar
const TOP_REL_TYPES = ['AlliedWith', 'HostileTo', 'MemberOf', 'OperatesIn', 'LeaderOf']

// Entity types for filter toolbar
const ENTITY_TYPE_FILTERS = ['person', 'country', 'organization', 'armed_group']

// Distinct palette for community coloring (12 colors)
const COMMUNITY_PALETTE = [
  '#f59e0b', // amber
  '#3b82f6', // blue
  '#ef4444', // red
  '#22c55e', // green
  '#8b5cf6', // violet
  '#ec4899', // pink
  '#06b6d4', // cyan
  '#f97316', // orange
  '#14b8a6', // teal
  '#a855f7', // purple
  '#84cc16', // lime
  '#e11d48', // rose
]

function getCommunityColor(community: number): string {
  return COMMUNITY_PALETTE[community % COMMUNITY_PALETTE.length]
}

function getNodeColor(type: string): string {
  return TYPE_COLORS[type.toLowerCase()] ?? DEFAULT_COLOR
}

// ── Build graphology graph from API data ──
function buildGraph(data: GraphData, typeFilters: Set<string>): Graph {
  const g = new Graph({ multi: true, type: 'undirected' })

  // Track seen node IDs to skip duplicates from API
  const seenNodes = new Set<string>()

  // If filtering by type, collect visible node IDs first
  const visibleNodes = new Set<string>()

  for (const node of data.nodes) {
    if (seenNodes.has(node.id)) continue
    seenNodes.add(node.id)
    if (typeFilters.size > 0 && !typeFilters.has(node.type.toLowerCase())) continue
    visibleNodes.add(node.id)

    // Use the global degree from API properties if available
    const apiDegree = (node.properties?.degree as number) ?? 0

    g.addNode(node.id, {
      label: node.label,
      entityType: node.type,
      apiDegree,
      x: 0,
      y: 0,
      size: 4,
      color: getNodeColor(node.type),
    })
  }

  // Count edge multiplicity between node pairs for thickness
  const edgePairCount = new Map<string, number>()
  for (const edge of data.edges) {
    if (edge.source === edge.target) continue
    if (!visibleNodes.has(edge.source) || !visibleNodes.has(edge.target)) continue
    // Undirected pair key
    const pairKey = [edge.source, edge.target].sort().join('||')
    edgePairCount.set(pairKey, (edgePairCount.get(pairKey) ?? 0) + 1)
  }

  // Add edges, skipping any that reference missing nodes or are self-loops
  for (const edge of data.edges) {
    if (edge.source === edge.target) continue
    if (!g.hasNode(edge.source) || !g.hasNode(edge.target)) continue
    const edgeKey = `${edge.source}--${edge.rel_type}--${edge.target}`
    if (!g.hasEdge(edgeKey)) {
      const pairKey = [edge.source, edge.target].sort().join('||')
      const multiplicity = edgePairCount.get(pairKey) ?? 1

      // Edge thickness: prefer weight from temporal graph enrichment, fallback to multiplicity
      const weight = (edge.properties?.weight as number) ?? undefined
      const edgeSize = weight != null
        ? Math.min(1 + weight * 2, 6)
        : Math.min(1 + (multiplicity - 1) * 0.6, 4)

      // Edge opacity from confidence
      const confidence = (edge.properties?.confidence as number) ?? undefined
      const baseColor = getEdgeColor(edge.rel_type)
      // Convert confidence (0-1) to hex alpha suffix (40-FF)
      const alphaHex = confidence != null
        ? Math.round(0x40 + confidence * (0xFF - 0x40)).toString(16).padStart(2, '0')
        : ''

      const evidenceCount = (edge.properties?.evidence_count as number) ?? undefined

      g.addEdgeWithKey(edgeKey, edge.source, edge.target, {
        label: edge.rel_type,
        color: baseColor + alphaHex,
        size: edgeSize,
        type: 'line',
        relType: edge.rel_type,
        weight: weight,
        confidence: confidence,
        evidenceCount: evidenceCount,
      })
    }
  }

  // Assign random positions as seed for ForceAtlas2
  random.assign(g)

  // Size nodes by combined signal: visible degree + API global degree
  g.forEachNode((node, attrs) => {
    const visDegree = g.degree(node)
    const apiDeg = (attrs.apiDegree as number) ?? 0
    const effectiveDegree = Math.max(visDegree, apiDeg)
    const size = Math.min(4 + Math.sqrt(effectiveDegree) * 2.5, 25)
    g.setNodeAttribute(node, 'size', size)
  })

  // Apply ForceAtlas2 layout with improved settings
  const nodeCount = g.order
  const iterations = nodeCount > 500 ? 80 : nodeCount > 200 ? 150 : 250
  forceAtlas2.assign(g, {
    iterations,
    settings: {
      linLogMode: false,
      outboundAttractionDistribution: true,
      adjustSizes: true,
      edgeWeightInfluence: 1,
      scalingRatio: 2,
      strongGravityMode: false,
      gravity: 1,
      slowDown: 5,
      barnesHutOptimize: true,
      barnesHutTheta: 0.5,
    },
  })

  return g
}

// ── Tooltip component ──
function Tooltip({ text, x, y }: { text: string; x: number; y: number }) {
  return (
    <div
      className="absolute pointer-events-none z-50 px-2 py-1 rounded text-xs bg-popover text-popover-foreground border border-border shadow-md whitespace-pre-line max-w-xs"
      style={{ left: x + 14, top: y - 10 }}
    >
      {text}
    </div>
  )
}

// ── Legend component ──
function Legend({
  types,
  communityMode,
  communityLabels,
  edgeRelTypes,
}: {
  types: Set<string>
  communityMode: boolean
  communityLabels: { id: number; label: string }[]
  edgeRelTypes: Set<string>
}) {
  const nodeLegend = (() => {
    if (communityMode) {
      if (communityLabels.length === 0) return null
      return (
        <div className="absolute bottom-3 left-3 z-40 flex flex-wrap gap-x-3 gap-y-1 px-2 py-1.5 rounded bg-background/80 backdrop-blur-sm border border-border text-[10px] text-muted-foreground">
          {communityLabels.map((c) => (
            <span key={c.id} className="flex items-center gap-1">
              <span
                className="inline-block w-2 h-2 rounded-full"
                style={{ backgroundColor: getCommunityColor(c.id) }}
              />
              {c.label}
            </span>
          ))}
        </div>
      )
    }

    if (types.size === 0) return null
    const entries = Array.from(types).sort()
    return (
      <div className="absolute bottom-3 left-3 z-40 flex flex-wrap gap-x-3 gap-y-1 px-2 py-1.5 rounded bg-background/80 backdrop-blur-sm border border-border text-[10px] text-muted-foreground">
        {entries.map((t) => (
          <span key={t} className="flex items-center gap-1">
            <span
              className="inline-block w-2 h-2 rounded-full"
              style={{ backgroundColor: getNodeColor(t) }}
            />
            {t}
          </span>
        ))}
      </div>
    )
  })()

  // Edge color legend — show only relationship types present in current graph
  const edgeLegend = edgeRelTypes.size > 0 ? (
    <div className="absolute bottom-3 right-3 z-40 flex flex-wrap gap-x-3 gap-y-1 px-2 py-1.5 rounded bg-background/80 backdrop-blur-sm border border-border text-[10px] text-muted-foreground">
      {Array.from(edgeRelTypes).sort().map((rt) => (
        <span key={rt} className="flex items-center gap-1">
          <span
            className="inline-block w-2 h-2 rounded-full"
            style={{ backgroundColor: EDGE_COLORS[rt] ?? DEFAULT_EDGE_COLOR }}
          />
          {rt}
        </span>
      ))}
    </div>
  ) : null

  return (
    <>
      {nodeLegend}
      {edgeLegend}
    </>
  )
}

// ── Ego graph prompt (shown when no entity is selected) ──
function EgoPrompt({ onShowAll }: { onShowAll: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center h-full gap-4 text-muted-foreground">
      <svg
        xmlns="http://www.w3.org/2000/svg"
        width="48"
        height="48"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        className="opacity-30"
      >
        <circle cx="12" cy="12" r="2" />
        <circle cx="6" cy="6" r="1.5" />
        <circle cx="18" cy="6" r="1.5" />
        <circle cx="6" cy="18" r="1.5" />
        <circle cx="18" cy="18" r="1.5" />
        <line x1="12" y1="10" x2="7" y2="7" />
        <line x1="12" y1="10" x2="17" y2="7" />
        <line x1="12" y1="14" x2="7" y2="17" />
        <line x1="12" y1="14" x2="17" y2="17" />
      </svg>
      <div className="text-center">
        <p className="text-sm mb-1">Select an entity to explore its neighborhood</p>
        <p className="text-xs opacity-60">
          Click an entity in the Entities panel, or use the search above
        </p>
      </div>
      <button
        onClick={onShowAll}
        className="mt-2 h-8 px-4 text-xs rounded border border-border bg-muted text-muted-foreground hover:bg-accent hover:text-foreground transition-colors"
      >
        Show All
      </button>
    </div>
  )
}

// ── Main component ──
export function GraphPanel() {
  const selected = useSelectionStore((s) => s.selected)
  const focusEntity = useSelectionStore((s) => s.focusEntity)
  const select = useSelectionStore((s) => s.select)
  const openPanel = useWorkspaceStore((s) => s.openPanel)

  const containerRef = useRef<HTMLDivElement>(null)
  const sigmaRef = useRef<Sigma | null>(null)
  const graphRef = useRef<Graph | null>(null)

  // Use a ref for highlighted nodes so reducers always see latest without
  // recreating the Sigma instance on every search keystroke.
  const highlightedNodesRef = useRef<Set<string>>(new Set())
  const edgeFiltersRef = useRef<Set<string>>(new Set())
  const bestMatchRef = useRef<string | null>(null)
  // Hovered edge key for showing edge label on hover
  const hoveredEdgeRef = useRef<string | null>(null)

  const [searchQuery, setSearchQuery] = useState('')
  const [searchMatchCount, setSearchMatchCount] = useState(0)

  // Graph mode: 'ego' (default) or 'full' (explicit user action)
  const [graphMode, setGraphMode] = useState<'ego' | 'full'>('ego')
  const [egoDepth, setEgoDepth] = useState(2)

  const [typeFilters, setTypeFilters] = useState<Set<string>>(new Set())
  const [edgeFilters, setEdgeFilters] = useState<Set<string>>(new Set())
  // Keep ref in sync for reducer access
  useEffect(() => { edgeFiltersRef.current = edgeFilters }, [edgeFilters])
  const [showCommunities, setShowCommunities] = useState(false)
  const [communityCount, setCommunityCount] = useState(0)
  const [tooltip, setTooltip] = useState<{ text: string; x: number; y: number } | null>(null)
  // This state is only used to trigger re-renders for the UI, the ref is
  // what the reducers actually read
  const [, setHighlightTick] = useState(0)

  // Path finder state
  const [showPathFinder, setShowPathFinder] = useState(false)
  const [pathFrom, setPathFrom] = useState('')
  const [pathTo, setPathTo] = useState('')
  const [pathLoading, setPathLoading] = useState(false)
  const [pathError, setPathError] = useState<string | null>(null)

  // Edge CRUD state
  const [showAddEdge, setShowAddEdge] = useState(false)
  const [edgeFrom, setEdgeFrom] = useState('')
  const [edgeTo, setEdgeTo] = useState('')
  const [edgeRelType, setEdgeRelType] = useState('AlliedWith')
  const [edgeLoading, setEdgeLoading] = useState(false)
  const [edgeMsg, setEdgeMsg] = useState<string | null>(null)

  // Determine which entity to use for ego graph
  // Subscribe to both selected and focusEntity — prefer focusEntity, fallback to selected entity
  const entityName = useMemo(() => {
    if (focusEntity) return focusEntity
    if (selected?.type === 'entity') return selected.name
    return null
  }, [focusEntity, selected])

  // Auto-switch to ego mode when entity selection changes
  const prevEntityRef = useRef<string | null>(null)
  useEffect(() => {
    if (entityName && entityName !== prevEntityRef.current) {
      setGraphMode('ego')
    }
    prevEntityRef.current = entityName
  }, [entityName])

  // Determine if ego graph should be fetched
  const shouldFetchEgo = graphMode === 'ego' && !!entityName
  const shouldFetchFull = graphMode === 'full'

  // Fetch both datasets; only one is active at a time
  const { data: egoData, isLoading: egoLoading } = useEgoGraph(
    shouldFetchEgo ? entityName : null,
    egoDepth,
  )
  const { data: fullData, isLoading: fullLoading } = useGraph(shouldFetchFull)

  // Decide which graph data to use
  const useEgo = graphMode === 'ego' && !!entityName && !!egoData
  const graphData = useEgo ? egoData : (graphMode === 'full' ? fullData : null)
  const isLoading = useEgo ? egoLoading : fullLoading

  // Show the ego prompt when in ego mode with no entity selected
  const showEgoPrompt = graphMode === 'ego' && !entityName

  // Collect unique entity types for the legend and filter pills
  const entityTypes = useMemo(() => {
    if (!graphData) return new Set<string>()
    const types = new Set<string>()
    for (const node of graphData.nodes) {
      types.add(node.type.toLowerCase())
    }
    return types
  }, [graphData])

  // Serialize the Set for stable useMemo dependency
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const typeFilterKey = useMemo(() => [...typeFilters].sort().join(','), [typeFilters])

  // Build the graphology graph when data or type filter changes
  const builtGraph = useMemo(() => {
    if (!graphData || !graphData?.nodes?.length) return null
    return buildGraph(graphData, typeFilters)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graphData, typeFilterKey])

  // Build a lookup from node id -> GraphNode for click handling
  const nodeMap = useMemo(() => {
    if (!graphData) return new Map<string, GraphNode>()
    const m = new Map<string, GraphNode>()
    for (const n of graphData.nodes) {
      m.set(n.id, n)
    }
    return m
  }, [graphData])

  // Store nodeMap in a ref so event handlers can access without stale closures
  const nodeMapRef = useRef(nodeMap)
  nodeMapRef.current = nodeMap

  // ── Search logic ──
  const handleSearch = useCallback(
    (query: string) => {
      setSearchQuery(query)
      const g = graphRef.current
      const sigma = sigmaRef.current
      if (!g || !sigma || !query.trim()) {
        highlightedNodesRef.current = new Set()
        bestMatchRef.current = null
        setSearchMatchCount(0)
        setHighlightTick((t) => t + 1)
        sigma?.refresh()
        return
      }
      const q = query.trim().toLowerCase()
      const exact: string[] = []
      const prefix: string[] = []
      const wordBoundary: string[] = []
      const substring: string[] = []

      const wbRe = new RegExp(`(?:^|[^a-z])${q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`)

      g.forEachNode((node, attrs) => {
        if (!attrs.label) return
        const label = (attrs.label as string).toLowerCase()
        if (label === q) {
          exact.push(node)
        } else if (label.startsWith(q)) {
          prefix.push(node)
        } else if (wbRe.test(label)) {
          wordBoundary.push(node)
        } else if (label.includes(q)) {
          substring.push(node)
        }
      })

      const allMatches = [...exact, ...prefix, ...wordBoundary, ...substring]
      highlightedNodesRef.current = new Set(allMatches)
      const best = allMatches[0] ?? null
      bestMatchRef.current = best
      setSearchMatchCount(allMatches.length)
      setHighlightTick((t) => t + 1)
      sigma.refresh()
      if (best) {
        const pos = sigma.getNodeDisplayData(best)
        if (pos) {
          sigma.getCamera().animate(
            { x: pos.x, y: pos.y, ratio: 0.3 },
            { duration: 400 },
          )
        }
      }
    },
    [],
  )

  // ── Path finder ──
  const handleFindPath = useCallback(async () => {
    if (!pathFrom.trim() || !pathTo.trim()) return
    setPathLoading(true)
    setPathError(null)
    try {
      const res = await fetch(
        `/api/graph/path?from=${encodeURIComponent(pathFrom.trim())}&to=${encodeURIComponent(pathTo.trim())}`,
      )
      if (!res.ok) {
        const text = await res.text()
        setPathError(text || `Error ${res.status}`)
        return
      }
      const json = await res.json()
      const pathNodes: string[] = json.path_nodes ?? []
      if (pathNodes.length === 0) {
        setPathError('No path found between these entities.')
        return
      }
      highlightedNodesRef.current = new Set(pathNodes)
      bestMatchRef.current = pathNodes[0] ?? null
      setHighlightTick((t) => t + 1)
      sigmaRef.current?.refresh()
      if (pathNodes[0] && sigmaRef.current) {
        const pos = sigmaRef.current.getNodeDisplayData(pathNodes[0])
        if (pos) {
          sigmaRef.current.getCamera().animate(
            { x: pos.x, y: pos.y, ratio: 0.4 },
            { duration: 400 },
          )
        }
      }
    } catch (err) {
      setPathError(err instanceof Error ? err.message : 'Network error')
    } finally {
      setPathLoading(false)
    }
  }, [pathFrom, pathTo])

  const handleClearPath = useCallback(() => {
    highlightedNodesRef.current = new Set()
    bestMatchRef.current = null
    setHighlightTick((t) => t + 1)
    setPathError(null)
    sigmaRef.current?.refresh()
  }, [])

  // ── Add edge ──
  const handleAddEdge = useCallback(async () => {
    if (!edgeFrom.trim() || !edgeTo.trim()) return
    setEdgeLoading(true)
    setEdgeMsg(null)
    try {
      const form = new FormData()
      form.append('from', edgeFrom.trim())
      form.append('to', edgeTo.trim())
      form.append('rel_type', edgeRelType)
      const res = await fetch('/api/graph/edges', { method: 'POST', body: form })
      if (!res.ok) {
        const text = await res.text()
        setEdgeMsg(`Error: ${text || res.status}`)
        return
      }
      setEdgeMsg('Edge added. Refresh graph to see it.')
      setEdgeFrom('')
      setEdgeTo('')
    } catch (err) {
      setEdgeMsg(err instanceof Error ? err.message : 'Network error')
    } finally {
      setEdgeLoading(false)
    }
  }, [edgeFrom, edgeTo, edgeRelType])

  // ── Sigma instantiation and cleanup ──
  useEffect(() => {
    if (!containerRef.current || !builtGraph) return

    // Kill any existing instance
    if (sigmaRef.current) {
      sigmaRef.current.kill()
      sigmaRef.current = null
    }

    graphRef.current = builtGraph
    // Reset search highlight and community state when graph data changes
    highlightedNodesRef.current = new Set()
    bestMatchRef.current = null
    hoveredEdgeRef.current = null
    setShowCommunities(false)
    setCommunityCount(0)

    const sigma = new Sigma(builtGraph, containerRef.current, {
      allowInvalidContainer: true,
      defaultNodeColor: DEFAULT_COLOR,
      defaultEdgeColor: DEFAULT_EDGE_COLOR,
      defaultEdgeType: 'line',
      labelFont: 'Inter, ui-sans-serif, system-ui, sans-serif',
      labelSize: 11,
      labelWeight: '500',
      labelColor: { color: '#a1a1aa' },
      labelRenderedSizeThreshold: 6,
      labelDensity: 0.5,
      labelGridCellSize: 100,
      renderEdgeLabels: true,
      edgeLabelFont: 'Inter, ui-sans-serif, system-ui, sans-serif',
      edgeLabelSize: 9,
      edgeLabelColor: { color: '#888888' },
      hideEdgesOnMove: true,
      hideLabelsOnMove: false,
      stagePadding: 40,
      minEdgeThickness: 0.5,
      zIndex: true,

      // Reducers for search-highlight and edge-hover styling
      nodeReducer: (node, data) => {
        const hlSet = highlightedNodesRef.current
        if (hlSet.size === 0) return data
        const res = { ...data }
        if (hlSet.has(node)) {
          res.highlighted = true
          res.zIndex = 10
          res.size = (data.size ?? 5) * 1.3
          res.forceLabel = true
        } else {
          let isNeighbor = false
          for (const hl of hlSet) {
            if (builtGraph.areNeighbors(node, hl)) {
              isNeighbor = true
              break
            }
          }
          if (isNeighbor) {
            res.zIndex = 5
            res.color = data.color ? data.color + '99' : '#6b728099'
          } else {
            res.color = '#27272a'
            res.label = undefined
            res.size = 1.5
            res.zIndex = 0
          }
        }
        return res
      },
      edgeReducer: (edge, data) => {
        const res = { ...data }

        // Edge type filter: hide edges not in the filter set
        const ef = edgeFiltersRef.current
        if (ef.size > 0) {
          const relType = builtGraph.getEdgeAttribute(edge, 'relType') as string
          if (relType && !ef.has(relType)) {
            return { ...res, hidden: true }
          }
        }

        const hEdge = hoveredEdgeRef.current

        if (edge === hEdge) {
          res.forceLabel = true
          res.size = Math.max((data.size ?? 1) * 1.5, 2)
        } else {
          res.label = undefined
        }

        const hlSet = highlightedNodesRef.current
        if (hlSet.size === 0) return res
        const extremities = builtGraph.extremities(edge)
        const connected = extremities.some((e) => hlSet.has(e))
        if (!connected) {
          return { ...res, color: '#1a1a1e', hidden: true }
        }
        return { ...res, forceLabel: true, size: Math.max((data.size ?? 1) * 1.5, 2) }
      },
    })

    sigmaRef.current = sigma

    // ── Click handler: select node + highlight neighbors ──
    sigma.on('clickNode', ({ node }) => {
      const neighbors = new Set([node])
      builtGraph.forEachNeighbor(node, (neighbor) => neighbors.add(neighbor))
      highlightedNodesRef.current = neighbors
      bestMatchRef.current = node
      setHighlightTick((t) => t + 1)
      sigma.refresh()

      const apiNode = nodeMapRef.current.get(node)
      if (apiNode) {
        const entityName = apiNode.label || apiNode.id
        select({ type: 'entity', id: entityName, name: entityName })
      }
    })

    // ── Double click: open entity detail panel ──
    sigma.on('doubleClickNode', ({ node }) => {
      const apiNode = nodeMapRef.current.get(node)
      if (apiNode) {
        const entityName = apiNode.label || apiNode.id
        openPanel('entity-detail', { id: entityName })
      }
    })

    // ── Drag support: move nodes by dragging ──
    let draggedNode: string | null = null
    let isDragging = false

    sigma.on('downNode', ({ node }) => {
      draggedNode = node
      isDragging = false
      sigma.getCamera().disable()
    })

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    sigma.getMouseCaptor().on('mousemovebody', (e: any) => {
      if (!draggedNode) return
      isDragging = true
      const pos = sigma.viewportToGraph(e)
      builtGraph.setNodeAttribute(draggedNode, 'x', pos.x)
      builtGraph.setNodeAttribute(draggedNode, 'y', pos.y)
    })

    sigma.getMouseCaptor().on('mouseup', () => {
      if (draggedNode) {
        sigma.getCamera().enable()
        if (isDragging) {
          draggedNode = null
          isDragging = false
          return
        }
        draggedNode = null
        isDragging = false
      }
    })

    // Click on empty space: clear highlight
    sigma.on('clickStage', () => {
      highlightedNodesRef.current = new Set()
      bestMatchRef.current = null
      setHighlightTick((t) => t + 1)
      sigma.refresh()
    })

    // ── Hover handlers: tooltip ──
    sigma.on('enterNode', ({ node }) => {
      const displayData = sigma.getNodeDisplayData(node)
      if (displayData) {
        const containerRect = containerRef.current?.getBoundingClientRect()
        if (containerRect) {
          const viewPos = sigma.framedGraphToViewport({
            x: displayData.x,
            y: displayData.y,
          })
          const label = builtGraph.getNodeAttribute(node, 'label') as string || node
          const eType = builtGraph.getNodeAttribute(node, 'entityType') as string || ''
          const degree = builtGraph.degree(node)
          const parts = [label]
          if (eType) parts.push(`[${eType}]`)
          if (degree > 0) parts.push(`${degree} connections`)
          setTooltip({
            text: parts.join(' \u2014 '),
            x: viewPos.x,
            y: viewPos.y,
          })
        }
      }
      if (containerRef.current) {
        containerRef.current.style.cursor = 'pointer'
      }
    })

    sigma.on('leaveNode', () => {
      setTooltip(null)
      if (containerRef.current) {
        containerRef.current.style.cursor = 'default'
      }
    })

    // ── Edge hover: show tooltip with relationship type, weight, confidence, evidence_count ──
    sigma.on('enterEdge', ({ edge }) => {
      hoveredEdgeRef.current = edge
      sigma.refresh()
      const extremities = builtGraph.extremities(edge)
      const relType = builtGraph.getEdgeAttribute(edge, 'relType') as string || ''
      const weight = builtGraph.getEdgeAttribute(edge, 'weight') as number | undefined
      const confidence = builtGraph.getEdgeAttribute(edge, 'confidence') as number | undefined
      const evidenceCount = builtGraph.getEdgeAttribute(edge, 'evidenceCount') as number | undefined

      if (extremities.length === 2) {
        const srcLabel = builtGraph.getNodeAttribute(extremities[0], 'label') as string || extremities[0]
        const tgtLabel = builtGraph.getNodeAttribute(extremities[1], 'label') as string || extremities[1]
        const srcData = sigma.getNodeDisplayData(extremities[0])
        const tgtData = sigma.getNodeDisplayData(extremities[1])
        if (srcData && tgtData) {
          const midGraph = { x: (srcData.x + tgtData.x) / 2, y: (srcData.y + tgtData.y) / 2 }
          const viewPos = sigma.framedGraphToViewport(midGraph)

          // Build multi-line tooltip
          const lines = [`${srcLabel} \u2014[${relType}]\u2192 ${tgtLabel}`]
          if (weight != null) lines.push(`Weight: ${weight.toFixed(2)}`)
          if (confidence != null) lines.push(`Confidence: ${(confidence * 100).toFixed(0)}%`)
          if (evidenceCount != null) lines.push(`Evidence: ${evidenceCount}`)

          setTooltip({
            text: lines.join('\n'),
            x: viewPos.x,
            y: viewPos.y,
          })
        }
      }
      if (containerRef.current) {
        containerRef.current.style.cursor = 'pointer'
      }
    })

    sigma.on('leaveEdge', () => {
      hoveredEdgeRef.current = null
      sigma.refresh()
      setTooltip(null)
      if (containerRef.current) {
        containerRef.current.style.cursor = 'default'
      }
    })

    // Cleanup
    return () => {
      sigma.kill()
      sigmaRef.current = null
      graphRef.current = null
    }
    // select and openPanel are stable zustand refs, safe to include
  }, [builtGraph, select, openPanel])

  // ── Community detection toggle ──
  useEffect(() => {
    const g = graphRef.current
    const sigma = sigmaRef.current
    if (!g || !sigma) {
      setCommunityCount(0)
      return
    }

    if (showCommunities) {
      if (g.size === 0) {
        setCommunityCount(0)
        return
      }

      louvain.assign(g)

      const communities = new Set<number>()
      g.forEachNode((_node, attrs) => {
        communities.add(attrs.community as number)
      })
      setCommunityCount(communities.size)

      g.forEachNode((node, attrs) => {
        g.setNodeAttribute(node, 'color', getCommunityColor(attrs.community as number))
      })
    } else {
      setCommunityCount(0)
      g.forEachNode((node, attrs) => {
        g.setNodeAttribute(node, 'color', getNodeColor(attrs.entityType as string))
      })
    }

    sigma.refresh()
  }, [showCommunities, builtGraph])

  // Stats line
  const stats = builtGraph
    ? `${builtGraph.order} nodes, ${builtGraph.size} edges`
    : null

  // Sorted entity types for filter pills
  const sortedTypes = useMemo(() => Array.from(entityTypes).sort(), [entityTypes])

  // Community labels
  const communityLabels = useMemo(() => {
    const g = graphRef.current
    if (!showCommunities || !g || communityCount === 0) return []

    const communityNodes = new Map<number, { types: Map<string, number>; names: string[] }>()
    g.forEachNode((_node, attrs) => {
      const cid = attrs.community as number
      if (!communityNodes.has(cid)) {
        communityNodes.set(cid, { types: new Map(), names: [] })
      }
      const entry = communityNodes.get(cid)!
      const t = (attrs.entityType as string || '').toLowerCase()
      if (t) entry.types.set(t, (entry.types.get(t) ?? 0) + 1)
      if (attrs.label) entry.names.push(attrs.label as string)
    })

    const labels: { id: number; label: string }[] = []
    for (const [cid, info] of Array.from(communityNodes.entries()).sort((a, b) => a[0] - b[0])) {
      let topType = ''
      let topCount = 0
      for (const [t, c] of info.types) {
        if (c > topCount) { topType = t; topCount = c }
      }
      const topNames = info.names.slice(0, 3).join(', ')
      const total = info.names.length
      let label: string
      if (total > 5 && topCount > 3) {
        label = `${topCount} ${topType}s, ${total - topCount} others`
      } else if (total > 3) {
        label = `${topNames} +${total - 3}`
      } else {
        label = topNames
      }
      labels.push({ id: cid, label })
    }
    return labels
  }, [showCommunities, communityCount, builtGraph])

  // Collect edge relationship types present in the current graph
  const activeEdgeRelTypes = useMemo(() => {
    const g = builtGraph
    if (!g || g.size === 0) return new Set<string>()
    const types = new Set<string>()
    g.forEachEdge((_edge, attrs) => {
      const rt = attrs.relType as string
      if (rt) types.add(rt)
    })
    return types
  }, [builtGraph])

  return (
    <div className="flex flex-col h-full">
      {/* ── Toolbar ── */}
      <div className="flex items-center gap-2 p-2 border-b border-border shrink-0 flex-wrap">
        {/* Search input */}
        <div className="relative">
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => handleSearch(e.target.value)}
            placeholder="Search nodes..."
            className="h-7 px-2 text-xs rounded bg-muted border border-border text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring w-44"
          />
          {searchQuery && searchMatchCount > 0 && (
            <span className="absolute -top-1.5 -right-1.5 px-1 min-w-[16px] text-center text-[9px] leading-4 rounded-full bg-primary text-primary-foreground font-medium">
              {searchMatchCount}
            </span>
          )}
        </div>

        {/* Clear search */}
        {searchQuery && (
          <button
            onClick={() => handleSearch('')}
            className="h-7 px-1.5 text-xs text-muted-foreground hover:text-foreground"
            title="Clear search"
          >
            &times;
          </button>
        )}

        {/* Ego / Full graph toggle */}
        <button
          onClick={() => setGraphMode(graphMode === 'ego' ? 'full' : 'ego')}
          className={`h-7 px-2.5 text-xs rounded border transition-colors ${
            graphMode === 'ego'
              ? 'bg-primary text-primary-foreground border-primary'
              : 'bg-muted text-muted-foreground border-border hover:bg-accent'
          }`}
        >
          {graphMode === 'ego' ? 'Ego Graph' : 'Full Graph'}
        </button>

        {/* Show All button (when in ego mode, loads full graph) */}
        {graphMode === 'ego' && (
          <button
            onClick={() => setGraphMode('full')}
            className="h-7 px-2.5 text-xs rounded border border-border bg-muted text-muted-foreground hover:bg-accent transition-colors"
          >
            Show All
          </button>
        )}

        {/* Depth slider (only when ego mode is active and entity is selected) */}
        {graphMode === 'ego' && entityName && (
          <div className="flex items-center gap-1.5">
            <span className="text-[10px] text-muted-foreground">Depth:</span>
            <input
              type="range"
              min={1}
              max={3}
              value={egoDepth}
              onChange={(e) => setEgoDepth(parseInt(e.target.value))}
              className="w-16 h-4 accent-primary"
              title={`Ego graph depth: ${egoDepth}`}
            />
            <span className="text-[10px] text-muted-foreground w-3">{egoDepth}</span>
          </div>
        )}

        {/* Community detection toggle */}
        <button
          onClick={() => setShowCommunities((v) => !v)}
          className={`h-7 px-2.5 text-xs rounded border transition-colors flex items-center gap-1.5 ${
            showCommunities
              ? 'bg-primary text-primary-foreground border-primary'
              : 'bg-muted text-muted-foreground border-border hover:bg-accent'
          }`}
          title="Toggle Louvain community detection"
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            width="13"
            height="13"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <circle cx="6" cy="6" r="3" />
            <circle cx="18" cy="6" r="3" />
            <circle cx="12" cy="18" r="3" />
            <line x1="8.5" y1="7.5" x2="10.5" y2="16" />
            <line x1="15.5" y1="7.5" x2="13.5" y2="16" />
            <line x1="9" y1="6" x2="15" y2="6" />
          </svg>
          Communities
          {showCommunities && communityCount > 0 && (
            <span className="ml-0.5 px-1.5 py-0 rounded-full bg-primary-foreground/20 text-[10px] leading-4 font-medium">
              {communityCount}
            </span>
          )}
        </button>

        {/* Stats summary */}
        <span className="ml-auto text-[11px] text-muted-foreground">
          {useEgo && entityName
            ? `Ego: ${entityName}`
            : graphMode === 'full' ? 'Full graph' : 'Ego graph'}
          {stats && ` \u2014 ${stats}`}
        </span>
      </div>

      {/* ── Entity type filter checkboxes ── */}
      <div className="flex items-center gap-1.5 px-2 py-1.5 border-b border-border shrink-0 overflow-x-auto">
        <span className="text-[10px] text-muted-foreground uppercase tracking-wide mr-0.5">Types:</span>
        <button
          onClick={() => setTypeFilters(new Set())}
          className={`h-5 px-2 text-[10px] rounded-full border transition-colors ${
            typeFilters.size === 0
              ? 'bg-primary text-primary-foreground border-primary'
              : 'bg-muted text-muted-foreground border-border hover:bg-accent'
          }`}
        >
          All
        </button>
        {ENTITY_TYPE_FILTERS.map((t) => (
          <button
            key={t}
            onClick={() => setTypeFilters((prev) => {
              const next = new Set(prev)
              if (next.has(t)) next.delete(t)
              else next.add(t)
              return next
            })}
            className={`h-5 px-2 text-[10px] rounded-full border transition-colors flex items-center gap-1 ${
              typeFilters.has(t)
                ? 'bg-primary text-primary-foreground border-primary'
                : 'bg-muted text-muted-foreground border-border hover:bg-accent'
            }`}
          >
            <span
              className="inline-block w-1.5 h-1.5 rounded-full"
              style={{ backgroundColor: getNodeColor(t) }}
            />
            {t}
          </button>
        ))}
        {/* Also show any entity types present in data but not in the standard filter list */}
        {sortedTypes.filter((t) => !ENTITY_TYPE_FILTERS.includes(t)).map((t) => (
          <button
            key={t}
            onClick={() => setTypeFilters((prev) => {
              const next = new Set(prev)
              if (next.has(t)) next.delete(t)
              else next.add(t)
              return next
            })}
            className={`h-5 px-2 text-[10px] rounded-full border transition-colors flex items-center gap-1 ${
              typeFilters.has(t)
                ? 'bg-primary text-primary-foreground border-primary'
                : 'bg-muted text-muted-foreground border-border hover:bg-accent'
            }`}
          >
            <span
              className="inline-block w-1.5 h-1.5 rounded-full"
              style={{ backgroundColor: getNodeColor(t) }}
            />
            {t}
          </button>
        ))}
      </div>

      {/* ── Relationship type filter checkboxes ── */}
      <div className="flex items-center gap-1.5 px-2 py-1.5 border-b border-border shrink-0 overflow-x-auto">
        <span className="text-[10px] text-muted-foreground uppercase tracking-wide mr-0.5">Edges:</span>
        <button
          onClick={() => { setEdgeFilters(new Set()); sigmaRef.current?.refresh() }}
          className={`h-5 px-2 text-[10px] rounded-full border transition-colors ${
            edgeFilters.size === 0
              ? 'bg-primary text-primary-foreground border-primary'
              : 'bg-muted text-muted-foreground border-border hover:bg-accent'
          }`}
        >
          All
        </button>
        {TOP_REL_TYPES.map((rt) => (
          <button
            key={rt}
            onClick={() => {
              setEdgeFilters((prev) => {
                const next = new Set(prev)
                if (next.has(rt)) next.delete(rt)
                else next.add(rt)
                return next
              })
              setTimeout(() => sigmaRef.current?.refresh(), 0)
            }}
            className={`h-5 px-2 text-[10px] rounded-full border transition-colors flex items-center gap-1 ${
              edgeFilters.has(rt)
                ? 'bg-primary text-primary-foreground border-primary'
                : 'bg-muted text-muted-foreground border-border hover:bg-accent'
            }`}
          >
            <span
              className="inline-block w-1.5 h-1.5 rounded-full"
              style={{ backgroundColor: EDGE_COLORS[rt] ?? DEFAULT_EDGE_COLOR }}
            />
            {rt}
          </button>
        ))}
        {/* Also show active edge types not in the top 5 */}
        {Array.from(activeEdgeRelTypes).sort().filter((rt) => !TOP_REL_TYPES.includes(rt)).map((rt) => (
          <button
            key={rt}
            onClick={() => {
              setEdgeFilters((prev) => {
                const next = new Set(prev)
                if (next.has(rt)) next.delete(rt)
                else next.add(rt)
                return next
              })
              setTimeout(() => sigmaRef.current?.refresh(), 0)
            }}
            className={`h-5 px-2 text-[10px] rounded-full border transition-colors flex items-center gap-1 ${
              edgeFilters.has(rt)
                ? 'bg-primary text-primary-foreground border-primary'
                : 'bg-muted text-muted-foreground border-border hover:bg-accent'
            }`}
          >
            <span
              className="inline-block w-1.5 h-1.5 rounded-full"
              style={{ backgroundColor: EDGE_COLORS[rt] ?? DEFAULT_EDGE_COLOR }}
            />
            {rt}
          </button>
        ))}
      </div>

      {/* ── Path Finder (collapsible) ── */}
      <div className="border-b border-border shrink-0">
        <button
          onClick={() => setShowPathFinder((v) => !v)}
          className="w-full flex items-center gap-1.5 px-2 py-1 text-[11px] text-muted-foreground hover:text-foreground transition-colors"
        >
          <span className="text-[9px]">{showPathFinder ? '\u25BC' : '\u25B6'}</span>
          Path Finder
        </button>
        {showPathFinder && (
          <div className="flex items-center gap-1.5 px-2 pb-2 flex-wrap">
            <input
              type="text"
              value={pathFrom}
              onChange={(e) => setPathFrom(e.target.value)}
              placeholder="From entity..."
              className="h-6 px-2 text-[11px] rounded bg-muted border border-border text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring w-32"
            />
            <input
              type="text"
              value={pathTo}
              onChange={(e) => setPathTo(e.target.value)}
              placeholder="To entity..."
              className="h-6 px-2 text-[11px] rounded bg-muted border border-border text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring w-32"
            />
            <button
              onClick={handleFindPath}
              disabled={pathLoading || !pathFrom.trim() || !pathTo.trim()}
              className="h-6 px-2.5 text-[11px] rounded bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 transition-colors"
            >
              {pathLoading ? '...' : 'Find Path'}
            </button>
            {highlightedNodesRef.current.size > 0 && (
              <button
                onClick={handleClearPath}
                className="h-6 px-2 text-[11px] rounded bg-muted text-muted-foreground border border-border hover:bg-accent transition-colors"
              >
                Clear
              </button>
            )}
            {pathError && (
              <span className="text-[10px] text-destructive">{pathError}</span>
            )}
          </div>
        )}
      </div>

      {/* ── Add Edge (collapsible) ── */}
      <div className="border-b border-border shrink-0">
        <button
          onClick={() => setShowAddEdge((v) => !v)}
          className="w-full flex items-center gap-1.5 px-2 py-1 text-[11px] text-muted-foreground hover:text-foreground transition-colors"
        >
          <span className="text-[9px]">{showAddEdge ? '\u25BC' : '\u25B6'}</span>
          Add Edge
        </button>
        {showAddEdge && (
          <div className="flex items-center gap-1.5 px-2 pb-2 flex-wrap">
            <input
              type="text"
              value={edgeFrom}
              onChange={(e) => setEdgeFrom(e.target.value)}
              placeholder="From entity..."
              className="h-6 px-2 text-[11px] rounded bg-muted border border-border text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring w-28"
            />
            <input
              type="text"
              value={edgeTo}
              onChange={(e) => setEdgeTo(e.target.value)}
              placeholder="To entity..."
              className="h-6 px-2 text-[11px] rounded bg-muted border border-border text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring w-28"
            />
            <select
              value={edgeRelType}
              onChange={(e) => setEdgeRelType(e.target.value)}
              className="h-6 px-1.5 text-[11px] rounded bg-muted border border-border text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
            >
              {EDGE_REL_TYPES.map((rt) => (
                <option key={rt} value={rt}>{rt}</option>
              ))}
            </select>
            <button
              onClick={handleAddEdge}
              disabled={edgeLoading || !edgeFrom.trim() || !edgeTo.trim()}
              className="h-6 px-2.5 text-[11px] rounded bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 transition-colors"
            >
              {edgeLoading ? '...' : 'Add'}
            </button>
            {edgeMsg && (
              <span className={`text-[10px] ${edgeMsg.startsWith('Error') ? 'text-destructive' : 'text-muted-foreground'}`}>
                {edgeMsg}
              </span>
            )}
          </div>
        )}
      </div>

      {/* ── Graph container ── */}
      <div className="flex-1 relative overflow-hidden" style={{ backgroundColor: '#09090b' }}>
        {/* Ego prompt: shown when panel opens with no entity selected in ego mode */}
        {showEgoPrompt && (
          <EgoPrompt onShowAll={() => setGraphMode('full')} />
        )}

        {/* Loading spinner */}
        {isLoading && !graphData && !showEgoPrompt && (
          <div className="flex items-center justify-center h-full text-sm text-muted-foreground">
            <svg
              className="animate-spin h-5 w-5 mr-2 text-muted-foreground"
              viewBox="0 0 24 24"
              fill="none"
            >
              <circle
                className="opacity-25"
                cx="12"
                cy="12"
                r="10"
                stroke="currentColor"
                strokeWidth="4"
              />
              <path
                className="opacity-75"
                fill="currentColor"
                d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
              />
            </svg>
            Loading graph data...
          </div>
        )}

        {/* Empty state */}
        {!showEgoPrompt && graphData && graphData.nodes.length === 0 && (
          <div className="flex items-center justify-center h-full text-sm text-muted-foreground">
            No graph data available
          </div>
        )}

        {/* Sigma renders into this div */}
        <div
          ref={containerRef}
          className="absolute inset-0"
          style={{
            visibility: graphData && graphData.nodes.length > 0 ? 'visible' : 'hidden',
          }}
        />

        {/* Tooltip overlay */}
        {tooltip && <Tooltip text={tooltip.text} x={tooltip.x} y={tooltip.y} />}

        {/* Legend */}
        <Legend types={entityTypes} communityMode={showCommunities} communityLabels={communityLabels} edgeRelTypes={activeEdgeRelTypes} />
      </div>
    </div>
  )
}
