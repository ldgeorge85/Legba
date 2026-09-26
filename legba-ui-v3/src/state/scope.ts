/**
 * SCOPE — "what the wall is about", as distinct from FOCUS ("what I am reading").
 *
 * ## The one sentence that governs all 57 panels
 *
 * > **A panel FILTERS by scope and HIGHLIGHTS by focus.**
 *
 * ## Why a second store rather than a second field on `useSelection`
 *
 * `state/feedView.ts:7-26` is the confession this file answers. The feed used to
 * derive its server-side target filter from the global selection:
 *
 *     const serverTargetId = selection?.kind === 'target' ? selection.id : ''
 *
 * Picking a desk filtered the feed (good); then clicking any ROW to read it
 * flipped the selection to `kind:'finding'`, the derived target went empty, the
 * query key changed, the page refetched, and the desk filter, the loaded pages
 * and the scroll position all vanished. The fix taken at the time was **stop
 * following** — right fix, wrong problem. The defect was never the coupling; it
 * was **one slot doing two jobs**. "What am I scoped to" and "what am I reading"
 * have different LIFETIMES, and a single `{kind, id}` cannot hold both.
 *
 * So:
 *
 *  * **FOCUS** stays exactly where it was — `state/selection.ts`, one record,
 *    written by every row click, read by the Inspector. All 68 write sites keep
 *    working untouched: zero regression by construction.
 *  * **SCOPE** is this store. It is written only by DELIBERATE acts — the
 *    Navigator, a choropleth/desk click, ⌘K, a future "Scope to this" button —
 *    and it **survives every row click**, which is precisely what `feedView`
 *    could not do.
 *
 * ## Members come free — no backend work
 *
 * A scope carries its WORLD (`ScopeMembers`), and every member id is already on
 * the row the reader already fetched: `AssemblyPayload.blocks[]` carries
 * `finding_id`, `desk`, `target_id`, `target_name`, `signals[]{signal_id,
 * source_id}`, plus `coverage[]` and `as_of`. See `lib/scopeFromReport.ts` — it
 * is pure, unit-testable, and issues no fetch of its own.
 *
 * ## History
 *
 * Back AND forward, as v2 had (the selection store shipped `back()` only).
 * `setScope` pushes the outgoing scope onto `history` and CLEARS `future`;
 * `back()` moves the current scope onto `future`; `forward()` reverses it.
 */
import { create } from 'zustand'

/**
 * What kind of thing the wall is scoped to.
 *
 * `report` is the one the v3 vocabulary had no word for at all: a report is a
 * `kind='finding'` row whose `analyst_id` is one of the composition producers
 * (`world_assessor`, `world_assessment`, `region_composition`,
 * `country_composition`, `country_assessment`, `chronicle_assessor`), so "click
 * a report and everything follows it" was not merely unimplemented — it was
 * inexpressible.
 */
export type ScopeKind =
  | 'report'
  | 'target'
  | 'entity'
  | 'situation'
  | 'journal_entry'
  | 'none'

/**
 * The selected thing's WORLD — the ids every following panel filters by.
 *
 * Every field is a plain array (never null) so a consumer can spread it without
 * a guard; emptiness means "this scope says nothing about that axis", which is
 * NOT the same as "filter to nothing" — see {@link scopeParams}, which only ever
 * pushes a parameter it actually has a value for.
 */
export interface ScopeMembers {
  /** Findings carried by the scope (an assembly's block heads, say). */
  findingIds: string[]
  /** Desks/targets the scope covers — the feed's and map's primary filter. */
  targetIds: string[]
  /** Signals cited inside the scope. */
  signalIds: string[]
  /** Sources behind those signals. */
  sourceIds: string[]
  /** Entity canonical names the scope mentions — the graph's seed set. */
  entityNames: string[]
  /** Producing analysts, for a scope that is about a producer. */
  analystIds: string[]
  /** ISO-8601 instant the scope was composed at (`assembly.as_of`). */
  asOf: string | null
  /** How far back the scope's evidence reaches, in hours. */
  windowHours: number | null
}

export interface Scope {
  kind: ScopeKind
  id: string
  /** Human label for the chip, the status bar and the consult pin. */
  label: string
  members: ScopeMembers
  /** Which surface set it — 'navigator', 'map', 'palette', 'selection-bridge'. */
  origin: string
}

/** An empty member set — the shape every projector starts from. */
export function emptyMembers(): ScopeMembers {
  return {
    findingIds: [],
    targetIds: [],
    signalIds: [],
    sourceIds: [],
    entityNames: [],
    analystIds: [],
    asOf: null,
    windowHours: null,
  }
}

