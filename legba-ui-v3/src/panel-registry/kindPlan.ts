/**
 * kindPlan — the kind plan as DATA (UI-B1/shell; UI-D1 §2.2, §2.5, D2–D4).
 *
 * Three things the shell used to keep as flags or not at all:
 *
 *   1. **Drills.** A kind whose params need a ref — a subject, a unit, a claim
 *      — has nothing to show without one. It is never listed in the `＋`
 *      catalog; it is opened from its PARENT, the object it is about.
 *      "Hidden" stopped being a registry flag and became this derived property
 *      ({@link isDrill}): `HIDDEN_KINDS` is gone.
 *
 *   2. **The retirement schedule** — UI-D1 Table B, all 36 rows: each retiring
 *      id, the survivor (and tab) it lands on, and the lane that lands it. A
 *      row the lane has LANDED is flagged `retired: true` — the kind is gone
 *      from the registry and its alias row is in `aliases.ts` (UI-B1/merges
 *      flipped its twenty in W8); a row still pending names a registered kind
 *      the ratchets and the stance rule read (UI-B3, the A/B close, the
 *      palette).
 *
 *   3. **The survivors** Table A names that do not exist yet (`unit.page`;
 *      UI-B3 built `subject.page` in W11, UI-B1/merges the other three —
 *      `system.register`, `system.ledgers`, `system.registry`).
 *
 * The ratchets in `kindPlan.test.ts` replaced `navGroups.test.ts`'s 26-row
 * budget in the commit that deleted the tree.
 */
import type { PanelKind } from '@/types'
import { PANEL_REGISTRY } from './registry'
import { isMapSurfaceKind, mapSurfaceDeclared } from '@/lib/subjectGeometry'

/** A kind this profile can mount (UI-B4/map: no map surface under `geometry: none`). */
function mountable(kind: PanelKind): boolean {
  return mapSurfaceDeclared() || !isMapSurfaceKind(kind)
}

/** Who a drill is opened from (its parent object or surface). */
export type DrillParent =
  | { object: 'subject' }
  | { object: 'unit' }
  | { object: 'claim'; from: PanelKind[] }
  | { object: 'candidate'; from: PanelKind[] }

/**
 * The singleton drills — kinds with no binding whose params still need a ref.
 * (Every `requiresBinding` kind is a drill too, parented by its axis.) The
 * prompt-module diff left this list in UI-B1/merges: it is the Optimizer's
 * Diff tab now, opened from the candidate row it sits beside.
 */
export const SINGLETON_DRILLS: Readonly<Partial<Record<PanelKind, DrillParent>>> = {
  'analysis.cross_framing': {
    object: 'claim',
    from: ['subject.page', 'analysis.contentions', 'system.inspector'],
  },
}

/** The parent a drill is opened from; undefined for a catalog kind. */
export function drillParent(kind: PanelKind): DrillParent | undefined {
  const declared = SINGLETON_DRILLS[kind]
  if (declared) return declared
  const def = PANEL_REGISTRY[kind]?.definition
  if (!def?.requiresBinding) return undefined
  return def.category === 'analyst' ? { object: 'unit' } : { object: 'subject' }
}

/** A drill needs a ref to render, so it is never listed in the `＋` catalog. */
export function isDrill(kind: PanelKind): boolean {
  return drillParent(kind) !== undefined
}

/** The bound drills of one object axis, in registry order. */
export function drillsOf(object: 'subject' | 'unit'): PanelKind[] {
  return (Object.keys(PANEL_REGISTRY) as PanelKind[]).filter((k) => drillParent(k)?.object === object && mountable(k))
}

/** The `＋` catalog: every registered kind that is not a drill. */
export function catalogKinds(): PanelKind[] {
  return (Object.keys(PANEL_REGISTRY) as PanelKind[]).filter((k) => !isDrill(k) && mountable(k))
}

// ---------------------------------------------------------------------------
// Table B — the retirement schedule.
// ---------------------------------------------------------------------------

