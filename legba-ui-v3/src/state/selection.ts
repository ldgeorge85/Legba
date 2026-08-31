/**
 * Global selection store (v4) — the cross-room linking grammar AND the single
 * source of truth for "what is selected" across the whole app.
 *
 * Click a desk on the World map → the map, feed, timeline, Why graph and the
 * World Assessment report all follow it → the Inspector renders its full detail.
 * One shared, single-active selection, FROZEN surface (UI_V4_PLAN §2.4 / redesign
 * Move 2): every panel is a dumb subscriber; only this file owns the shape.
 *
 * The selection is intentionally single-active (one desk/finding/entity at a
 * time) — that is the workstation's brushing ANCHOR, and it also serializes to
 * the URL hash for shareable deep-links (see lib/shareState.ts). Under the v2
 * mission-control vision every panel projects this one selection (synchronized
 * brushing everywhere), so the old "cap it at ~3 surfaces / 3 rooms not 82
 * panels" rationale no longer binds — any number of panels can subscribe.
 *
 * This file replaces THREE former selection systems (redesign Move 2):
 *   1. this store (the v4 cross-room store)               — kept, extended here
 *   2. Flow's local `flowState.selectedNodeId`            — retired; Flow reads
 *      `useSelection` for the highlight and renders detail in the Inspector
 *   3. the `legba:open-*` window-event bus (~20 dispatch  — retired; every
 *      sites firing into the void)                          dispatcher now calls
 *      `selectRow()` and every listener subscribes here
 */
import { create } from 'zustand'

import { emitRead } from '@/lib/readTelemetry'

export type SelectionKind =
  | 'target'
  | 'entity'
  | 'source'
  | 'analyst'
  | 'finding'
  | 'situation'
  | 'signal'

/**
 * Optimistic detail the caller ALREADY HAS at click time (e.g. a feed row's
 * prose body + title). Lets the Inspector paint the report in <300ms while the
 * full lineage / citations / provenance hydrate behind it. Never authoritative
 * — it is replaced the moment the real `InspectorDetail` resolves.
 */
export interface SelectionPreview {
  title?: string
  body?: string
  severity?: string | null
  analystId?: string | null
  targetId?: string | null
}

export interface Selection {
  kind: SelectionKind
  id: string
  /** Optional human label for chrome (breadcrumbs, chips, Inspector header). */
  label?: string
  /** Optimistic, already-in-hand detail for a fast first paint (see above). */
  preview?: SelectionPreview
  /**
   * Disambiguates same-descriptor instances (redesign P-A15) — e.g. two Flow
   * nodes projecting the same descriptorId. Additive; never required and never
   * a breaking schema change. When set, consumers that key by descriptor (the
   * Flow highlight) prefer it over `id`.
   */
  instanceKey?: string
  /** The room/panel that set the selection — for breadcrumb provenance. */
  origin?: string
}

interface SelectionState {
  selection: Selection | null
  /**
   * Drill breadcrumb — oldest → newest, the selections the operator walked
   * through to reach the current one. `select()` pushes the previous selection
   * (deduped) so the Inspector can render a back-trail.
   */
  history: Selection[]
  select: (sel: Selection | null) => void
  /** Pop back to the previous selection in the breadcrumb (Inspector back). */
  back: () => void
  clear: () => void
}

/** Cap the breadcrumb so a long drill session doesn't grow unbounded. */
const MAX_HISTORY = 12

function sameRef(a: Selection | null, b: Selection | null): boolean {
  if (!a || !b) return a === b
  return a.kind === b.kind && a.id === b.id
}

export const useSelection = create<SelectionState>((set) => ({
  selection: null,
  history: [],
  select: (selection) => {
    // READ TELEMETRY (D2e) — this is the ONE chokepoint every "open a record"
    // path in the app funnels through: `selectRow` from the panels, a bare
    // `useSelection.getState().select` from `RecordLink`, the citation-drill
    // fallback in `CitedProse`, the map and Flow click handlers. Instrumenting
    // it here is why the count can be trusted; instrumenting the call sites
    // would have missed whichever one was added next.
    //
    // Only `finding` emits: the wager's question is "did the operator drill a
    // FINDING", and the 0189 vocabulary has no generic record-open kind, so a
    // target/source/entity click is deliberately not counted as one.
    //
    // Emitted OUTSIDE the `set` updater on purpose — zustand may invoke an
    // updater more than once, and a telemetry write is not idempotent.
    if (selection && selection.kind === 'finding') {
      emitRead('finding_open', {
        subjectKind: 'finding',
        subjectId: selection.id,
      })
    }
    set((s) => {
      // No-op re-select of the same record keeps history stable.
      if (sameRef(selection, s.selection)) return { selection }
      const prev = s.selection
      if (!prev) return { selection }
      // Push the prior selection onto the trail (dedupe consecutive repeats).
      const trimmed = s.history.filter((h) => !sameRef(h, prev))
      const history = [...trimmed, prev].slice(-MAX_HISTORY)
      return { selection, history }
    })
  },
  back: () =>
    set((s) => {
      const history = [...s.history]
      const prev = history.pop()
      if (!prev) return s
      return { selection: prev, history }
    }),
  clear: () => set({ selection: null, history: [] }),
}))

