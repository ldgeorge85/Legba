/**
 * The Flow — F.A layout. Runs the {@link GraphProjection} through ELK's
 * layered algorithm (left→right) and writes the resulting geometry onto a
 * fresh copy of the projection. The input is never mutated.
 *
 * Lanes: ELK's layered algorithm derives columns from edge direction, but the
 * registry graph is not a clean DAG (packs grant *into* analysts/targets, a
 * target with no resolved sources gets fanned-out edges, etc.), so ELK can
 * place a node off its semantic lane. We therefore bias each node's final x by
 * KIND_LANE[kind] — sources leftmost (lane 0) → packs rightmost (lane 3) — so
 * the family columns stay legible regardless of how ELK ordered the layers.
 */
import ELK from 'elkjs/lib/elk.bundled.js'
import type { GraphProjection, FlowNode, FlowNodeKind } from './types'
import { KIND_LANE } from './types'

const NODE_WIDTH = 190
const NODE_HEIGHT = 64

/** Horizontal nudge per lane so families read left→right even when ELK doesn't
 *  separate them cleanly. One lane-step ≈ one node-width + the inter-layer gap. */
const LANE_STRIDE = NODE_WIDTH + 120

const LAYOUT_OPTIONS: Record<string, string> = {
  'elk.algorithm': 'layered',
  'elk.direction': 'RIGHT',
  'elk.layered.spacing.nodeNodeBetweenLayers': '120',
  'elk.spacing.nodeNode': '30',
}

/** Minimal subset of the ELK result we read back. */
interface ElkLaidOutNode {
  id: string
  x?: number
  y?: number
}
interface ElkLaidOutGraph {
  children?: ElkLaidOutNode[]
}

function laneOf(node: FlowNode): number {
  return KIND_LANE[node.data.kind as FlowNodeKind] ?? 0
}

/**
 * Lay out the projection with ELK and return a NEW {@link GraphProjection}
 * whose nodes carry computed positions. Edges are passed through unchanged.
 * An empty graph (no nodes) is returned as-is.
 */
export async function layoutGraph(p: GraphProjection): Promise<GraphProjection> {
  if (p.nodes.length === 0) {
    return { nodes: [...p.nodes], edges: [...p.edges] }
  }

  const elk = new ELK()

  const graph = {
    id: 'root',
    layoutOptions: LAYOUT_OPTIONS,
    children: p.nodes.map((n) => ({
      id: n.id,
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
    })),
    edges: p.edges.map((e) => ({
      id: e.id,
      sources: [e.source],
      targets: [e.target],
    })),
  }

  let positions: Record<string, { x: number; y: number }> = {}
  try {
    const laid = (await elk.layout(graph)) as ElkLaidOutGraph
    for (const child of laid.children ?? []) {
      positions[child.id] = { x: child.x ?? 0, y: child.y ?? 0 }
    }
  } catch {
    // ELK failed (e.g. a degenerate graph): fall back to a lane grid so the
    // canvas still renders rather than throwing. Stack each lane's nodes
    // vertically; x comes purely from the lane bias below.
    positions = {}
  }

  // Per-lane vertical cursor for the fallback grid (only used when ELK gave
  // us no position for a node).
  const laneCursor: Record<number, number> = {}

  const nodes: FlowNode[] = p.nodes.map((n) => {
    const lane = laneOf(n)
    const elkPos = positions[n.id]
    let x: number
    let y: number
    if (elkPos) {
      // Bias x by the lane so families separate left→right even if ELK packed
      // them into the same layer.
      x = elkPos.x + lane * LANE_STRIDE
      y = elkPos.y
    } else {
      const row = laneCursor[lane] ?? 0
      laneCursor[lane] = row + 1
      x = lane * LANE_STRIDE
      y = row * (NODE_HEIGHT + 30)
    }
    return { ...n, position: { x, y } }
  })

  return { nodes, edges: p.edges.map((e) => ({ ...e })) }
}
