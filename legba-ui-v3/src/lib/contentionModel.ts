/**
 * Contested-claims data layer (Holes-B Wave 5 — #101).
 *
 * The backend `fact_contention` / `fact_contention_values` sidecar (migration
 * 0055) records every live dispute: when >= 2 credible sources disagree on a
 * `(subject, predicate)` value, both fact rows legitimately coexist OPEN, but
 * the disagreement is invisible at the fact layer. The arbiter scores each
 * competing value `Q·C·R·F`, surfaces at most one winner (or ABSTAINS on a
 * near-tie), and exposes the group via `GET /api/v1/contention`.
 *
 * This module normalizes that read into a `Contention` view-model + the small
 * derived metrics the support panel renders (the per-value credibility SHARE,
 * the distinct-source counts, the surfaced-winner flag), reading defensively
 * so a partial/older payload never throws. Pure — no DOM, no fetch — so it is
 * unit-testable the same way `claimsModel` is.
 */

/** One competing value cluster — mirrors `ContentionValueRow` (read API). */
export interface ContentionValueRow {
  value_key: string
  representative_fact_id: string | null
  distinct_source_count: number
  source_credibility_sum: number
  confidence_max: number
  confidence_mean: number
  source_types: string[]
  arbiter_score: number | null
  surfaced_winner: boolean
  is_junk: boolean
  junk_reason: string | null
  latest_asserted_at: string | null
}

/** One contention group — mirrors `ContentionRow` (read API). */
export interface ContentionRow {
  id: string
  subject_key: string
  predicate_key: string
  status: 'contested' | 'surfaced' | 'collapsed'
  surfaced_value: string | null
  value_count: number
  junk_count: number
  opened_at: string
  resolved_at: string | null
  updated_at: string
  values: ContentionValueRow[]
}

export interface ContentionPage {
  data: ContentionRow[]
  next_cursor: string | null
}

/** A competing value, decorated with the panel's derived support metrics. */
export interface ContentionValueView {
  valueKey: string
  representativeFactId: string | null
  distinctSourceCount: number
  sourceCredibilitySum: number
  confidenceMax: number
  /** This value's share of the group's total source-credibility, in [0,1].
   *  0 when the whole group sums to 0 (no credibility scored yet). */
  credibilityShare: number
  arbiterScore: number | null
  surfacedWinner: boolean
  isJunk: boolean
  junkReason: string | null
  sourceTypes: string[]
}

export interface ContentionView {
  id: string
  subjectKey: string
  predicateKey: string
  status: ContentionRow['status']
  /** True while the dispute is LIVE (contested / surfaced) — drives the badge. */
  isLive: boolean
  /** The arbiter's current winner value, or null when it ABSTAINED. */
  surfacedValue: string | null
  /** Distinct NON-junk competing values (the "N sources disagree" N). */
  valueCount: number
  /** Junk-gated clusters excluded from the dispute (operator-reportable). */
  junkCount: number
  /** True when the arbiter surfaced no winner — an honest "disputed, unresolved". */
  abstained: boolean
  values: ContentionValueView[]
  openedAt: string
  updatedAt: string
}

function num(v: unknown, fallback = 0): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : fallback
}

/** Decorate one value cluster with its credibility share against the group
 *  total. The total is passed in (computed once per group) so a single value's
 *  share is stable regardless of iteration order. */
export function toValueView(
  row: ContentionValueRow,
  credibilityTotal: number,
): ContentionValueView {
  const sum = num(row.source_credibility_sum)
  return {
    valueKey: row.value_key,
    representativeFactId: row.representative_fact_id ?? null,
    distinctSourceCount: num(row.distinct_source_count),
    sourceCredibilitySum: sum,
    confidenceMax: num(row.confidence_max),
    credibilityShare: credibilityTotal > 0 ? sum / credibilityTotal : 0,
    arbiterScore: typeof row.arbiter_score === 'number' ? row.arbiter_score : null,
    surfacedWinner: Boolean(row.surfaced_winner),
    isJunk: Boolean(row.is_junk),
    junkReason: row.junk_reason ?? null,
    sourceTypes: Array.isArray(row.source_types) ? row.source_types : [],
  }
}

/** Normalize a `ContentionRow` to its view-model. NON-junk values are the
 *  dispute; the credibility share is computed over the non-junk total so a
 *  junk cluster never dilutes the real contenders' shares. Values keep the
 *  API's order (surfaced winner / top arbiter score first). */
export function toContention(row: ContentionRow): ContentionView {
  const allValues = Array.isArray(row.values) ? row.values : []
  const live = row.status === 'contested' || row.status === 'surfaced'
  // Share denominator = the non-junk credibility total (junk is excluded from
  // the dispute, so it must not be in the share base either).
  const credibilityTotal = allValues
    .filter((v) => !v.is_junk)
    .reduce((acc, v) => acc + num(v.source_credibility_sum), 0)
  const values = allValues.map((v) => toValueView(v, credibilityTotal))
  const hasWinner = values.some((v) => v.surfacedWinner)
  return {
    id: row.id,
    subjectKey: row.subject_key,
    predicateKey: row.predicate_key,
    status: row.status,
    isLive: live,
    surfacedValue: row.surfaced_value ?? null,
    valueCount: num(row.value_count),
    junkCount: num(row.junk_count),
    // ABSTAINED iff the dispute is live but the arbiter surfaced no winner.
    abstained: live && !row.surfaced_value && !hasWinner,
    values,
    openedAt: row.opened_at,
    updatedAt: row.updated_at,
  }
}

export function toContentions(rows: ContentionRow[]): ContentionView[] {
  return (rows ?? []).map(toContention)
}

/** Pick the single contention group for a given fact id from a page (the
 *  fact/Why view fetches `?fact_id=<id>` which returns 0 or 1 group). Returns
 *  null when the fact is not contested. */
export function contentionForFact(page: ContentionPage | undefined): ContentionView | null {
  const first = page?.data?.[0]
  return first ? toContention(first) : null
}

/** A compact one-line badge label for a contested fact. */
export function badgeLabel(view: ContentionView): string {
  if (view.abstained) {
    return `Contested — ${view.valueCount} values, no winner`
  }
  return `Contested — ${view.valueCount} values`
}