/**
 * Table A's new survivors that do not exist yet. `subject.page` left this list
 * when UI-B3 built it (W11); `unit.page` is the other half of UI-B3's row.
 */
export const PLANNED_SURVIVORS = ['unit.page'] as const

/**
 * Bound kinds a STANCE may seed unbound: the tile then FOLLOWS the subject the
 * reader selected instead of naming one (UI-D1 Table A: the Subject stance
 * seeds `subject.page`). Opened with a subject — the rail, ⌘K `@`, a chip, a
 * case — the same kind is bound to that subject and pins it.
 */
export const SUBJECT_FOLLOWING_KINDS: ReadonlySet<PanelKind> = new Set<PanelKind>(['subject.page'])

export interface ScheduledRetirement {
  /** The survivor (a registered kind, or a planned one). */
  survivor: string
  tab?: string
  /** The lane that lands the merge and moves this row into `aliases.ts`. */
  lands: 'UI-B1/merges' | 'UI-B3' | 'A/B close' | 'palette'
  /**
   * LANDED: the kind is deleted from the registry and `aliases.ts` carries its
   * row (same survivor, same tab — `kindPlan.test.ts` holds the two equal).
   */
  retired?: true
}

export const RETIREMENT_SCHEDULE: Readonly<Record<string, ScheduledRetirement>> = {
  // → subject.page (UI-B3, W11 — LANDED)
  'target.overview': { survivor: 'subject.page', tab: 'runtime', lands: 'UI-B3', retired: true },
  'target.signals': { survivor: 'subject.page', tab: 'signals', lands: 'UI-B3', retired: true },
  'target.findings': { survivor: 'subject.page', tab: 'evidence', lands: 'UI-B3', retired: true },
  'target.claims': { survivor: 'subject.page', tab: 'claims', lands: 'UI-B3', retired: true },
  'target.situations': { survivor: 'subject.page', tab: 'situations', lands: 'UI-B3', retired: true },
  'target.sources': { survivor: 'subject.page', tab: 'sources', lands: 'UI-B3', retired: true },
  'target.graph': { survivor: 'subject.page', tab: 'graph', lands: 'UI-B3', retired: true },
  'target.desk_brief_page': { survivor: 'subject.page', tab: 'brief', lands: 'UI-B3', retired: true },
  // → the map and the clock, with a subject param (UI-B3)
  'target.map': { survivor: 'v4.map', lands: 'UI-B3' },
  'target.timeline': { survivor: 'system.timeline', lands: 'UI-B3' },
  // → unit.page (UI-B3)
  'analyst.runs': { survivor: 'unit.page', tab: 'runs', lands: 'UI-B3' },
  'analyst.outputs': { survivor: 'unit.page', tab: 'outputs', lands: 'UI-B3' },
  'analyst.critiques': { survivor: 'unit.page', tab: 'critiques', lands: 'UI-B3' },
  'analyst.cross_target': { survivor: 'unit.page', tab: 'cross', lands: 'UI-B3' },
  // → system.registry (UI-B1/merges, W8 — LANDED)
  'registry.targets': { survivor: 'system.registry', tab: 'subjects', lands: 'UI-B1/merges', retired: true },
  'registry.analysts': { survivor: 'system.registry', tab: 'units', lands: 'UI-B1/merges', retired: true },
  'registry.sources': { survivor: 'system.registry', tab: 'sources', lands: 'UI-B1/merges', retired: true },
  'registry.stack': { survivor: 'system.registry', tab: 'stack', lands: 'UI-B1/merges', retired: true },
  'registry.action_packs': { survivor: 'system.registry', tab: 'packs', lands: 'UI-B1/merges', retired: true },
  'source.detail': { survivor: 'system.registry', tab: 'sources', lands: 'UI-B1/merges', retired: true },
  'source.subscription_builder': {
    survivor: 'system.registry',
    tab: 'builder',
    lands: 'UI-B1/merges',
    retired: true,
  },
  'source.subscription_policy': {
    survivor: 'system.registry',
    tab: 'policy',
    lands: 'UI-B1/merges',
    retired: true,
  },
  // → system.provenance (LANDED)
  'source.fanout': { survivor: 'system.provenance', tab: 'fanout', lands: 'UI-B1/merges', retired: true },
  // → system.status (LANDED)
  'system.actor_health': { survivor: 'system.status', tab: 'actors', lands: 'UI-B1/merges', retired: true },
  'system.stream_lag': { survivor: 'system.status', tab: 'queues', lands: 'UI-B1/merges', retired: true },
  'system.production_gauge': { survivor: 'system.status', tab: 'production', lands: 'UI-B1/merges', retired: true },
  'system.dead_letter': { survivor: 'system.status', tab: 'failures', lands: 'UI-B1/merges', retired: true },
  // → system.ledgers (LANDED)
  'system.governor': { survivor: 'system.ledgers', tab: 'governor', lands: 'UI-B1/merges', retired: true },
  'system.audit': { survivor: 'system.ledgers', tab: 'audit', lands: 'UI-B1/merges', retired: true },
  'system.budget': { survivor: 'system.ledgers', tab: 'budget', lands: 'UI-B1/merges', retired: true },
  // → system.optimizer (LANDED)
  'system.optimizer.diff': { survivor: 'system.optimizer', tab: 'diff', lands: 'UI-B1/merges', retired: true },
  // → system.wall (LANDED; the KPI strip is the header band, no tab)
  'v4.kpi': { survivor: 'system.wall', lands: 'UI-B1/merges', retired: true },
  'system.wall_movers': { survivor: 'system.wall', tab: 'movers', lands: 'UI-B1/merges', retired: true },
  // → v4.morning_read
  'v4.assessment': { survivor: 'v4.morning_read', lands: 'A/B close' },
  'system.navigator': { survivor: 'v4.morning_read', lands: 'UI-B1/merges', retired: true },
  // → the palette's `/` family
  'system.search': { survivor: 'palette:/', lands: 'palette' },
}

