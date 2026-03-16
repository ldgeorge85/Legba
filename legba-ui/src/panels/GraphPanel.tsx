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
}
const DEFAULT_COLOR = '#6b7280'

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
function buildGraph(data: GraphData): Graph {
  const g = new Graph({ multi: true, type: 'undirected' })

  // Track seen node IDs to skip duplicates from API
  const seenNodes = new Set<string>()

  for (const node of data.nodes) {
    if (seenNodes.has(node.id)) continue
    seenNodes.add(node.id)
    g.addNode(node.id, {
      label: node.label,
      entityType: node.type,
      x: 0,
      y: 0,
      size: 4,
      color: getNodeColor(node.type),
    })
  }

  // Add edges, skipping any that reference missing nodes or are self-loops
  for (const edge of data.edges) {
    if (edge.source === edge.target) continue
    if (!g.hasNode(edge.source) || !g.hasNode(edge.target)) continue
    const edgeKey = `${edge.source}--${edge.rel_type}--${edge.target}`
    if (!g.hasEdge(edgeKey)) {
      g.addEdgeWithKey(edgeKey, edge.source, edge.target, {
        label: edge.rel_type,
        color: '#444444',
        size: 1,
        type: 'line',
      })
    }
  }

  // Assign random positions as seed for ForceAtlas2
  random.assign(g)

  // Size by degree: base 3, scaled by degree, capped at 20
  g.forEachNode((node) => {
    const degree = g.degree(node)
    g.setNodeAttribute(node, 'size', Math.min(3 + degree * 0.8, 20))
  })

  // Run ForceAtlas2 synchronously
  // Fewer iterations for larger graphs to keep UI responsive
  const nodeCount = g.order
  const iterations = nodeCount > 500 ? 80 : nodeCount > 200 ? 150 : 250
  forceAtlas2.assign(g, {
    iterations,
    settings: {
      linLogMode: false,
      outboundAttractionDistribution: true,
      adjustSizes: true,
      edgeWeightInfluence: 1,
      scalingRatio: nodeCount > 200 ? 20 : 10,
      strongGravityMode: false,
      gravity: 1,
      slowDown: 5,
      barnesHutOptimize: nodeCount > 100,
      barnesHutTheta: 0.5,
    },
  })

  return g
}

