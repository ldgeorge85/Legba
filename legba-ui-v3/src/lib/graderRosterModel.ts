/**
 * graderRosterModel — the correctness GRADER's roster (`GET /api/v1/v3/eval/grader_roster`).
 *
 * The fleet-wide companion to `unitCorrectnessModel`, which serves ONE unit's
 * number to the Inspector badge. Same measurement, same vocabulary, same
 * server-composed badge string; this one carries every graded desk at once,
 * plus the roster totals.
 *
 * THREE RULES THIS FILE KEEPS, and the reason it is thin:
 *
 *   1. **Correctness never appears without coverage.** Every helper that
 *      formats one is paired with the helper that formats the other, and the
 *      section renders them side by side. A correctness share alone is the
 *      failure this whole surface exists to prevent.
 *   2. **NULL is "unmeasured", never 0%.** `correctnessCell` returns the word
 *      when the share is null — the reference decided nothing that desk said.
 *      A coverage of `0.0`, by contrast, is a MEASURED zero (the reference bore
 *      on nothing) and prints as `0.0%`, because those are different facts.
 *      Nothing here coalesces a null to a number.
 *   3. **The percentage formatting matches the server's.** One decimal, the
 *      same as `unit_correctness_api._pct`, so the split-out columns and the
 *      verbatim `badge` string (rendered as each row's tooltip) can never show
 *      the same share two ways.
 *
 * All of it is pure — no fetch, no React — so the arithmetic and the ordering
 * are tested without a DOM.
 */

/** `current` / `stale` / `none` — the GRADER's reference-currency vocabulary. */
export type ReferenceState = 'current' | 'stale' | 'none'

/** The graded unit's grain. A roster is over ONE of these, never both:
 *  `country_composition` / `country_assessment` are graded against the same
 *  references as the desks they compose over, so pooling would count the same
 *  claims twice. The route echoes which grain the figures describe. */
export type Grain = 'desk' | 'composition'

/** One desk's newest graded night inside the window. */
export interface DeskLatest {
  as_of: string
  /** NULL — never 0 — when the reference decided nothing this desk said. */
  correctness_share: number | null
  /** A 0 here is a measured zero; NULL only when no claim was published. */
  coverage_share: number | null
  n_claims: number
  n_contains: number
  n_decided: number
  n_silent: number
  single_family: boolean
  reference_state: ReferenceState
  reference_age_days: number | null
  /** The server-composed line, rendered verbatim (as the row's tooltip). */
  badge: string
}

/** The same desk across every graded night in the window. */
export interface DeskWindow {
  nights_graded: number
  correctness_mean: number | null
  coverage_mean: number | null
}

export interface DeskRosterRow {
  target_id: string
  analyst_id: string
  latest: DeskLatest
  window: DeskWindow
}

/** Every claim under the roster's latest rows, counted once. */
export interface ClaimsPooled {
  n_claims: number
  n_decided: number
  n_contains: number
  correctness_pooled: number | null
  coverage_pooled: number | null
}

export interface RosterTotals {
  desks_graded: number
  desks_unmeasured: number
  correctness_mean_of_desks: number | null
  coverage_mean_of_desks: number | null
  claims_pooled: ClaimsPooled
}

export interface GraderRoster {
  available: boolean
  nights: number
  /** The grain these figures are over — echoed by the route, shown in the
   *  header, never inferred. */
  grain: Grain
  as_of: string | null
  desks: DeskRosterRow[]
  roster: RosterTotals
  honesty_note: string
}

/** How a null share prints — the same word the server's badge uses. */
export const UNMEASURED = 'unmeasured'

/** What a missing non-numeric value renders as. Never `0`, never blank. */
export const ABSENT = '—'

/**
 * The U-5 explain text for the section. Says what each share is OVER, because
 * the pair IS the measurement, and says what this axis is not — the operator
 * gold-set section directly below it is a different instrument entirely.
 */
