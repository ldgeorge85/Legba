/**
 * The Flow — shared contract (orchestrator-owned). Wave-1 Track B agents code
 * against these: the projection (F.A) produces a GraphProjection; the canvas
 * (F.B) renders it; telemetry (F.C) + wiring (F.D) layer on top. None import
 * each other's files.
 */
import type { Node, Edge } from '@xyflow/react'

export type FlowNodeKind = 'source' | 'target' | 'analyst' | 'pack'

export type LifecycleState =
  | 'draft'
  | 'configured'
  | 'active'
  | 'paused'
  | 'retired'

export interface FlowNodeData extends Record<string, unknown> {
  kind: FlowNodeKind
  descriptorId: string
  label: string
  state: LifecycleState
  /** Descriptor family string (source/target/analyst/action_pack). */
  family: string
  /** Secondary type — source kind (rss/gdelt), analyst method, etc. */
  subkind?: string
}

export type FlowNode = Node<FlowNodeData>

export type FlowEdgeKind = 'subscription' | 'grant' | 'analyst_target'

export interface FlowEdgeData extends Record<string, unknown> {
  kind: FlowEdgeKind
}

export type FlowEdge = Edge<FlowEdgeData>

export interface GraphProjection {
  nodes: FlowNode[]
  edges: FlowEdge[]
}

/** Column lanes for the ELK layered layout. */
export const KIND_LANE: Record<FlowNodeKind, number> = {
  source: 0,
  target: 1,
  analyst: 2,
  pack: 3,
}
