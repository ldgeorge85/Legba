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
 * WHAT IS RETIRED HERE: the twelve U-3/GLASS-2 merge originals, and the twenty
 * UI-D1 Table B ids UI-B1/merges (W8) folded into the merged wrappers
 * (`system.registry`, `system.status`, `system.ledgers`, `system.optimizer`,
 * `system.wall`, `system.provenance`, and `system.navigator` onto the read) —
 * each survivor renders the retired component, unmodified, as a tab
 * (`panels/merged/*` + `PanelEmbedProvider`). Opening `system.watchlist` gives
 * you Alerts & Watches on its Watches tab — the same component tree the old
 * kind rendered, so the retirement costs nothing and the alias is exact rather
 * than approximate. UI-B1/merges also RE-POINTED three rows (Table B:
 * "re-pointed, not new"): `v4.flow` → the Registry's Canvas, and
 * `system.situations` / `system.narratives` → the new Register kind.
 *
 * Two rows land on a survivor WITHOUT a tab, by design: `v4.kpi` is the Wall's
 * header band (on every tab), and `system.navigator` — the products index —
 * is the object rail's Reads section now, so its id opens the read itself.
 * `source.detail` shares the Registry's Sources tab with `registry.sources`:
 * the tab mounts the list and the per-source detail together.
 *
 * UI-B3 (W11) retired the eight per-subject reads onto `subject.page`'s tabs
 * — the first BOUND survivor: a retired bound tile `target.findings:<id>`
 * becomes `subject.page:<id>` on its Evidence tab, registration and all.
 *
 * WHAT IS NOT RETIRED YET (kindPlan.ts `RETIREMENT_SCHEDULE`): `target.map` /
 * `target.timeline` (the map and the clock with a subject param) and the four
 * `analyst.*` kinds wait for their survivors (`unit.page`), `v4.assessment`
 * for the A/B to close, and `system.search` for the palette's `/` family. An alias may only point at a surface that genuinely
 * renders the retired one; pointing it at an approximation would silently lose
 * a capability.
 *
 * FOUR CALL SITES resolve through this and nothing else changes:
 *   1. `panel-registry/loader.ts` — a stale `panel_id` on a runtime
 *      registration row maps to its survivor before dispatch;
 *   2. `App.tsx`'s singleton opener — a stale kind maps to its survivor and
 *      passes `tab` through `params` so it opens on the right tab;
 *   3. `rewriteSerializedLayout` — the `fromJSON` pre-pass that rewrites stale
 *      ids, COLLAPSES DUPLICATES (a saved layout holding both `v4.why` and
 *      `system.lineage` becomes one Provenance tile, not two), and drops the
 *      unresolvable BEFORE Dockview sees it (which has no per-panel fallback
 *      for an unknown component and would throw);
 *   4. `lib/applyAction.ts` — the one executor's `open_panel` resolves a
 *      retired kind to its survivor (and tab) before the shell port opens it,
 *      so ⌘K, the rail, a mission, the hash and a consult case all land there.
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
 * Every row points at a survivor that mounts the retired component (or, for
 * the two tab-less rows above, carries it on every tab / replaced it with the
 * rail).
 */
export const KIND_ALIASES: Readonly<Record<string, PanelAlias>> = {
  // → the subject page (Brief / Evidence / Claims / Signals / Situations /
  //   Sources / Graph / Runtime) — UI-B3. BOUND: `target.findings:<id>`
  //   becomes `subject.page:<id>` on its tab (the pre-pass below rewrites the
  //   bound tile's registration too, and collapses a subject's several retired
  //   tiles onto one page).
  'target.overview': { kind: 'subject.page', tab: 'runtime' },
  'target.signals': { kind: 'subject.page', tab: 'signals' },
  'target.findings': { kind: 'subject.page', tab: 'evidence' },
  'target.claims': { kind: 'subject.page', tab: 'claims' },
  'target.situations': { kind: 'subject.page', tab: 'situations' },
  'target.sources': { kind: 'subject.page', tab: 'sources' },
  'target.graph': { kind: 'subject.page', tab: 'graph' },
  'target.desk_brief_page': { kind: 'subject.page', tab: 'brief' },
  // → Timeline (Events / Validity)
  'v4.timeline': { kind: 'system.timeline', tab: 'events' },
  // → Provenance (Why / Lineage / Fan-out)
  'v4.why': { kind: 'system.provenance', tab: 'why' },
  'system.lineage': { kind: 'system.provenance', tab: 'lineage' },
  'source.fanout': { kind: 'system.provenance', tab: 'fanout' },
  // → Register (Situations / Narratives) — re-pointed off Provenance (UI-B1/merges)
  'system.situations': { kind: 'system.register', tab: 'situations' },
  'system.narratives': { kind: 'system.register', tab: 'narratives' },
  // → Alerts & Watches (Watches / Triggers / Deliveries)
  'system.watchlist': { kind: 'system.alerts_watches', tab: 'watches' },
  'system.alert_center': { kind: 'system.alerts_watches', tab: 'triggers' },
  'system.escalations': { kind: 'system.alerts_watches', tab: 'deliveries' },
  // → Consult (Chat / Deep)
  'system.deep_consult': { kind: 'system.consult', tab: 'deep' },
  // → Entities (List / Graph / Structure)
  'system.entity_graph': { kind: 'system.entities', tab: 'graph' },
  'system.notable_structure': { kind: 'system.entities', tab: 'structure' },
  // → Registry (Subjects / Units / Sources / Stack / Packs / Builder / Policy / Canvas)
  'registry.targets': { kind: 'system.registry', tab: 'subjects' },
  'registry.analysts': { kind: 'system.registry', tab: 'units' },
  'registry.sources': { kind: 'system.registry', tab: 'sources' },
  'source.detail': { kind: 'system.registry', tab: 'sources' },
  'registry.stack': { kind: 'system.registry', tab: 'stack' },
  'registry.action_packs': { kind: 'system.registry', tab: 'packs' },
  'source.subscription_builder': { kind: 'system.registry', tab: 'builder' },
  'source.subscription_policy': { kind: 'system.registry', tab: 'policy' },
  'v4.flow': { kind: 'system.registry', tab: 'canvas' },
  // → System Status (Status / Production / Failures / Actors / Queues)
  'system.production_gauge': { kind: 'system.status', tab: 'production' },
  'system.dead_letter': { kind: 'system.status', tab: 'failures' },
  'system.actor_health': { kind: 'system.status', tab: 'actors' },
  'system.stream_lag': { kind: 'system.status', tab: 'queues' },
  // → Ledgers (Governor / Audit / Budget)
  'system.governor': { kind: 'system.ledgers', tab: 'governor' },
  'system.audit': { kind: 'system.ledgers', tab: 'audit' },
  'system.budget': { kind: 'system.ledgers', tab: 'budget' },
  // → Optimizer (Candidates / Diff)
  'system.optimizer.diff': { kind: 'system.optimizer', tab: 'diff' },
  // → the Wall (the At-a-Glance band over Overview / Movers)
  'v4.kpi': { kind: 'system.wall' },
  'system.wall_movers': { kind: 'system.wall', tab: 'movers' },
  // → the read (the products index is the object rail's Reads section)
  'system.navigator': { kind: 'v4.morning_read' },
}

