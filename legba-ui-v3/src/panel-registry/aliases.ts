/**
 * Panel-kind ALIASES — the resolve-time answer to "a retired id must keep
 * working" (UI_HOLISTIC_DESIGN_2026-08-24 §4.4).
 *
 * A retired panel id has to keep resolving forever: saved layouts, ⌘K
 * deep-links and `#sel=` share hashes all persist ids. The mechanism the repo
 * used until now — `HIDDEN_KINDS` — paid for that with a FULL REGISTRY ROW per
 * retirement (component import, bundle weight, a row in every registry
 * iteration, a `hidden` flag to remember). Eighteen of the sixty-seven
 * registered kinds existed only to be invisible: 27% of the catalog.
 *
 * This table replaces that. A retired id is DATA — one line naming its
 * survivor and, where the survivor is a tabbed panel, which tab it lands on.
 * No component import, no bundle weight, no catalog membership, no sidebar row
 * to argue about.
 *
 * WHAT IS RETIRED HERE: the twelve U-3/GLASS-2 merge originals whose survivor
 * already renders them, unmodified, as a tab (`panels/merged/*` +
 * `PanelEmbedProvider`). Opening `system.watchlist` gives you Alerts & Watches
 * on its Watches tab — the same component tree the old kind rendered, so the
 * retirement costs nothing and the alias is exact rather than approximate.
 *
 * WHAT IS NOT RETIRED YET, and why (honesty over arithmetic): the design's
 * remaining retirements each need a survivor that does not exist in the bundle
 * yet — `target.desk` (9 target kinds), `analyst.desk` (4 + eval scorecard),
 * `system.registry` (9 registry/source kinds + the flow canvas),
 * `system.engine` (5 ops kinds), `system.ledgers` (3). An alias may only point
 * at a surface that genuinely renders the retired one; pointing it at an
 * approximation would silently lose a capability. Those rows land with their
 * merge trains, and land HERE — the table is the reason those trains are
 * non-breaking.
 *
 * THREE CALL SITES resolve through this and nothing else changes:
 *   1. `panel-registry/loader.ts` — a stale `panel_id` on a runtime
 *      registration row maps to its survivor before dispatch;
 *   2. `App.tsx`'s singleton opener — a stale kind maps to its survivor and
 *      passes `tab` through `params` so it opens on the right tab;
 *   3. `rewriteSerializedLayout` — the `fromJSON` pre-pass that rewrites stale
 *      ids, COLLAPSES DUPLICATES (a saved layout holding both `v4.why` and
 *      `system.lineage` becomes one Provenance tile, not two), and drops the
 *      unresolvable BEFORE Dockview sees it (which has no per-panel fallback
 *      for an unknown component and would throw).
 */

import type { SerializedDockview } from 'dockview-react'
import type { PanelKind } from '@/types'
import { PANEL_REGISTRY } from './registry'

export interface PanelAlias {
  /** The live kind that renders the retired surface. */
  kind: PanelKind
  /** Which tab/mode of the survivor the retired surface IS, when it has tabs. */
  tab?: string
}

/**
 * Retired panel KIND → its survivor. The only place a retired id is named.
 *
 * Every row here was `hidden: true` in `PANEL_REGISTRY` before this train and
 * pointed at a component that its survivor already mounts as a tab.
 */
export const KIND_ALIASES: Readonly<Record<string, PanelAlias>> = {
  // → Timeline (Events / Validity)
  'v4.timeline': { kind: 'system.timeline', tab: 'events' },
  // → Provenance (Why / Lineage / Flow / Trajectory / Narratives)
  'v4.why': { kind: 'system.provenance', tab: 'why' },
  'system.lineage': { kind: 'system.provenance', tab: 'lineage' },
  'v4.flow': { kind: 'system.provenance', tab: 'flow' },
  'system.situations': { kind: 'system.provenance', tab: 'trajectory' },
  'system.narratives': { kind: 'system.provenance', tab: 'narratives' },
  // → Alerts & Watches (Watches / Triggers / Deliveries)
  'system.watchlist': { kind: 'system.alerts_watches', tab: 'watches' },
  'system.alert_center': { kind: 'system.alerts_watches', tab: 'triggers' },
  'system.escalations': { kind: 'system.alerts_watches', tab: 'deliveries' },
  // → Consult (Chat / Deep)
  'system.deep_consult': { kind: 'system.consult', tab: 'deep' },
  // → Entities (List / Graph / Structure)
  'system.entity_graph': { kind: 'system.entities', tab: 'graph' },
  'system.notable_structure': { kind: 'system.entities', tab: 'structure' },
}

