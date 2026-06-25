/**
 * Sidebar navigation grouping (TASK D5).
 *
 * Operators asked for the flat singleton panel list to be reorganized into
 * collapsible, named sections instead of one long "Always-on" list. This
 * module owns the grouping policy: given a set of singleton `PanelKind`s, it
 * buckets them into ordered, named groups.
 *
 * DESIGN — auto-slotting:
 *   Groups are derived from the panel-kind taxonomy, not hard-wired to a
 *   frozen membership list. Each panel is assigned by:
 *     1. an explicit per-kind override (`KIND_GROUP`) for the cases where the
 *        task group differs from the bare kind prefix (e.g. a `system.*`
 *        monitor/investigate panel belongs in Monitor or Investigate — not the
 *        default Operate group), then
 *     2. a prefix fallback (`PREFIX_GROUP`) keyed on the kind's leading
 *        segment (`registry.`, `system.`, `source.`, …), so a *new* panel
 *        kind added to the registry auto-slots into a sensible group with no
 *        change here, then
 *     3. a catch-all "More" group so every panel stays reachable even if a
 *        future prefix is introduced that we don't yet map.
 *
 * Only singleton (non-binding) panels flow through here; per-target and
 * per-analyst panels keep their existing instance-scoped grouping in the
 * Sidebar.
 */

import type { PanelKind } from '@/types'
import { PANEL_REGISTRY } from './registry'

/** A stable id for a nav group; also drives collapse persistence. */
export type NavGroupId =
  | 'monitor'
  | 'investigate'
  | 'configure'
  | 'operate'
  | 'more'

export interface NavGroupDef {
  id: NavGroupId
  label: string
}

/**
 * Ordered group catalog. Order here = render order in the sidebar.
 * `more` is the catch-all and always renders last.
 */
export const NAV_GROUP_DEFS: readonly NavGroupDef[] = [
  { id: 'monitor', label: 'Monitor' },
  { id: 'investigate', label: 'Investigate' },
  { id: 'configure', label: 'Configure' },
  { id: 'operate', label: 'Operate' },
  { id: 'more', label: 'More' },
]

const GROUP_ORDER: Record<NavGroupId, number> = NAV_GROUP_DEFS.reduce(
  (acc, def, i) => {
    acc[def.id] = i
    return acc
  },
  {} as Record<NavGroupId, number>,
)

/**
 * Explicit per-kind group assignment.
 *
 * The nav is organized by what the operator is *doing* — Monitor / Investigate /
 * Configure / Operate — not by subsystem. The `system.*` family spans all four,
 * so its monitor/investigate members are pinned here; everything else `system.*`
 * falls to Operate via the prefix fallback, and `registry.*`/`source.*` → Configure.
 */
const KIND_GROUP: Partial<Record<PanelKind, NavGroupId>> = {
  // --- Monitor: what's happening now ---
  'system.findings': 'monitor',
  'system.targets.roster': 'monitor',
  'system.alert_center': 'monitor',
  'system.report_export': 'monitor',
  'system.pulse': 'monitor',

  // --- Investigate: dig into the why ---
  'system.lineage': 'investigate',
  'system.entities': 'investigate',
  'system.entity_graph': 'investigate',
  // #99 — the notable-structure overlay is an analysis product (Intelligence),
  // not plumbing; the system.* prefix fallback would mis-bucket it to Operate.
  'system.notable_structure': 'investigate',
  'system.search': 'investigate',
  'system.consult': 'investigate',
  // #90 Wave A — these are ANALYSIS tools, not plumbing; the prefix fallback
  // (system.* → operate → Operations) mis-bucketed them. Pin to Investigate
  // (Intelligence) alongside Consult.
  'system.deep_consult': 'investigate',
  'system.optimizer': 'investigate',
  'system.optimizer.diff': 'investigate',
  'system.eval_scorecard': 'investigate',

  // --- Configure: tenancy/admin (registries route via the prefix fallback) ---
  'system.tenant_view': 'configure',
  'system.settings': 'configure',

  // --- v4 visual workspace panels ---
  'v4.map': 'monitor',
  'v4.assessment': 'monitor',
  'v4.why': 'investigate',
  'v4.case': 'investigate',
  'v4.flow': 'operate',

  // Everything else system.* (runtime, actor_health, dead_letter, stream_lag,
  // governor, audit, budget, optimizer[.diff], eval[_scorecard], backfill,
  // streams, users) → Operate via PREFIX_GROUP.
}

/**
 * Prefix fallback keyed on the leading kind segment. A new panel kind added
 * to the registry without a `KIND_GROUP` override auto-slots here.
 */
const PREFIX_GROUP: Record<string, NavGroupId> = {
  registry: 'configure',
  source: 'configure',
  system: 'operate',
  dashboard: 'monitor',
  // `target.*` / `analyst.*` are binding-scoped and don't reach this module,
  // but map them anyway so the function is total for every PanelKind.
  target: 'investigate',
  analyst: 'operate',
}

/** Resolve the leading segment of a panel kind (`system.optimizer.diff` → `system`). */
export function kindPrefix(kind: PanelKind): string {
  const dot = kind.indexOf('.')
  return dot === -1 ? kind : kind.slice(0, dot)
}

/**
 * Assign a single panel kind to its nav group.
 *
 * Resolution order: explicit override → prefix fallback → `more` catch-all.
 * Total over every `PanelKind`.
 */
export function groupForKind(kind: PanelKind): NavGroupId {
  return KIND_GROUP[kind] ?? PREFIX_GROUP[kindPrefix(kind)] ?? 'more'
}

/**
 * #89 — the two TOP-LEVEL product buckets the sidebar splits panels into:
 * `intelligence` (the analytical product the operator reads) vs `operations`
 * (the plumbing that runs it). The product is DERIVED from the nav group
 * (Monitor/Investigate → intelligence; Configure/Operate/More → operations) so
 * no per-panel list is maintained; a panel may override via its registry
 * `product_group` field.
 */
export type ProductGroup = 'intelligence' | 'operations'

export interface ProductGroupDef {
  id: ProductGroup
  label: string
}

/** Order = render order (Intelligence on top — the product leads). */
export const PRODUCT_GROUP_DEFS: readonly ProductGroupDef[] = [
  { id: 'intelligence', label: 'Intelligence' },
  { id: 'operations', label: 'Operations' },
]

const NAVGROUP_PRODUCT: Record<NavGroupId, ProductGroup> = {
  monitor: 'intelligence',
  investigate: 'intelligence',
  configure: 'operations',
  operate: 'operations',
  more: 'operations',
}

/**
 * Resolve a panel kind to its product bucket: an explicit registry
 * `product_group` override wins, else derive from the panel's nav group.
 * Total over every `PanelKind`.
 */
export function productForKind(kind: PanelKind): ProductGroup {
  return (
    PANEL_REGISTRY[kind]?.definition.product_group ??
    NAVGROUP_PRODUCT[groupForKind(kind)]
  )
}

export interface NavGroup {
  id: NavGroupId
  label: string
  kinds: PanelKind[]
}

/**
 * Bucket a list of singleton panel kinds into ordered, non-empty nav groups.
 *
 * Within a group, kinds are sorted by their registry `defaultTitle` so the
 * list is stable and alphabetical regardless of registry declaration order.
 * Empty groups are omitted so the sidebar never renders a header with no rows.
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