/**
 * Retired descriptor-facing `panel_id` → the same survivor.
 *
 * The runtime registry (`ui_panel_registrations`) and any descriptor that ever
 * named one of these persists the SNAKE_CASE id, not the dotted kind (every
 * `def()` row spells it as the kind with dots → underscores), so the loader
 * needs its own lookup. DERIVED from the kind table, so a retirement can never
 * be half-declared (`aliases.test.ts` asserts the 1:1).
 */
export const PANEL_ID_ALIASES: Readonly<Record<string, PanelAlias>> = Object.fromEntries(
  Object.entries(KIND_ALIASES).map(([kind, alias]) => [kind.replace(/[.]/g, '_'), alias]),
)

/** Retired descriptor-facing `panel_id` → the retired KIND it named. */
const RETIRED_KIND_BY_PANEL_ID: Readonly<Record<string, string>> = Object.fromEntries(
  Object.keys(KIND_ALIASES).map((kind) => [kind.replace(/[.]/g, '_'), kind]),
)

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
    // U-2 — a SLOT view (an honest empty region, `lib/surfaces.ts::SLOTS`) is
    // not a registry kind and never retires: it passes through untouched.
    if (panel?.contentComponent === 'slot') {
      idMap.set(id, id)
      nextPanels[id] = panel
      continue
    }
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
    nextPanels[targetId] = retargetPanel(panel, targetId, resolved, kind !== resolved.kind)
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
    // A bound tile names its kind by its registration's panel id. A RETIRED
    // panel id answers with the retired KIND (not its survivor), so the walk
    // below re-targets the tile id and lands it on the alias's tab.
    const raw = registration.panel_id.trim()
    const normalized = raw.startsWith('panels.') ? raw.slice('panels.'.length) : raw
    const retired = RETIRED_KIND_BY_PANEL_ID[normalized]
    if (retired) return retired
    const alias = resolveRetiredPanelId(normalized)
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
  /** The saved id was a RETIRED kind (not the live kind itself). */
  retired: boolean,
): SerializedPanel {
  const entry = PANEL_REGISTRY[alias.kind]
  const params = { ...(panel.params ?? {}) }
  if (typeof params.singletonKind === 'string') params.singletonKind = alias.kind
  if (alias.tab) params.tab = alias.tab
  // A BOUND tile carries its own registration (`App.addBound`): a retired one
  // is re-pointed at the survivor so the tile id, the registration's panel id
  // and its layout slot all name the same kind (UI-B3 — the first bound
  // survivor). Its binding — the subject — is kept verbatim.
  const reg = params.registration as Record<string, unknown> | null | undefined
  if (retired && reg && typeof reg === 'object' && entry) {
    const recordId = typeof reg.descriptor_id === 'string' ? reg.descriptor_id : null
    params.registration = {
      ...reg,
      id: targetId,
      panel_id: entry.definition.panelId,
      layout_slot: alias.kind,
      title: recordId ? `${entry.definition.defaultTitle} · ${recordId}` : entry.definition.defaultTitle,
    }
  }
  // A live kind keeps its saved title. A retired one takes the survivor's —
  // with a tab or without one (`v4.kpi` → the Wall must not stay titled
  // "At a Glance").
  const keepTitle = panel.title && !retired && !alias.tab
  return {
    ...panel,
    id: targetId,
    params,
    title: keepTitle
      ? panel.title
      : ((params.registration as { title?: string } | null | undefined)?.title ??
        entry?.definition.defaultTitle ??
        panel.title),
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
