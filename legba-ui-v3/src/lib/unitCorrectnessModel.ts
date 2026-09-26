/**
 * unitCorrectnessModel — the per-unit CORRECTNESS number (Program 2, track G2).
 *
 * Reads `GET /api/v1/units/{target_id}/correctness`: the latest
 * `unit_correctness` row per bounded unit for one country, and, on demand, the
 * per-claim ledger under it. This is a DIFFERENT measurement from
 * `unitEvalModel`'s faithfulness board and is never pooled with it —
 * faithfulness asks "did the read stay true to its citations", correctness asks
 * "was it right, against a reference built blind to our substrate".
 *
 * THE CONTRACT THIS FILE KEEPS (and the reason it is so thin):
 *
 *   * The `badge` string is composed SERVER-side and rendered verbatim, exactly
 *     as `unitEvalModel` does. Nothing here turns 0.244 into "about a quarter",
 *     and nothing here decides what a null share looks like.
 *   * A unit with NO row is absent from `data[]` and the badge renders NOTHING.
 *     "We never measured this" and "we measured this and it was perfect" must
 *     not look alike; a fabricated 100% is the specific failure this whole
 *     track exists to make impossible.
 *   * `reference.state` (`current` / `stale` / `none`) is the absence and
 *     currency vocabulary — how the surface says "no reference" for a country
 *     nothing has graded yet. `stale` is the GRADER's meaning of the word, not
 *     ours: a reference stays current until `window_end` plus the operator's
 *     `LEGBA_GRADER_REFERENCE_GRACE_DAYS` (default 7), and the server resolves
 *     that so the badge and the grader cannot disagree.
 *
 * The fetch is memoised per (target, claims) at module scope so the eight desk
 * badges on a country page share ONE request. The grader runs daily; this is
 * not a hot board.
 */
import { apiGet } from '@/lib/api'

export type ReferenceState = 'current' | 'stale' | 'none'

/** The independent reference a number was computed against. */
export interface ReferenceRef {
  state: ReferenceState
  id: string | null
  sha256: string | null
  builder: string | null
  window_start: string | null
  window_end: string | null
  span_verified_rate: number | null
  thin_dimensions: string[]
  /** Days past `window_end`. On a row this is the grader's STORED
   *  `unit_correctness.reference_age_days` wherever it exists. */
  age_days: number | null
  /** `stored` = the grader wrote the age on the row at grading time;
   *  `derived` = a pre-migration-0197 row aged from the window. Absent at page
   *  level, where there is no row to have stored anything. */
  age_source?: 'stored' | 'derived' | null
}

/** One claim in the ledger under a unit's number — the re-argument surface. */
export interface UnitCorrectnessClaim {
  id: string
  claim_id: string
  grain: string
  claim_text: string
  label_by_family: Record<string, unknown>
  adjudicated: string
  n_families: number
  single_family: boolean
  spans: Record<string, unknown>
  created_at: string
}

/** One bounded unit's correctness at one as-of stamp. */
export interface UnitCorrectness {
  id: string
  analyst_id: string
  target_id: string
  head_id: string
  as_of: string
  rubric_sha: string
  grain: string
  n_claims: number
  n_contains: number
  n_contradicts: number
  n_silent: number
  n_split: number
  n_unparseable: number
  n_single_family: number
  n_decided: number
  correctness_share: number | null
  coverage_share: number | null
  single_family: boolean
  /** The honest, server-composed badge string — rendered verbatim. */
  badge: string
  families: Record<string, unknown>
  cost_usd: number
  created_at: string
  reference: ReferenceRef
  claims?: UnitCorrectnessClaim[] | null
}

export interface UnitCorrectnessPage {
  target_id: string
  reference: ReferenceRef
  data: UnitCorrectness[]
  next_cursor: string | null
}

/** The page-level absence line, for a country carrying no numbers yet. */
export const REFERENCE_STATE_LABEL: Record<ReferenceState, string> = {
  current: 'reference current',
  stale: 'reference stale',
  none: 'no reference',
}

/**
 * What the badge teaches a first-time reader. Deliberately says what each
 * share is OVER, because the pair is the measurement: a correctness of 100% at
 * a coverage of 5% is one claim confirmed and nineteen never looked at.
 */
