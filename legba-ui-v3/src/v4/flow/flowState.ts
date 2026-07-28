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

/**
 * Density gate (P0-2f): above this per-kind edge count a wiring kind ALSO
 * defaults to hidden. At live scale the subscription fan-out alone is ~1,180
 * edges — a full-tile moiré with every node off-viewport — so the default
 * view keeps only the kinds that stay legible. The toggles keep showing the
 * true count for every hidden kind, and one click brings a kind back.
 */
export const DENSE_EDGE_COUNT_THRESHOLD = 400

/**
 * Compute the default-hidden edge kinds for a given per-kind edge census:
 * the static baseline (`analyst_target`) plus any kind past the density gate.
 * Returned in EDGE_KINDS order so array comparisons are stable.
 */
export function densityHiddenEdgeKinds(
  counts: Partial<Record<FlowEdgeKind, number>>,
): FlowEdgeKind[] {
  return EDGE_KINDS.filter(
    (kind) =>
      DEFAULT_HIDDEN_EDGE_KINDS.includes(kind) ||
      (counts[kind] ?? 0) > DENSE_EDGE_COUNT_THRESHOLD,
  )
}

function sameKinds(a: readonly FlowEdgeKind[], b: readonly FlowEdgeKind[]): boolean {
  return a.length === b.length && a.every((k, i) => k === b[i])
}

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
  /** True once the operator has touched a toggle — density defaults then
   *  stop overriding their choice (until reset). */
  edgeKindsTouched: boolean
  /** The density-computed default-hidden set for the current graph (what
   *  `resetEdgeKinds` restores). */
  densityDefaults: FlowEdgeKind[]
  /**
   * Apply the density gate for a fresh per-kind edge census (canvas calls
   * this whenever the projection's edge counts change). Recomputes the
   * default-hidden set; only overwrites `hiddenEdgeKinds` while the operator
   * hasn't touched the toggles.
   */
  applyEdgeDensityDefaults: (counts: Partial<Record<FlowEdgeKind, number>>) => void

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
      edgeKindsTouched: true,
      hiddenEdgeKinds: s.hiddenEdgeKinds.includes(kind)
        ? s.hiddenEdgeKinds.filter((k) => k !== kind)
        : [...s.hiddenEdgeKinds, kind],
    })),
  resetEdgeKinds: () =>
    set((s) => ({ edgeKindsTouched: false, hiddenEdgeKinds: [...s.densityDefaults] })),
  edgeKindsTouched: false,
  densityDefaults: [...DEFAULT_HIDDEN_EDGE_KINDS],
  applyEdgeDensityDefaults: (counts) =>
    set((s) => {
      const defaults = densityHiddenEdgeKinds(counts)
      const next: Partial<FlowState> = {}
      if (!sameKinds(defaults, s.densityDefaults)) next.densityDefaults = defaults
      if (!s.edgeKindsTouched && !sameKinds(defaults, s.hiddenEdgeKinds)) {
        next.hiddenEdgeKinds = [...defaults]
      }
      return next
    }),

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