/**
 * Subscribe to selection changes OUTSIDE React (e.g. a MapLibre click handler or
 * a deck.gl layer). Returns an unsubscribe fn.
 */
export function onSelectionChange(
  fn: (sel: Selection | null) => void,
): () => void {
  return useSelection.subscribe((s) => fn(s.selection))
}

// ---------------------------------------------------------------------------
// Row-kind bridge — the single, honest map from the substrate `row_kind`
// strings that the lineage walk / legacy panels speak to the cross-room
// `SelectionKind` vocabulary. This replaces the `legba:open-lineage` event
// detail `{row_kind, row_id, title}` shape: a former
//   window.dispatchEvent(new CustomEvent('legba:open-lineage',
//     {detail:{row_kind, row_id, title}}))
// becomes a 1:1
//   selectRow(row_kind, row_id, title)
// ---------------------------------------------------------------------------

/**
 * The substrate row_kinds a lineage walk / legacy panel can surface that are
 * NOT (yet) first-class cross-room selection kinds. They still select — the
 * Inspector renders them generically (raw selection + lineage walk) — but they
 * carry no bespoke resolver, so they map onto `finding` defensively only when
 * the caller passes one of the three walkable kinds. Everything else is coerced
 * to the nearest sensible `SelectionKind` so a click is never a dead-end.
 */
const ROW_KIND_TO_SELECTION: Record<string, SelectionKind> = {
  finding: 'finding',
  meta_finding: 'finding',
  situation: 'situation',
  signal: 'signal',
  // Walkable-but-not-first-class substrate kinds: render via the generic
  // Inspector path keyed on their nearest cross-room kind so they still drill.
  hypothesis: 'finding',
  prediction: 'finding',
  alert: 'finding',
  critique: 'finding',
  meta: 'finding',
  prompt_module_candidate: 'finding',
  // Descriptor / entity kinds pass through unchanged.
  target: 'target',
  analyst: 'analyst',
  source: 'source',
  entity: 'entity',
}

/**
 * Map a raw substrate `row_kind` string to the cross-room `SelectionKind`.
 * Unknown kinds fall back to `finding` (a walkable Inspector path) rather than
 * throwing — a click should never crash the shell.
 */
export function selectionKindOf(rowKind: string): SelectionKind {
  return ROW_KIND_TO_SELECTION[rowKind] ?? 'finding'
}

/**
 * The drop-in replacement for the legacy `legba:open-lineage` dispatchers.
 * Maps the substrate `row_kind`/`row_id`/`title` triple onto the unified store
 * so a single `select()` brushes every room AND opens the Inspector.
 *
 * `opts.origin` tags the breadcrumb provenance.
 *
 * `opts.instanceKey` — pass this ONLY when the caller can surface two distinct
 * nodes/rows that resolve to the SAME `rowId` (descriptorId), and a consumer
 * that keys by descriptor (today: the Flow highlight, see
 * `v4/flow/FlowCanvas.tsx` `selectedNodeId`) would otherwise highlight the
 * WRONG one — it deterministically matches the first descriptor it finds
 * (redesign P-A15). The instanceKey is the caller's stable per-instance id
 * (e.g. a Flow node id, or a `pack` node that appears once per lane); the Flow
 * highlight prefers it over `id`. Most panels emit one row per descriptorId
 * and should NOT pass it. When omitted here, selectRow auto-fills instanceKey
 * with the original substrate kind whenever `rowKind` was coerced to a
 * different `SelectionKind` (e.g. `hypothesis`→`finding`), so the Inspector
 * resolver can still target the correct lineage table — passing an explicit
 * `instanceKey` overrides that.
 */
export function selectRow(
  rowKind: string,
  rowId: string,
  label?: string,
  opts?: { origin?: string; instanceKey?: string; preview?: SelectionPreview },
): void {
  const kind = selectionKindOf(rowKind)
  // When the substrate kind was coerced (e.g. hypothesis→finding), stash the
  // true kind so the Inspector resolver can target the right lineage table.
  const instanceKey =
    opts?.instanceKey ?? (kind !== (rowKind as SelectionKind) ? rowKind : undefined)
  useSelection.getState().select({
    kind,
    id: rowId,
    label,
    instanceKey,
    origin: opts?.origin,
    preview: opts?.preview,
  })
}