export const CORRECTNESS_EXPLAIN =
  'Correctness is measured against ONE independent reference, built from the ' +
  'open web without seeing this platform. "Correctness" is the share of the ' +
  'claims the reference BEARS ON that it bore out; "coverage" is how much of ' +
  'what this unit said the reference bears on at all. Read them together — ' +
  'neither is meaningful alone. A claim no two grader families agreed on is ' +
  'published "split" and counted in neither. "Single-family" means one grader ' +
  'stood behind the number: a real reading, and a weaker one. A unit with no ' +
  'number shows no badge — never a fabricated score.'

const CACHE = new Map<string, Promise<UnitCorrectnessPage>>()

function cacheKey(targetId: string, withClaims: boolean): string {
  return `${targetId}::${withClaims ? 'claims' : 'bare'}`
}

/**
 * Fetch one target's correctness page, memoised. `withClaims` pulls the
 * per-claim ledger in the same round trip (the drawer's source) and is cached
 * separately, so opening a drawer never re-fetches the badges.
 */
export function fetchUnitCorrectness(
  targetId: string,
  withClaims = false,
): Promise<UnitCorrectnessPage> {
  const key = cacheKey(targetId, withClaims)
  const hit = CACHE.get(key)
  if (hit) return hit
  const q = withClaims ? '?claims=1' : ''
  const p = apiGet<UnitCorrectnessPage>(
    `/units/${encodeURIComponent(targetId)}/correctness${q}`,
  ).catch((err) => {
    // A failed fetch must not poison the cache — the next badge retries.
    CACHE.delete(key)
    throw err
  })
  CACHE.set(key, p)
  return p
}

/** Drop the memo (tests, and an explicit refresh). */
export function resetUnitCorrectnessCache(): void {
  CACHE.clear()
}

/**
 * This unit's row on the page, or `null`.
 *
 * `null` is the honest answer for a unit nobody graded, and the badge renders
 * nothing for it. Matching is exact on `analyst_id`: a fuzzy match would let
 * `escalation` borrow `escalation_watch`'s number.
 */
export function findUnitCorrectness(
  page: UnitCorrectnessPage | null | undefined,
  analystId: string | null | undefined,
): UnitCorrectness | null {
  if (!page || !analystId || !Array.isArray(page.data)) return null
  return page.data.find((r) => r?.analyst_id === analystId) ?? null
}

/**
 * The claims worth showing FIRST in the drawer: the ones that moved the
 * number. `contradicts` leads (a published claim the reference refuted is the
 * reason anyone opens this drawer), then `contains`, then `split`, then the
 * silent remainder. Within a group, source order is preserved.
 */
const LEDGER_RANK: Record<string, number> = {
  contradicts: 0,
  contains: 1,
  split: 2,
  unparseable: 3,
  silent: 4,
}

export function orderLedger(
  claims: readonly UnitCorrectnessClaim[] | null | undefined,
): UnitCorrectnessClaim[] {
  if (!claims) return []
  return [...claims]
    .map((c, i) => ({ c, i }))
    .sort((a, b) => {
      const ra = LEDGER_RANK[a.c.adjudicated] ?? 9
      const rb = LEDGER_RANK[b.c.adjudicated] ?? 9
      return ra === rb ? a.i - b.i : ra - rb
    })
    .map((x) => x.c)
}

/**
 * The decisive spans a family offered for one claim, flattened for display.
 * `spans` is `{family: {decisive_span, core_claim, reason, ...}}`; a family
 * that offered nothing is omitted rather than shown as an empty quote.
 */
export function decisiveSpans(
  claim: UnitCorrectnessClaim,
): { family: string; span: string }[] {
  const out: { family: string; span: string }[] = []
  for (const [family, raw] of Object.entries(claim.spans ?? {})) {
    if (!raw || typeof raw !== 'object') continue
    const span = (raw as Record<string, unknown>).decisive_span
    if (typeof span === 'string' && span.trim() !== '') {
      out.push({ family, span: span.trim() })
    }
  }
  return out
}

/** Each family's label for one claim, unpooled, in stable family order. */
export function familyLabels(
  claim: UnitCorrectnessClaim,
): { family: string; label: string }[] {
  return Object.entries(claim.label_by_family ?? {})
    .filter(([, v]) => typeof v === 'string')
    .map(([family, v]) => ({ family, label: v as string }))
    .sort((a, b) => a.family.localeCompare(b.family))
}