/**
 * Retired descriptor-facing `panel_id` → the same survivor.
 *
 * The runtime registry (`ui_panel_registrations`) and any descriptor that ever
 * named one of these persists the SNAKE_CASE id, not the dotted kind, so the
 * loader needs its own lookup. Kept beside the kind table (and asserted
 * 1:1 with it in `aliases.test.ts`) so a retirement can never be half-declared.
 */
export const PANEL_ID_ALIASES: Readonly<Record<string, PanelAlias>> = {
  v4_timeline: KIND_ALIASES['v4.timeline'],
  v4_why: KIND_ALIASES['v4.why'],
  system_lineage: KIND_ALIASES['system.lineage'],
  v4_flow: KIND_ALIASES['v4.flow'],
  system_situations: KIND_ALIASES['system.situations'],
  system_narratives: KIND_ALIASES['system.narratives'],
  system_watchlist: KIND_ALIASES['system.watchlist'],
  system_alert_center: KIND_ALIASES['system.alert_center'],
  system_escalations: KIND_ALIASES['system.escalations'],
  system_deep_consult: KIND_ALIASES['system.deep_consult'],
  system_entity_graph: KIND_ALIASES['system.entity_graph'],
  system_notable_structure: KIND_ALIASES['system.notable_structure'],
}

/**
 * Resolve a panel kind that may be retired.
 *
 * Returns the live kind unchanged when it is still registered, the alias when
 * it is retired, and undefined when the id is neither — the caller decides
 * whether that is a dropped tile or an unbound placeholder.
 */
export function resolveKind(kind: string): PanelAlias | undefined {
  if (PANEL_REGISTRY[kind as PanelKind]) return { kind: kind as PanelKind }
  return KIND_ALIASES[kind]
}

/** Same, for a descriptor-facing `panel_id`. Live ids are NOT resolved here. */
export function resolveRetiredPanelId(panelId: string): PanelAlias | undefined {
  return PANEL_ID_ALIASES[panelId]
}

// ---------------------------------------------------------------------------
// The `fromJSON` pre-pass.
// ---------------------------------------------------------------------------

export interface LayoutRewrite {
  /** The layout, safe to hand to `api.fromJSON`. */
  layout: SerializedDockview
  /** Stale ids that resolved onto a survivor. */
  rewritten: string[]
  /** Stale ids that collapsed onto a survivor tile already in the layout. */
  collapsed: string[]
  /** Ids with no alias and no registry entry — removed before Dockview saw them. */
  dropped: string[]
  /** True when nothing renderable survived (the caller should seed instead). */
  empty: boolean
}

/** Panel-id → the id it becomes; null means "remove this panel entirely". */
type IdMap = Map<string, string | null>

/**
 * Rewrite a serialized Dockview layout so every panel id resolves.
 *
 * Pure: the input is never mutated. Unknown shapes pass through untouched —
 * a layout this function cannot understand is handed to Dockview exactly as it
 * was found rather than being silently emptied.
 */
export function rewriteSerializedLayout(input: SerializedDockview): LayoutRewrite {
  const rewritten: string[] = []
  const collapsed: string[] = []
  const dropped: string[] = []

  const layout = JSON.parse(JSON.stringify(input)) as SerializedDockview & {
    panels?: Record<string, SerializedPanel>
  }
  const panels = layout.panels
  if (!panels || typeof panels !== 'object') {
    return { layout, rewritten, collapsed, dropped, empty: false }
  }

  const idMap: IdMap = new Map()
  const nextPanels: Record<string, SerializedPanel> = {}

  for (const [id, panel] of Object.entries(panels)) {
    const kind = panelKindOf(id, panel)
    const resolved = kind ? resolveKind(kind) : undefined

    if (!resolved) {
      // No alias, no registry row: a kind deleted outright in an older
      // consolidation. Dropping it loudly beats handing Dockview a component
      // it cannot construct.
      idMap.set(id, null)
      dropped.push(id)
      continue
    }

    const targetId = kind === resolved.kind ? id : retargetId(id, kind!, resolved.kind)
    if (nextPanels[targetId]) {
      // Duplicate collapse: this stale tile resolves onto a survivor already
      // present (both `v4.why` and `system.lineage` → one Provenance tile).
      idMap.set(id, targetId)
      collapsed.push(id)
      continue
    }

    if (targetId !== id) rewritten.push(id)
    idMap.set(id, targetId)
    nextPanels[targetId] = retargetPanel(panel, targetId, resolved)
  }

  layout.panels = nextPanels as typeof layout.panels
  // `placed` is GLOBAL across the walk, not per-group: two retired tiles in two
  // DIFFERENT groups can collapse onto the same survivor id, and a panel id
  // referenced by two groups makes Dockview mount it twice. First group wins.
  const rootAlive = rewriteTree((layout as { grid?: { root?: unknown } }).grid?.root, idMap, new Set())
  pruneActiveGroup(layout)

  return {
    layout,
    rewritten,
    collapsed,
    dropped,
    empty: Object.keys(nextPanels).length === 0 || !rootAlive,
  }
}

