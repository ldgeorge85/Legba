/**
 * Sidebar navigation grouping — the ONE grouped tree (S7-T2).
 *
 * The workstation sidebar is a single grouped tree modeled on the original
 * mission-control UI (UI_UX_DIRECTION_2026-07-02 §"what the original got right"
 * #1): five verb-grouped sections the analyst scans top to bottom —
 *
 *   AWARENESS      — what's happening now (Wall, Live Feed, World Map,
 *                    Timeline, Alerts & Watches, the detail rail)
 *   INVESTIGATION  — dig into the why (Entities, Provenance, search)
 *   ANALYSIS       — reason over it (consult, optimizer, eval)
 *   PRODUCTS       — the finished intelligence (World Assessment, Journal)
 *   ENGINE ROOM    — the plumbing that runs it (registries, health, ledgers,
 *                    id stays `operations` — see NAV_GROUP_DEFS); U-3 also
 *                    nests the raw Targets/Analysts instance groups inside it
 *                    (Sidebar.tsx), collapsed by default
 *
 * This REPLACES the prior two-level Intelligence/Operations split + Monitor/
 * Investigate/Configure/Operate sub-buckets: one flat, five-headed tree.
 *
 * DESIGN — auto-slotting: groups are derived from the panel-kind taxonomy, not
 * a frozen membership list. Each singleton panel is assigned by
 *   1. an explicit per-kind override (`KIND_GROUP`), then
 *   2. a prefix fallback (`PREFIX_GROUP`) keyed on the leading kind segment, so
 *      a NEW panel kind auto-slots with no change here, then
 *   3. an `operations` (Engine Room) catch-all so every panel is always
 *      reachable.
 *
 * Within a group, kinds sort by task order where U-3 §3 pins one (`TASK_ORDER`),
 * alphabetical-by-title otherwise (`compareInGroup`) — NOT purely alphabetical.
 *
 * Only singleton (non-binding) panels flow through here; per-target and
 * per-analyst panels keep their instance-scoped grouping in the Sidebar.
 */

import type { PanelKind } from '@/types'
import { PANEL_REGISTRY } from './registry'

/** A stable id for a nav group; also drives collapse persistence. */
export type NavGroupId =
  | 'awareness'
  | 'investigation'
  | 'analysis'
  | 'products'
  | 'operations'

export interface NavGroupDef {
  id: NavGroupId
  label: string
}

/**
 * Ordered group catalog. Order here = render order in the sidebar. The
 * `operations` group is labeled "Engine Room" (U-3 §2) — all 14 Operations
 * rows PLUS the raw Targets/Analysts instance groups nest inside it (see
 * Sidebar.tsx); the id stays `operations` so `legba_nav_collapsed` /
 * `DEFAULT_COLLAPSED` need no migration.
 */
export const NAV_GROUP_DEFS: readonly NavGroupDef[] = [
  { id: 'awareness', label: 'Awareness' },
  { id: 'investigation', label: 'Investigation' },
  { id: 'analysis', label: 'Analysis' },
  { id: 'products', label: 'Products' },
  { id: 'operations', label: 'Engine Room' },
]

const GROUP_ORDER: Record<NavGroupId, number> = NAV_GROUP_DEFS.reduce(
  (acc, def, i) => {
    acc[def.id] = i
    return acc
  },
  {} as Record<NavGroupId, number>,
)

/**
 * Explicit per-kind group assignment. The sidebar is organized by what the
 * analyst is *doing*, so the `system.*` family (which spans all five) is pinned
 * here; registry.* / source.* fall to Operations via the prefix fallback.
 *
 * Hidden kinds (panel-registry/registry.ts HIDDEN_KINDS — the U-3 merge-set
 * originals folded away behind a tabbed/moded survivor) are deliberately
 * ABSENT here: they never reach `buildNavGroups` (SINGLETON_PANELS excludes
 * hidden kinds), so their group assignment is moot; they fall through to the
 * prefix fallback below, which stays total over every `PanelKind`.
 */
const KIND_GROUP: Partial<Record<PanelKind, NavGroupId>> = {
  // --- Awareness: the live surfaces + the detail rail ---
  'system.wall': 'awareness',
  'system.findings': 'awareness',
  'system.inspector': 'awareness',
  'v4.map': 'awareness',
  'v4.kpi': 'awareness',
  // The merged Timeline (U-3 — Events/Validity mode switch) reads as a live
  // awareness surface, not an investigate one; the merged Alerts & Watches
  // (U-3 — Watches/Triggers/Deliveries tabs) likewise.
  'system.timeline': 'awareness',
  'system.alerts_watches': 'awareness',

  // --- Investigation: dig into the why ---
  'system.entities': 'investigation',
  'system.search': 'investigation',
  // The merged Provenance surface (U-3 — Why/Lineage/Flow tabs).
  'system.provenance': 'investigation',

  // --- Analysis: reason over the substrate ---
  'system.consult': 'analysis',
  'system.optimizer': 'analysis',
  'system.optimizer.diff': 'analysis',
  'system.eval_scorecard': 'analysis',

  // --- Products: the finished intelligence ---
  'v4.assessment': 'products',
  'system.journal': 'products',
  'system.report_export': 'products',

  // --- Engine Room (id stays `operations`): the weekly operator chores ---
  // The gold-set labeling worksheet is an OPERATE surface (a weekly operator
  // duty, not an analysis read) — pinned explicitly rather than left to the
  // prefix fallback so the intent survives a fallback change.
  'system.goldset': 'operations',

  // Everything else system.* (settings, status, actor_health, dead_letter,
  // governor, audit, budget, stream_lag) → Engine Room via PREFIX_GROUP.
}

