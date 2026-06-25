/**
 * The Flow — shared state store (orchestrator-owned contract). The canvas
 * (F.B), telemetry (F.C), and wiring (F.D) agents share this; live telemetry is
 * keyed by descriptorId so the canvas can paint nodes/edges from it.
 */
import { create } from 'zustand'
import type { FlowEdgeKind } from './types'

/** The three wiring kinds the operator can show/hide independently. */
export const EDGE_KINDS: FlowEdgeKind[] = ['subscription', 'analyst_target', 'grant']

/**
 * Default-hidden edge kinds. `analyst_target` is the predicate fan-out (every
 * subscribing analyst → every active target) and is by far the biggest
 * contributor to the 1000+-edge hairball, so the canvas OPENS with it hidden —
 * a readable diagnostic by default. The operator re-enables it with one toggle.
 */
const DEFAULT_HIDDEN_EDGE_KINDS: FlowEdgeKind[] = ['analyst_target']

export interface NodeTelemetry {
  /** signals/sec for a source (rolling). */
  ratePerSec?: number
  /** analyst fires in the window. */
  fires?: number
  /** DLQ / governor errors touching this descriptor. */
  errors?: number
  /** analyst budget burn (tokens or usd). */
  budgetSpent?: number
  /** source stalled — last_pulled_at older than cadence. */
  stalled?: boolean
}

interface FlowState {
  /**
   * Subgraph focus — the FLOW NODE id the canvas is scoped to (that node plus
   * its directly-connected neighbours). Double-click ANY node to toggle it.
   * Keyed by node id (not descriptorId) so a duplicate-descriptor instance —
   * e.g. a `pack` granted into both an analyst and a target lane — focuses the
   * exact node the operator clicked.
   */
  focusNodeId: string | null
  setFocusNodeId: (id: string | null) => void

  // NOTE (redesign Move 2): the former `selectedNodeId`/`selectNode` were
  // retired — selection is now owned solely by `state/selection.ts`. The Flow
  // canvas reads `useSelection` for its node highlight and renders detail in
  // the Inspector, not a local NodeDetails drawer. This store keeps ONLY
  // telemetry (its real job) + focus + edge-kind visibility.

  /** Wiring kinds currently HIDDEN from the canvas (edge-set filtering). */
  hiddenEdgeKinds: FlowEdgeKind[]
  /** Toggle one wiring kind's visibility on/off. */
  toggleEdgeKind: (kind: FlowEdgeKind) => void
  /** Reset edge-kind visibility back to the default (less-dense) view. */
  resetEdgeKinds: () => void

  /** Live telemetry keyed by descriptorId (set by F.C). */
  telemetry: Record<string, NodeTelemetry>
  setTelemetry: (id: string, t: NodeTelemetry) => void
  mergeTelemetry: (map: Record<string, NodeTelemetry>) => void
}

export const useFlowState = create<FlowState>((set) => ({
  focusNodeId: null,
  setFocusNodeId: (focusNodeId) => set({ focusNodeId }),

  hiddenEdgeKinds: [...DEFAULT_HIDDEN_EDGE_KINDS],
  toggleEdgeKind: (kind) =>
    set((s) => ({
      hiddenEdgeKinds: s.hiddenEdgeKinds.includes(kind)
        ? s.hiddenEdgeKinds.filter((k) => k !== kind)
        : [...s.hiddenEdgeKinds, kind],
    })),
  resetEdgeKinds: () => set({ hiddenEdgeKinds: [...DEFAULT_HIDDEN_EDGE_KINDS] }),

  telemetry: {},
  setTelemetry: (id, t) =>
    set((s) => ({ telemetry: { ...s.telemetry, [id]: { ...s.telemetry[id], ...t } } })),
  mergeTelemetry: (map) =>
    set((s) => {
      const next = { ...s.telemetry }
      for (const [id, t] of Object.entries(map)) next[id] = { ...next[id], ...t }
      return { telemetry: next }
    }),
}))