interface SerializedPanel {
  id?: string
  contentComponent?: string
  tabComponent?: string
  title?: string
  params?: Record<string, unknown>
  [key: string]: unknown
}

/**
 * The panel KIND behind a serialized panel.
 *
 * Singletons persist `params.singletonKind` and use the kind as the panel id;
 * bound panels use `<kind>:<record_id>` (`loader.instanceId`). Both forms are
 * read here so a bound tile survives the same pre-pass.
 */
function panelKindOf(id: string, panel: SerializedPanel): string | undefined {
  const fromParams = panel.params?.singletonKind
  if (typeof fromParams === 'string' && fromParams) return fromParams
  const registration = panel.params?.registration as { panel_id?: string } | undefined
  if (registration?.panel_id) {
    const alias = resolveRetiredPanelId(registration.panel_id)
    if (alias) return alias.kind
  }
  // Fall back to the id itself: `<kind>` or `<kind>:<record>`.
  const colon = id.indexOf(':')
  return colon === -1 ? id : id.slice(0, colon)
}

/** `<kind>[:record]` → `<survivor>[:record]`. */
function retargetId(id: string, fromKind: string, toKind: PanelKind): string {
  const suffix = id.startsWith(`${fromKind}:`) ? id.slice(fromKind.length) : ''
  return `${toKind}${suffix}`
}

/** Re-point a serialized panel at its survivor, landing on the aliased tab. */
function retargetPanel(
  panel: SerializedPanel,
  targetId: string,
  alias: PanelAlias,
): SerializedPanel {
  const entry = PANEL_REGISTRY[alias.kind]
  const params = { ...(panel.params ?? {}) }
  if (typeof params.singletonKind === 'string') params.singletonKind = alias.kind
  if (alias.tab) params.tab = alias.tab
  return {
    ...panel,
    id: targetId,
    params,
    title: panel.title && !alias.tab ? panel.title : entry?.definition.defaultTitle ?? panel.title,
  }
}

/**
 * Walk the serialized grid, rewriting view ids, collapsing duplicates within a
 * group, and pruning anything that became empty. Returns whether the node still
 * carries a renderable panel.
 */
function rewriteTree(node: unknown, idMap: IdMap, placed: Set<string>): boolean {
  if (!node || typeof node !== 'object') return false
  const n = node as { type?: string; data?: unknown }

  if (n.type === 'branch' && Array.isArray(n.data)) {
    const kept = (n.data as unknown[]).filter((child) => rewriteTree(child, idMap, placed))
    n.data = kept
    return kept.length > 0
  }

  const data = n.data as { views?: unknown; activeView?: unknown } | undefined
  if (!data || !Array.isArray(data.views)) return false

  const views: string[] = []
  for (const view of data.views as unknown[]) {
    if (typeof view !== 'string') continue
    const mapped = idMap.has(view) ? idMap.get(view) : view
    if (!mapped) continue
    // Already mounted by an earlier group (a cross-group duplicate collapse) —
    // one panel id may appear in exactly one group, or Dockview mounts it twice.
    if (placed.has(mapped)) continue
    if (!views.includes(mapped)) views.push(mapped)
    placed.add(mapped)
  }
  data.views = views
  if (typeof data.activeView === 'string') {
    const mapped = idMap.has(data.activeView) ? idMap.get(data.activeView) : data.activeView
    if (mapped && views.includes(mapped)) data.activeView = mapped
    else data.activeView = views[0]
  }
  return views.length > 0
}

/** Drop an `activeGroup` pointer whose group no longer exists in the tree. */
function pruneActiveGroup(layout: SerializedDockview & { activeGroup?: string }): void {
  if (!layout.activeGroup) return
  const alive = new Set<string>()
  collectGroupIds((layout as { grid?: { root?: unknown } }).grid?.root, alive)
  if (!alive.has(layout.activeGroup)) delete layout.activeGroup
}

function collectGroupIds(node: unknown, out: Set<string>): void {
  if (!node || typeof node !== 'object') return
  const n = node as { type?: string; data?: unknown }
  if (n.type === 'branch' && Array.isArray(n.data)) {
    for (const child of n.data as unknown[]) collectGroupIds(child, out)
    return
  }
  const data = n.data as { id?: unknown } | undefined
  if (data && typeof data.id === 'string') out.add(data.id)
}
