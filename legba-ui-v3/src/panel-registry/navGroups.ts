/**
 * Sidebar navigation grouping — the ONE grouped tree (S7-T2).
 *
 * The workstation sidebar is a single grouped tree modeled on the original
 * mission-control UI (UI_UX_DIRECTION_2026-07-02 §"what the original got right"
 * #1): five verb-grouped sections the analyst scans top to bottom —
 *
 *   AWARENESS      — what's happening now (feed, map, alerts, the detail rail)
 *   INVESTIGATION  — dig into the why (entities, graph, lineage, search, flow)
 *   ANALYSIS       — reason over it (consult, optimizer, eval)
 *   PRODUCTS       — the finished intelligence (World Assessment, Journal)
 *   OPERATIONS     — the plumbing that runs it (registries, health, ledgers)
 *
 * This REPLACES the prior two-level Intelligence/Operations split + Monitor/
 * Investigate/Configure/Operate sub-buckets: one flat, five-headed tree.
 *
 * DESIGN — auto-slotting: groups are derived from the panel-kind taxonomy, not
 * a frozen membership list. Each singleton panel is assigned by
 *   1. an explicit per-kind override (`KIND_GROUP`), then
 *   2. a prefix fallback (`PREFIX_GROUP`) keyed on the leading kind segment, so
 *      a NEW panel kind auto-slots with no change here, then
 *   3. an `operations` catch-all (the plumbing bucket) so every panel is always
 *      reachable.
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

/** Ordered group catalog. Order here = render order in the sidebar. */
export const NAV_GROUP_DEFS: readonly NavGroupDef[] = [
  { id: 'awareness', label: 'Awareness' },
  { id: 'investigation', label: 'Investigation' },
  { id: 'analysis', label: 'Analysis' },
  { id: 'products', label: 'Products' },
  { id: 'operations', label: 'Operations' },
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
 */
const KIND_GROUP: Partial<Record<PanelKind, NavGroupId>> = {
  // --- Awareness: the live surfaces + the detail rail ---
  'system.findings': 'awareness',
  'system.alert_center': 'awareness',
  'system.escalations': 'awareness',
  'system.inspector': 'awareness',
  'v4.map': 'awareness',
  'v4.kpi': 'awareness',
  'v4.timeline': 'awareness',

  // --- Investigation: dig into the why ---
  'system.entities': 'investigation',
  'system.entity_graph': 'investigation',
  'system.lineage': 'investigation',
  'system.search': 'investigation',
  'system.notable_structure': 'investigation',
  'v4.why': 'investigation',
  'v4.flow': 'investigation',

  // --- Analysis: reason over the substrate ---
  'system.consult': 'analysis',
  'system.deep_consult': 'analysis',
  'system.optimizer': 'analysis',
  'system.optimizer.diff': 'analysis',
  'system.eval_scorecard': 'analysis',

  // --- Products: the finished intelligence ---
  'v4.assessment': 'products',
  'system.journal': 'products',
  'system.report_export': 'products',

  // Everything else system.* (settings, status, actor_health, dead_letter,
  // governor, audit, budget, stream_lag) → Operations via PREFIX_GROUP.
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
 * Bucket a list of singleton panel kinds into ordered, non-empty nav groups.
 *
 * Within a group, kinds are sorted by their registry `defaultTitle` so the list
 * is stable and alphabetical regardless of registry declaration order. Empty
 * groups are omitted so the sidebar never renders a header with no rows.
 */
export function buildNavGroups(kinds: readonly PanelKind[]): NavGroup[] {
  const byGroup = new Map<NavGroupId, PanelKind[]>()
  for (const kind of kinds) {
    const gid = groupForKind(kind)
    const bucket = byGroup.get(gid)
    if (bucket) bucket.push(kind)
    else byGroup.set(gid, [kind])
  }

  const titleOf = (k: PanelKind) => PANEL_REGISTRY[k].definition.defaultTitle

  const out: NavGroup[] = []
  for (const def of NAV_GROUP_DEFS) {
    const bucket = byGroup.get(def.id)
    if (!bucket || bucket.length === 0) continue
    bucket.sort((a, b) => titleOf(a).localeCompare(titleOf(b)))
    out.push({ id: def.id, label: def.label, kinds: bucket })
  }
  // Defensive: surface any group id produced by groupForKind that isn't in
  // NAV_GROUP_DEFS (shouldn't happen — the type is closed — but keeps every
  // panel reachable if the catalog and the resolver ever drift).
  for (const [gid, bucket] of byGroup) {
    if (GROUP_ORDER[gid] === undefined && bucket.length > 0) {
      bucket.sort((a, b) => titleOf(a).localeCompare(titleOf(b)))
      out.push({ id: gid, label: gid, kinds: bucket })
    }
  }
  return out
}