// ── Tooltip component ──
function Tooltip({ text, x, y }: { text: string; x: number; y: number }) {
  return (
    <div
      className="absolute pointer-events-none z-50 px-2 py-1 rounded text-xs bg-popover text-popover-foreground border border-border shadow-md whitespace-nowrap"
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
  communityCount,
}: {
  types: Set<string>
  communityMode: boolean
  communityCount: number
}) {
  if (communityMode) {
    if (communityCount === 0) return null
    const entries = Array.from({ length: communityCount }, (_, i) => i)
    return (
      <div className="absolute bottom-3 left-3 z-40 flex flex-wrap gap-x-3 gap-y-1 px-2 py-1.5 rounded bg-background/80 backdrop-blur-sm border border-border text-[10px] text-muted-foreground">
        {entries.map((c) => (
          <span key={c} className="flex items-center gap-1">
            <span
              className="inline-block w-2 h-2 rounded-full"
              style={{ backgroundColor: getCommunityColor(c) }}
            />
            C{c}
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
}

// ── Main component ──
export function GraphPanel() {
  const selected = useSelectionStore((s) => s.selected)
  const select = useSelectionStore((s) => s.select)
  const openPanel = useWorkspaceStore((s) => s.openPanel)

  const containerRef = useRef<HTMLDivElement>(null)
  const sigmaRef = useRef<Sigma | null>(null)
  const graphRef = useRef<Graph | null>(null)

  // Use a ref for highlighted nodes so reducers always see latest without
  // recreating the Sigma instance on every search keystroke.
  // The first entry is the "best" match (camera target); all are highlighted.
  const highlightedNodesRef = useRef<Set<string>>(new Set())
  const bestMatchRef = useRef<string | null>(null)

  const [searchQuery, setSearchQuery] = useState('')
  const [showEgo, setShowEgo] = useState(true)
  const [showCommunities, setShowCommunities] = useState(false)
  const [communityCount, setCommunityCount] = useState(0)
  const [tooltip, setTooltip] = useState<{ text: string; x: number; y: number } | null>(null)
  // This state is only used to trigger re-renders for the UI, the ref is
  // what the reducers actually read
  const [, setHighlightTick] = useState(0)

  // Determine which entity to use for ego graph
  const entityName = selected?.type === 'entity' ? selected.name : null

  // Fetch both datasets; only one is active at a time
  const { data: egoData, isLoading: egoLoading } = useEgoGraph(
    showEgo ? entityName : null,
    2,
  )
  const { data: fullData, isLoading: fullLoading } = useGraph()

  // Decide which graph data to use
  const useEgo = showEgo && !!entityName && !!egoData
  const graphData = useEgo ? egoData : fullData
  const isLoading = useEgo ? egoLoading : fullLoading

  // Collect unique entity types for the legend
  const entityTypes = useMemo(() => {
    if (!graphData) return new Set<string>()
    const types = new Set<string>()
    for (const node of graphData.nodes) {
      types.add(node.type.toLowerCase())
    }
    return types
  }, [graphData])

  // Build the graphology graph when data changes
  const builtGraph = useMemo(() => {
    if (!graphData || graphData.nodes.length === 0) return null
    return buildGraph(graphData)
  }, [graphData])

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
  // Rank: exact match > starts-with > word-boundary match > substring includes
  const handleSearch = useCallback(
    (query: string) => {
      setSearchQuery(query)
      const g = graphRef.current
      const sigma = sigmaRef.current
      if (!g || !sigma || !query.trim()) {
        highlightedNodesRef.current = new Set()
        bestMatchRef.current = null
        setHighlightTick((t) => t + 1)
        sigma?.refresh()
        return
      }
      const q = query.trim().toLowerCase()
      const exact: string[] = []
      const prefix: string[] = []
      const wordBoundary: string[] = []
      const substring: string[] = []

      // Word-boundary regex: match q preceded by start-of-string or non-letter
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

      // All matches highlighted; best match gets camera focus
      const allMatches = [...exact, ...prefix, ...wordBoundary, ...substring]
      highlightedNodesRef.current = new Set(allMatches)
      const best = allMatches[0] ?? null
      bestMatchRef.current = best
      setHighlightTick((t) => t + 1)
      sigma.refresh()
      if (best) {
        // Center camera on best match
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
    setShowCommunities(false)
    setCommunityCount(0)

    const sigma = new Sigma(builtGraph, containerRef.current, {
      allowInvalidContainer: true,
      defaultNodeColor: DEFAULT_COLOR,
      defaultEdgeColor: '#444444',
      defaultEdgeType: 'line',
      labelFont: 'Inter, ui-sans-serif, system-ui, sans-serif',
      labelSize: 11,
      labelWeight: '500',
      labelColor: { color: '#a1a1aa' },
      labelRenderedSizeThreshold: 6,
      labelDensity: 0.5,
      labelGridCellSize: 100,
      renderEdgeLabels: false,
      hideEdgesOnMove: true,
      hideLabelsOnMove: false,
      stagePadding: 40,
      minEdgeThickness: 0.5,
      zIndex: true,

      // Reducers for search-highlight styling
      nodeReducer: (node, data) => {
        const hlSet = highlightedNodesRef.current
        if (hlSet.size === 0) return data
        const res = { ...data }
        if (hlSet.has(node)) {
          res.highlighted = true
          res.zIndex = 10
        } else {
          // Check if neighbor of ANY highlighted node
          let isNeighbor = false
          for (const hl of hlSet) {
            if (builtGraph.areNeighbors(node, hl)) {
              isNeighbor = true
              break
            }
          }
          if (isNeighbor) {
            res.zIndex = 5
          } else {
            res.color = '#1f1f23'
            res.label = null
            res.zIndex = 0
          }
        }
        return res
      },
      edgeReducer: (edge, data) => {
        const hlSet = highlightedNodesRef.current
        if (hlSet.size === 0) return data
        const extremities = builtGraph.extremities(edge)
        const connected = extremities.some((e) => hlSet.has(e))
        if (!connected) {
          return { ...data, color: '#1a1a1e', hidden: true }
        }
        return { ...data, color: '#666666', size: 2 }
      },
    })

    sigmaRef.current = sigma

    // ── Click handler: first click highlights, second click opens detail ──
    sigma.on('clickNode', ({ node }) => {
      const hlSet = highlightedNodesRef.current
      const wasHighlighted = hlSet.has(node)

      // First click: highlight this node and its neighbors
      highlightedNodesRef.current = new Set([node])
      bestMatchRef.current = node
      setHighlightTick((t) => t + 1)
      sigma.refresh()

      // Second click on same node: open the detail panel
      if (wasHighlighted) {
        const apiNode = nodeMapRef.current.get(node)
        if (apiNode) {
          // entity_id from AGE properties, or use the name for lookup
          const entityId = (apiNode.properties?.entity_id as string) || ''
          select({
            type: 'entity',
            id: entityId || apiNode.id,
            name: apiNode.label,
          })
          openPanel('entity-detail')
        }
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
        // viewportToFramedGraph gives us viewport pixel coords relative to the container
        const containerRect = containerRef.current?.getBoundingClientRect()
        if (containerRect) {
          // sigma.framedGraphToViewport converts from the sigma coordinate system to viewport pixels
          const viewPos = sigma.framedGraphToViewport({
            x: displayData.x,
            y: displayData.y,
          })
          setTooltip({
            text: builtGraph.getNodeAttribute(node, 'label') as string || node,
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

    // Cleanup
    return () => {
      sigma.kill()
      sigmaRef.current = null
      graphRef.current = null
    }
    // select and openPanel are stable zustand refs, safe to include
  }, [builtGraph, select, openPanel])

  // ── Community detection toggle ──
  // Runs Louvain on the current graph and recolors nodes without recreating Sigma
  useEffect(() => {
    const g = graphRef.current
    const sigma = sigmaRef.current
    if (!g || !sigma) {
      setCommunityCount(0)
      return
    }

    if (showCommunities) {
      // Need at least 1 edge for meaningful community detection
      if (g.size === 0) {
        setCommunityCount(0)
        return
      }

      // Run Louvain — assigns 'community' attribute to each node
      louvain.assign(g)

      // Count distinct communities
      const communities = new Set<number>()
      g.forEachNode((_node, attrs) => {
        communities.add(attrs.community as number)
      })
      setCommunityCount(communities.size)

      // Recolor nodes by community
      g.forEachNode((node, attrs) => {
        g.setNodeAttribute(node, 'color', getCommunityColor(attrs.community as number))
      })
    } else {
      // Restore entity-type coloring
      setCommunityCount(0)
      g.forEachNode((node, attrs) => {
        g.setNodeAttribute(node, 'color', getNodeColor(attrs.entityType as string))
      })
    }

    sigma.refresh()
  }, [showCommunities, builtGraph])

  // Stats line
  const stats = graphData
    ? `${graphData.nodes.length} nodes, ${graphData.edges.length} edges`
    : null

  return (
    <div className="flex flex-col h-full">
      {/* ── Controls bar ── */}
      <div className="flex items-center gap-2 p-2 border-b border-border shrink-0">
        {/* Search input */}
        <input
          type="text"
          value={searchQuery}
          onChange={(e) => handleSearch(e.target.value)}
          placeholder="Search nodes..."
          className="h-7 px-2 text-xs rounded bg-muted border border-border text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring w-44"
        />

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

        {/* Ego / Full graph toggle (only when an entity is selected) */}
        {entityName && (
          <button
            onClick={() => setShowEgo((v) => !v)}
            className={`h-7 px-2.5 text-xs rounded border transition-colors ${
              useEgo
                ? 'bg-primary text-primary-foreground border-primary'
                : 'bg-muted text-muted-foreground border-border hover:bg-accent'
            }`}
          >
            {useEgo ? 'Ego graph' : 'Full graph'}
          </button>
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
            : 'Full graph'}
          {stats && ` \u2014 ${stats}`}
        </span>
      </div>

      {/* ── Graph container ── */}
      <div className="flex-1 relative overflow-hidden bg-background">
        {isLoading && !graphData ? (
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
        ) : graphData && graphData.nodes.length === 0 ? (
          <div className="flex items-center justify-center h-full text-sm text-muted-foreground">
            No graph data available
          </div>
        ) : null}

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
        <Legend types={entityTypes} communityMode={showCommunities} communityCount={communityCount} />
      </div>
    </div>
  )
}