/**
 * Task-order overrides (U-3 §3): within a group, a handful of kinds read in
 * WORKFLOW order rather than alphabetical — "Awareness reads Wall → Live Feed
 * → World Map → Timeline → Alerts & Watches → Inspector → the rest." Kinds
 * absent here fall back to alphabetical-by-title, appended after the ordered
 * ones (see `buildNavGroups`).
 */
const TASK_ORDER: Partial<Record<PanelKind, number>> = {
  'system.wall': 0,
  'system.findings': 1,
  'v4.map': 2,
  'system.timeline': 3,
  'system.alerts_watches': 4,
  'system.inspector': 5,
}

/**
 * Prefix fallback keyed on the leading kind segment. A new panel kind added to
 * the registry without a `KIND_GROUP` override auto-slots here.
 */
const PREFIX_GROUP: Record<string, NavGroupId> = {
  registry: 'operations',
  source: 'operations',
  system: 'operations',
  v4: 'investigation',
  // `target.*` / `analyst.*` are binding-scoped and don't reach this module,
  // but map them anyway so the function is total for every PanelKind.
  target: 'investigation',
  analyst: 'operations',
}

/** Resolve the leading segment of a panel kind (`system.optimizer.diff` → `system`). */
export function kindPrefix(kind: PanelKind): string {
  const dot = kind.indexOf('.')
  return dot === -1 ? kind : kind.slice(0, dot)
}

/**
 * Assign a single panel kind to its nav group.
 *
 * Resolution order: explicit override → prefix fallback → `operations`
 * catch-all (the plumbing bucket). Total over every `PanelKind`.
 */
export function groupForKind(kind: PanelKind): NavGroupId {
  return KIND_GROUP[kind] ?? PREFIX_GROUP[kindPrefix(kind)] ?? 'operations'
}

export interface NavGroup {
  id: NavGroupId
  label: string
  kinds: PanelKind[]
}

/**
 * Order two kinds within a group: an explicit `TASK_ORDER` entry always beats
 * one without (task order reads top-to-bottom as the analyst's workflow, not
 * the alphabet); kinds with no override fall back to alphabetical-by-title
 * among themselves, appended after every ordered kind ("… → the rest").
 */
function compareInGroup(a: PanelKind, b: PanelKind): number {
  const oa = TASK_ORDER[a]
  const ob = TASK_ORDER[b]
  if (oa !== undefined && ob !== undefined) return oa - ob
  if (oa !== undefined) return -1
  if (ob !== undefined) return 1
  const titleOf = (k: PanelKind) => PANEL_REGISTRY[k].definition.defaultTitle
  return titleOf(a).localeCompare(titleOf(b))
}

/**
 * Bucket a list of singleton panel kinds into ordered, non-empty nav groups.
 *
 * Within a group, kinds sort by `compareInGroup` — task order where U-3 §3
 * pins one, alphabetical-by-title otherwise — so the list is stable
 * regardless of registry declaration order. Empty groups are omitted so the
 * sidebar never renders a header with no rows.
 */
export function buildNavGroups(kinds: readonly PanelKind[]): NavGroup[] {
  const byGroup = new Map<NavGroupId, PanelKind[]>()
  for (const kind of kinds) {
    const gid = groupForKind(kind)
    const bucket = byGroup.get(gid)
    if (bucket) bucket.push(kind)
    else byGroup.set(gid, [kind])
  }

  const out: NavGroup[] = []
  for (const def of NAV_GROUP_DEFS) {
    const bucket = byGroup.get(def.id)
    if (!bucket || bucket.length === 0) continue
    bucket.sort(compareInGroup)
    out.push({ id: def.id, label: def.label, kinds: bucket })
  }
  // Defensive: surface any group id produced by groupForKind that isn't in
  // NAV_GROUP_DEFS (shouldn't happen — the type is closed — but keeps every
  // panel reachable if the catalog and the resolver ever drift).
  for (const [gid, bucket] of byGroup) {
    if (GROUP_ORDER[gid] === undefined && bucket.length > 0) {
      bucket.sort(compareInGroup)
      out.push({ id: gid, label: gid, kinds: bucket })
    }
  }
  return out
}