export const GRADER_ROSTER_EXPLAIN =
  'Correctness is the share of the claims the independent reference BEARS ON that it ' +
  'bore out. Coverage is how much of what the desk said the reference bears on at all — ' +
  '9% coverage means nine claims in ten were never looked at. Read them together: ' +
  'neither is meaningful alone, and a high correctness at a thin coverage is a handful ' +
  'of claims confirmed, not a desk that was right. A desk whose reference decided ' +
  'nothing reads "unmeasured" and is left out of both means — it is not a zero. This is ' +
  'the machine grader\'s axis, against a reference built without seeing this platform; ' +
  'it is never pooled with faithfulness, with calibration, or with the operator ' +
  'gold-set correctness axis below.'

/** Why the two roster figures exist and how they differ. */
export const ROSTER_MEAN_EXPLAIN =
  'Mean of desks gives every desk one vote, so a desk with four claims counts as much ' +
  'as one with four hundred. Pooled counts every claim once. They answer different ' +
  'questions and routinely disagree, so both ship, each labelled.'

/** A share as a one-decimal percentage — the server's own formatting. */
export function pct(share: number | null | undefined): string {
  return share == null ? UNMEASURED : `${(share * 100).toFixed(1)}%`
}

/**
 * The correctness cell, in the badge's idiom: `48.0% (10/21)`, or the bare
 * word when the share is null. The denominator travels with the number always.
 */
export function correctnessCell(latest: DeskLatest): string {
  if (latest.correctness_share == null) {
    return `${UNMEASURED} (${latest.n_decided} decided)`
  }
  return `${pct(latest.correctness_share)} (${latest.n_contains}/${latest.n_decided})`
}

/** The coverage cell: `8.6% (n=45)`. `n` is what the share is OVER. */
export function coverageCell(latest: DeskLatest): string {
  return `${pct(latest.coverage_share)} (n=${latest.n_claims})`
}

/** `current` / `stale (9.0 d)` / `no reference`. */
export function referenceCell(latest: DeskLatest): string {
  if (latest.reference_state === 'none') return 'no reference'
  if (latest.reference_state === 'stale') {
    const age = latest.reference_age_days
    return age == null ? 'stale' : `stale (${age.toFixed(1)} d)`
  }
  return 'current'
}

/**
 * Thinnest coverage FIRST — the point of the section is the desks nobody
 * looked at, not the ones that scored well. A null coverage (no claim
 * published at all) is not "thin", so it sorts last rather than leading;
 * ties break on target then desk so the order is stable across refetches.
 */
export function sortByCoverageAscending(
  desks: readonly DeskRosterRow[] | null | undefined,
): DeskRosterRow[] {
  return [...(desks ?? [])].sort((a, b) => {
    const ca = a.latest.coverage_share
    const cb = b.latest.coverage_share
    if (ca !== cb) {
      if (ca == null) return 1
      if (cb == null) return -1
      return ca - cb
    }
    return (
      a.target_id.localeCompare(b.target_id) ||
      a.analyst_id.localeCompare(b.analyst_id)
    )
  })
}

/** One headline figure pair, already labelled with what it is a mean OVER. */
export interface RosterHeadline {
  label: string
  correctness: string
  coverage: string
  /** What the pair is computed over — rendered so neither number floats free. */
  basis: string
}

/**
 * The two roster figures, each with its own denominator spelled out. Returned
 * as a pair so a caller cannot render one and drop the other — which is the
 * exact mistake ("0.48 correctness") this section was built to stop.
 */
export function rosterHeadlines(roster: RosterTotals): RosterHeadline[] {
  const desks = roster.desks_graded + roster.desks_unmeasured
  const pooled = roster.claims_pooled
  return [
    {
      label: 'mean of desks',
      correctness: pct(roster.correctness_mean_of_desks),
      coverage: pct(roster.coverage_mean_of_desks),
      basis: `${roster.desks_graded} graded of ${desks} desk${desks === 1 ? '' : 's'}`,
    },
    {
      label: 'pooled claims',
      correctness:
        pooled.correctness_pooled == null
          ? `${UNMEASURED} (${pooled.n_decided} decided)`
          : `${pct(pooled.correctness_pooled)} (${pooled.n_contains}/${pooled.n_decided})`,
      coverage: `${pct(pooled.coverage_pooled)} (n=${pooled.n_claims})`,
      basis: `${pooled.n_decided} decided of ${pooled.n_claims} claim${
        pooled.n_claims === 1 ? '' : 's'
      }`,
    },
  ]
}