/** Table B rows not landed yet — each names a kind that is still registered. */
export function pendingRetirements(): string[] {
  return Object.keys(RETIREMENT_SCHEDULE).filter((k) => !RETIREMENT_SCHEDULE[k].retired)
}

/** Table B rows a lane has landed — each is an alias row, never a registry row. */
export function landedRetirements(): string[] {
  return Object.keys(RETIREMENT_SCHEDULE).filter((k) => RETIREMENT_SCHEDULE[k].retired)
}

/**
 * The ceiling the registry may never rise above again (was `registry.test.ts:442`).
 *
 * 62 → 45 in UI-B1/merges (W8): twenty Table B kinds retired into the merged
 * wrappers' tabs, three Table A survivors registered (`system.registry`,
 * `system.ledgers`, `system.register`). 45 → 38 in UI-B3's subject half (W11):
 * the eight per-subject reads retired into `subject.page`'s tabs, which is the
 * one kind it registered (45 − 8 + 1). The unit half (`unit.page`, −4 + 1),
 * the map and the clock taking a subject param (−2), the A/B close and the
 * palette's `/` family take it to Table A's 31.
 * 38 → 39 in N-J (2026-11-13): `system.flow`, the operator-requested read-only
 * canvas over the descriptors — a NEW survivor (Table A grows to 32), taking
 * one of the two headroom places under the survivor ceiling.
 */
export const REGISTRY_TOTAL_CEILING = 39

/** UI-D1 §2.2: Table A's 31 survivors + 2 headroom. Counts kinds NOT on the schedule. */
export const REGISTRY_SURVIVOR_CEILING = 33

/** Registered kinds not scheduled to retire — what the ≤ 33 ratchet counts. */
export function survivorKinds(): PanelKind[] {
  return (Object.keys(PANEL_REGISTRY) as PanelKind[]).filter((k) => !RETIREMENT_SCHEDULE[k])
}
