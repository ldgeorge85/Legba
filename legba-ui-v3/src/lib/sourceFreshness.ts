/**
 * sourceFreshness — the UI-side classification for the A7 per-source
 * freshness grade (`system.status` — Acquisition table), pure + DOM-free.
 *
 * Mirrors `legba.data.registry.source_freshness.grade_freshness`: the
 * registry grades each source's freshest-signal age against a budget derived
 * from that source's OWN declared cadence (never a hardcoded global window),
 * and reports the CLOSED grade on `SourceFiringRow.freshness_grade`:
 *
 *   * `ok`       — freshest signal age within budget.
 *   * `stale`    — over budget, but ≤ 3× budget.
 *   * `warn`     — badly overdue (> 3× budget).
 *   * `empty`    — active + cadence-declared but never produced a signal (a
 *     first-class honest state, not a worst `warn`).
 *   * `ungraded` — no honest grade exists: no parsable cadence declaration
 *     (NEVER faked to `ok`), or the head is not active.
 *
 * This module only classifies the grade into a display tone + honest title;
 * it never re-derives the grade itself (that stays server-side, against the
 * live budget/age — recomputing it here would drift).
 */

export type FreshnessGrade = 'ok' | 'stale' | 'warn' | 'empty' | 'ungraded'

/** Coarse display tone for the grade — maps 1:1 onto the panel's existing
 *  traffic-light vocabulary (green/amber/red/grey), kept as its own type here
 *  so this module has no dependency on a panel-local type. */
export type FreshnessTone = 'ok' | 'watch' | 'bad' | 'muted'

const FRESHNESS_TONE: Record<FreshnessGrade, FreshnessTone> = {
  ok: 'ok',
  stale: 'watch',
  warn: 'bad',
  empty: 'muted',
  ungraded: 'muted',
}

/** The display tone for a freshness grade. An unrecognized grade (a future
 *  server addition this build doesn't know about) reads `muted` — never a
 *  fabricated `ok`. */
export function freshnessTone(grade: string): FreshnessTone {
  return (FRESHNESS_TONE as Record<string, FreshnessTone>)[grade] ?? 'muted'
}

/** The honest hover/title text for a grade, folding in the cadence-derived
 *  budget when one exists (`budget_minutes` is `null` exactly when
 *  `ungraded` — no budget was derivable, or the source isn't active). */
export function freshnessTitle(grade: string, budgetMinutes: number | null): string {
  const budget = budgetMinutes != null ? ` (budget ${budgetMinutes}m)` : ''
  switch (grade) {
    case 'ok':
      return `within its cadence-derived budget${budget}`
    case 'stale':
      return `over its cadence-derived budget${budget}`
    case 'warn':
      return `badly overdue — beyond 3× its cadence-derived budget${budget}`
    case 'empty':
      return 'active + cadence-declared, but has never produced a signal'
    case 'ungraded':
      return 'ungraded — no parsable cadence declaration, or the head is not active'
    default:
      return grade
  }
}

/** Worst-first ordering for sorting a source list by freshness grade. */
const FRESHNESS_SEVERITY: Record<FreshnessGrade, number> = {
  warn: 0,
  empty: 1,
  stale: 2,
  ungraded: 3,
  ok: 4,
}

/** Comparator for worst-freshness-first sorting (unknown grades sort last,
 *  same as `ungraded`, next to `ok`). */
export function compareFreshness(a: string, b: string): number {
  const sa = (FRESHNESS_SEVERITY as Record<string, number>)[a] ?? FRESHNESS_SEVERITY.ungraded
  const sb = (FRESHNESS_SEVERITY as Record<string, number>)[b] ?? FRESHNESS_SEVERITY.ungraded
  return sa - sb
}