/** Cap the scope trail. Scope changes are rare (deliberate acts), so 50 is ample. */
export const MAX_SCOPE_HISTORY = 50

interface ScopeState {
  scope: Scope | null
  /** Oldest → newest, the scopes walked through to reach the current one. */
  history: Scope[]
  /** Scopes stepped back OUT of, newest-first; `setScope` clears it. */
  future: Scope[]
  setScope: (s: Scope | null) => void
  /** Step back one scope. No-op when the trail is empty. */
  back: () => void
  /** Step forward into a scope `back()` left. No-op when nothing was undone. */
  forward: () => void
  clear: () => void
}

function sameScope(a: Scope | null, b: Scope | null): boolean {
  if (!a || !b) return a === b
  return a.kind === b.kind && a.id === b.id
}

export const useScope = create<ScopeState>((set) => ({
  scope: null,
  history: [],
  future: [],
  setScope: (scope) =>
    set((s) => {
      // A no-op re-scope keeps the trail stable (the Navigator re-affirming the
      // row it already scoped to must not manufacture a history entry).
      if (sameScope(scope, s.scope)) return { scope }
      const prev = s.scope
      // A deliberate new scope is a new branch: whatever `back()` had parked in
      // `future` is no longer reachable, exactly as a browser drops its forward
      // stack on a fresh navigation.
      if (!prev) return { scope, future: [] }
      const trimmed = s.history.filter((h) => !sameScope(h, prev))
      return {
        scope,
        history: [...trimmed, prev].slice(-MAX_SCOPE_HISTORY),
        future: [],
      }
    }),
  back: () =>
    set((s) => {
      const history = [...s.history]
      const prev = history.pop()
      if (!prev) return s
      const future = s.scope ? [s.scope, ...s.future].slice(0, MAX_SCOPE_HISTORY) : s.future
      return { scope: prev, history, future }
    }),
  forward: () =>
    set((s) => {
      const future = [...s.future]
      const next = future.shift()
      if (!next) return s
      const history = s.scope
        ? [...s.history.filter((h) => !sameScope(h, s.scope)), s.scope].slice(-MAX_SCOPE_HISTORY)
        : s.history
      return { scope: next, history, future }
    }),
  clear: () => set({ scope: null, history: [], future: [] }),
}))

/**
 * Subscribe to scope changes OUTSIDE React (a MapLibre handler, the consult
 * store bridge). Returns an unsubscribe fn — mirrors `onSelectionChange`.
 */
export function onScopeChange(fn: (s: Scope | null) => void): () => void {
  return useScope.subscribe((s) => fn(s.scope))
}

/**
 * The server-side filter a scope implies, as query params.
 *
 * Deliberately CONSERVATIVE. `/findings` and `/signals` take a single
 * `target_id`, not a list, so a multi-desk scope pushes a `target_id` only when
 * it names exactly one desk; a world read (14 desks) filters client-side off
 * `members` instead, because pushing the first of fourteen would silently show
 * the operator a thirteenth of their scope and call it the scope.
 *
 * `since` is pushed whenever the scope knows its own window, which is the half
 * of the report-scope filter that always applies.
 */
export function scopeParams(scope: Scope | null): Record<string, string> {
  const out: Record<string, string> = {}
  if (!scope) return out
  const { targetIds, asOf, windowHours } = scope.members
  if (targetIds.length === 1) out.target_id = targetIds[0]
  if (asOf && windowHours != null && windowHours > 0) {
    const end = Date.parse(asOf)
    if (Number.isFinite(end)) {
      out.since = new Date(end - windowHours * 3600_000).toISOString()
    }
  }
  return out
}

/**
 * Whether a record id is inside the scope — the client-side half of the filter,
 * used where the server cannot express the scope (a multi-desk world read).
 *
 * A scope with no members of the relevant axis matches EVERYTHING: an empty
 * member list means "this scope is silent about findings", never "no finding
 * qualifies". Filtering to nothing on an under-populated payload is how a
 * scoped panel goes blank and the operator concludes the link is broken.
 */
export function inScope(
  scope: Scope | null,
  row: { id?: string | null; target_id?: string | null },
): boolean {
  if (!scope) return true
  const { findingIds, targetIds, signalIds } = scope.members
  if (targetIds.length > 0 && row.target_id) return targetIds.includes(row.target_id)
  const ids = [...findingIds, ...signalIds]
  if (ids.length > 0 && row.id) return ids.includes(row.id)
  return true
}

/** Test/teardown helper — a pristine, history-free scope store. */
export function resetScope(): void {
  useScope.setState({ scope: null, history: [], future: [] })
}
