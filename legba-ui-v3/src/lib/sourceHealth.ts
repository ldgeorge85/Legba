/**
 * sourceHealth — the pure layer under `system.source_health`.
 *
 * The panel folds three server surfaces that had NO UI consumer at all
 * (`/v3/system/staleness-debt`, `/v3/source-quality`, `/v3/sources/{id}/quality`)
 * into one rollup. Every rule that makes that rollup honest rather than
 * flattering lives HERE, DOM-free, so it can be argued with in a unit test:
 *
 *   1. ASSERTED ≠ EARNED. `asserted` is what a source — or a rater looking at
 *      it — CLAIMS: an admiralty grade, a compiled dossier, a host score. It is
 *      testimony. `earned` is the measured contested-claim track record. A
 *      source can assert A1 and have lost every contest it ever entered, so the
 *      two are never averaged, blended, or reduced to one "quality score";
 *      `assertedVsEarned` keeps them as two strings plus an explicit `tension`
 *      when the claim outruns the record.
 *
 *   2. A RATE WITHOUT ITS n IS NOT A RATE. `formatRate` cannot be called
 *      without a denominator, and it names the server field it came from, so
 *      "100%" can never render without "n=2" beside it and "smoothed" over it.
 *      `earned.low_sample` is the server's own flag for exactly this.
 *
 *   3. ABSENCE ≠ ZERO, and there are TWO different absences here:
 *      `earned === null` means no track-record row exists at all — nothing was
 *      ever measured. `earned.contested_total === 0` means the row exists and
 *      the source has simply never been contested. `earnedRecordState`
 *      separates them so the panel can too. Likewise a null `win_rate_raw` /
 *      `corroboration_rate` renders as `—`, never as 0%.
 *
 *   4. `freshness_grade` `empty` and `ungraded` are ABSENCES, not faults —
 *      `isFreshnessAbsence` marks them, and the tone comes from the existing
 *      `sourceFreshness.freshnessTone` (`muted` for both) rather than a second,
 *      drifting copy of the classification.
 *
 *   5. `StalenessDebtResponse.match_verified` is hard-false on the wire today.
 *      `matchVerifiedCaveat` turns that into a caveat ON the numbers. It
 *      returns `null` on the true branch precisely so nothing renders a green
 *      checkmark: an unverified count is a lower bound, and a verified one is
 *      merely not-caveated.
 *
 *   6. `/v3/source-quality` 503s when migration 0115's `source_quality` view is
 *      absent. `classifyQualityError` separates that "not provisioned" case
 *      from a real fault, because telling an operator their sources are broken
 *      when the VIEW is missing is its own kind of lie.
 */

import { ApiError } from '@/lib/api'
import type {
  AssertedQuality,
  ComputedQuality,
  SourceEarned,
  SourceQualityRow,
  SourceRating,
  StalenessDebtResponse,
} from '@/lib/api'
import { relativeTime } from '@/lib/findingsViews'
import { compareFreshness } from '@/lib/sourceFreshness'

// ---------------------------------------------------------------------------
// Numbers, and the absences that are not numbers.
// ---------------------------------------------------------------------------

/** What a null rate renders as. Never `0%` — the server said "unknown". */
export const ABSENT = '—'

/** A ratio → a percent, or `—` when the server gave null. A null rate is an
 *  absence of measurement, and rendering it as `0%` would invent a result. */
export function formatPercent(value: number | null | undefined, digits = 0): string {
  if (value == null || !Number.isFinite(value)) return ABSENT
  return `${(value * 100).toFixed(digits)}%`
}

/** One rate, and everything a reader needs to weigh it. */
export interface RateText {
  /** The server field this came from — rendered, so nobody has to guess which
   *  of `win_rate_raw` / `win_rate_smoothed` / `win_rate_lower` they're seeing. */
  basis: string
  /** The percent, or `—`. */
  value: string
  /** `n=<denominator>`. Rendered next to `value`, always. */
  n: string
  /** The bare denominator, for callers that lay it out themselves. */
  denominator: number
  /** True when the server gave null — render an absence, not a zero. */
  absent: boolean
}

/**
 * A rate WITH its denominator. The denominator is a required argument on
 * purpose: there is no call site in this panel that is allowed to show a rate
 * without the n it was computed over.
 *
 * Note the denominators differ by field — `win_rate_*` are over
 * `contested_total`, `corroboration_rate` is over `corroboration_total`. The
 * caller passes the RIGHT one; pairing a corroboration rate with the contest
 * count would be a fresh lie rather than a fix for the old one.
 */
export function formatRate(
  basis: string,
  value: number | null,
  denominator: number,
  digits = 0,
): RateText {
  return {
    basis,
    value: formatPercent(value, digits),
    n: `n=${denominator}`,
    denominator,
    absent: value == null,
  }
}

// ---------------------------------------------------------------------------
// EARNED — the measured track record, and its two distinct absences.
// ---------------------------------------------------------------------------

/**
 * The four states a track record can be in. `no-record` and `never-contested`
 * are BOTH absences but they are not the same absence, and collapsing them to
 * "0" would tell an operator that a source lost nothing when in one case
 * nothing was ever looked at.
 */
export type EarnedRecordState =
  /** No `source_track_record` row exists — nothing has ever been measured. */
  | 'no-record'
  /** A row exists; this source has never entered a contested claim. */
  | 'never-contested'
  /** Contested, but over too few contests for the rate to mean anything. */
  | 'low-sample'
  /** Contested enough that the server stopped flagging the sample. */
  | 'measured'

export function earnedRecordState(earned: SourceEarned | null): EarnedRecordState {
  if (earned == null) return 'no-record'
  if (earned.contested_total <= 0) return 'never-contested'
  if (earned.low_sample) return 'low-sample'
  return 'measured'
}

export const EARNED_STATE_LABEL: Record<EarnedRecordState, string> = {
  'no-record': 'no record',
  'never-contested': 'never contested',
  'low-sample': 'low sample',
  measured: 'measured',
}

export const EARNED_STATE_TITLE: Record<EarnedRecordState, string> = {
  'no-record':
    'No track-record row exists for this source at all — nothing has been measured. This is NOT a score of zero.',
  'never-contested':
    'A track-record row exists, but this source has never been party to a contested claim (n=0). No win rate can be computed from nothing.',
  'low-sample':
    'The server flags this sample as too small to read: the win rate is over a handful of contests and is not evidence of anything yet.',
  measured:
    'Measured over enough contests that the server stopped flagging the sample. Still read the lower bound, not the raw rate.',
}

/** One honest line about the measured record — the `earned` half of the split. */
export function earnedSummary(earned: SourceEarned | null): string {
  const state = earnedRecordState(earned)
  if (earned == null || state === 'no-record') {
    return 'No contested-claim record exists — nothing measured (not a zero).'
  }
  if (state === 'never-contested') {
    return 'Track-record row exists but the source has never been contested (n=0) — no win rate is computable.'
  }
  const smoothed = formatPercent(earned.win_rate_smoothed)
  const lower = formatPercent(earned.win_rate_lower)
  const n = earned.contested_total
  const record = `${earned.wins}W/${earned.losses}L`
  if (state === 'low-sample') {
    return `${record} — smoothed ${smoothed} (lower bound ${lower}) over n=${n} contests, too few to mean anything.`
  }
  return `${record} — smoothed ${smoothed} (lower bound ${lower}) over n=${n} contests.`
}

/** The headline win rate: SMOOTHED, never raw. Null when there is no row. */
export function winRateDisplay(earned: SourceEarned | null): RateText | null {
  if (earned == null) return null
  return formatRate('win_rate_smoothed', earned.win_rate_smoothed, earned.contested_total)
}

/** The pessimistic bound — the number an operator should actually act on. */
export function winRateLowerDisplay(earned: SourceEarned | null): RateText | null {
  if (earned == null) return null
  return formatRate('win_rate_lower', earned.win_rate_lower, earned.contested_total)
}

/** The raw rate, shown as SECONDARY. It is the one that flatters a tiny n. */
export function winRateRawDisplay(earned: SourceEarned | null): RateText | null {
  if (earned == null) return null
  return formatRate('win_rate_raw', earned.win_rate_raw, earned.contested_total)
}

/** Corroboration — over `corroboration_total`, which is NOT `contested_total`. */
export function corroborationDisplay(earned: SourceEarned | null): RateText | null {
  if (earned == null) return null
  return formatRate('corroboration_rate', earned.corroboration_rate, earned.corroboration_total)
}

// ---------------------------------------------------------------------------
// ASSERTED — the claims. Testimony, never evidence.
// ---------------------------------------------------------------------------

/**
 * The asserted admiralty grade, or null when nothing was asserted. Falls back
 * to composing reliability+credibility when the server left `admiralty_grade`
 * null but shipped the two halves; an unknown half renders `?` rather than
 * being silently dropped.
 */
export function assertedGrade(asserted: AssertedQuality): string | null {
  if (asserted.admiralty_grade) return asserted.admiralty_grade
  const r = asserted.admiralty_reliability
  const c = asserted.admiralty_credibility
  if (r || c) return `${r ?? '?'}${c ?? '?'}`
  return null
}

/** Whether ANYTHING was asserted about this source — grade, ratings, dossier
 *  or host score. False means the asserted column is honestly empty. */
export function hasAssertion(asserted: AssertedQuality): boolean {
  return (
    assertedGrade(asserted) != null ||
    asserted.has_dossier ||
    asserted.host_score != null ||
    asserted.host_tier != null ||
    asserted.public_rating_count > 0 ||
    asserted.private_rating_count > 0
  )
}

/** One line about what is CLAIMED. Deliberately verbed as claims, not facts. */
export function assertedSummary(asserted: AssertedQuality): string {
  if (!hasAssertion(asserted)) {
    return 'Nothing asserted — no rating, dossier, or host score on record.'
  }
  const parts: string[] = []
  const grade = assertedGrade(asserted)
  if (grade) {
    const by = asserted.admiralty_rater ? ` by ${asserted.admiralty_rater}` : ''
    const method = asserted.admiralty_method ? ` (${asserted.admiralty_method})` : ''
    parts.push(`claims ${grade}${by}${method}`)
  }
  const ratings = asserted.public_rating_count + asserted.private_rating_count
  if (ratings > 0) {
    parts.push(
      `${ratings} rating${ratings === 1 ? '' : 's'} (${asserted.public_rating_count} public / ${asserted.private_rating_count} private)`,
    )
  }
  if (asserted.has_dossier) {
    parts.push(
      asserted.dossier_compiled_by
        ? `dossier by ${asserted.dossier_compiled_by}`
        : 'dossier compiled',
    )
  }
  if (asserted.host_score != null || asserted.host_tier) {
    const tier = asserted.host_tier ? `tier ${asserted.host_tier}` : 'host scored'
    const score = asserted.host_score != null ? ` ${formatPercent(asserted.host_score)}` : ''
    parts.push(`${tier}${score}`)
  }
  if (asserted.host_state_affiliation === true) parts.push('state-affiliated host')
  return parts.join(' · ')
}

/**
 * THE central split. Two strings that are never merged, plus the `tension`:
 * the one line that says out loud when a claim has outrun (or contradicts) the
 * measured record. `tension === null` means there is nothing to flag — NOT
 * that the source is good.
 */
export interface AssertedEarnedSplit {
  /** What is CLAIMED about this source. */
  asserted: string
  /** What it has actually EARNED. */
  earned: string
  /** The disagreement between the two, when there is one. */
  tension: string | null
}

export function assertedVsEarned(row: SourceQualityRow): AssertedEarnedSplit {
  const grade = assertedGrade(row.asserted)
  const e = row.earned
  const claim = grade ? `asserting ${grade}` : 'asserting no grade'
  let tension: string | null = null

  if (e != null && !e.low_sample && e.contested_total > 0 && e.win_rate_lower < 0.5) {
    tension =
      `Loses at least as often as it wins — lower bound ${formatPercent(e.win_rate_lower)} ` +
      `over n=${e.contested_total} contests, while ${claim}.`
  } else if (grade && e == null) {
    tension = `Asserts ${grade} with NO measured contest record behind it — the claim is untested, not confirmed.`
  } else if (grade && e != null && e.contested_total === 0) {
    tension = `Asserts ${grade} and has never been contested (n=0) — nothing has yet tested the claim.`
  } else if (grade && e != null && e.low_sample) {
    tension = `Asserts ${grade} on a record of only n=${e.contested_total} contest${e.contested_total === 1 ? '' : 's'} — too few to back it.`
  }

  return {
    asserted: assertedSummary(row.asserted),
    earned: earnedSummary(e),
    tension,
  }
}

// ---------------------------------------------------------------------------
// COMPUTED — freshness. Absences are not faults.
// ---------------------------------------------------------------------------

/** `empty` and `ungraded` are honest absences of a grade, NOT bad grades. The
 *  panel must not paint them like a fault. */
export function isFreshnessAbsence(grade: string): boolean {
  return grade === 'empty' || grade === 'ungraded'
}

/** When this source last produced a signal, or the honest absence. */
export function lastSignalText(computed: ComputedQuality, now: number = Date.now()): string {
  if (!computed.last_signal_at) return 'no signal on record'
  return relativeTime(computed.last_signal_at, now)
}

/** Volume line for the row — `signals_24h` / `signals_7d` are plain counts. */
export function signalVolumeText(computed: ComputedQuality): string {
  return `${computed.signals_24h} in 24h · ${computed.signals_7d} in 7d`
}

// ---------------------------------------------------------------------------
// Attention flags + sorting.
//
// These are a SORT AID and a set of honest labels. They are explicitly not a
// composite score: nothing here adds an asserted grade to an earned rate.
// ---------------------------------------------------------------------------

export type AttentionFlag =
  | 'losing_contests'
  | 'asserted_unbacked'
  | 'low_sample'
  | 'overdue'
  | 'never_contested'
  | 'no_track_record'

export const ATTENTION_FLAG_LABEL: Record<AttentionFlag, string> = {
  losing_contests: 'losing contests',
  asserted_unbacked: 'claim unbacked',
  low_sample: 'low sample',
  overdue: 'overdue',
  never_contested: 'never contested',
  no_track_record: 'no track record',
}

export const ATTENTION_FLAG_TITLE: Record<AttentionFlag, string> = {
  losing_contests:
    'Measured over enough contests, and its lower-bound win rate is below a coin flip.',
  asserted_unbacked:
    'Something is asserted about this source (a grade) that the measured record does not back — either no record, no contests, or too few.',
  low_sample: 'earned.low_sample is set — the win rate is over too few contests to read.',
  overdue: 'freshness_grade is stale or warn — the source is past its cadence-derived budget.',
  never_contested:
    'A track-record row exists and the contest count is 0. An absence of contests, not a loss.',
  no_track_record: 'No track-record row exists at all. An absence of measurement, not a zero.',
}

/** Worst-first. Faults rank above absences: an absence is not a failing. */
const FLAG_RANK: Record<AttentionFlag, number> = {
  losing_contests: 0,
  asserted_unbacked: 1,
  overdue: 2,
  low_sample: 3,
  never_contested: 4,
  no_track_record: 5,
}

/** Every flag that applies, worst-first. Empty = nothing to flag. */
export function attentionFlags(row: SourceQualityRow): AttentionFlag[] {
  const flags: AttentionFlag[] = []
  const e = row.earned
  const state = earnedRecordState(e)
  const grade = assertedGrade(row.asserted)

  if (e != null && state === 'measured' && e.win_rate_lower < 0.5) flags.push('losing_contests')
  if (grade != null && state !== 'measured') flags.push('asserted_unbacked')
  if (state === 'low-sample') flags.push('low_sample')
  if (row.computed.freshness_grade === 'stale' || row.computed.freshness_grade === 'warn') {
    flags.push('overdue')
  }
  if (state === 'never-contested') flags.push('never_contested')
  if (state === 'no-record') flags.push('no_track_record')

  return flags.sort((a, b) => FLAG_RANK[a] - FLAG_RANK[b])
}

/** Rank of a row's worst flag; unflagged rows rank last. */
export function attentionRank(row: SourceQualityRow): number {
  const flags = attentionFlags(row)
  return flags.length === 0 ? 99 : FLAG_RANK[flags[0]]
}

export type SourceSort = 'attention' | 'source' | 'contested' | 'freshness'

export const SOURCE_SORTS: ReadonlyArray<{ id: SourceSort; label: string }> = [
  { id: 'attention', label: 'attention' },
  { id: 'contested', label: 'most contested' },
  { id: 'freshness', label: 'freshness' },
  { id: 'source', label: 'source id' },
]

/** Non-mutating sort. `contested` puts rows with NO record last rather than
 *  pretending their contest count is 0. */
export function sortSourceQuality(
  rows: SourceQualityRow[],
  mode: SourceSort,
): SourceQualityRow[] {
  const byId = (a: SourceQualityRow, b: SourceQualityRow) =>
    a.source_id.localeCompare(b.source_id)
  const out = [...rows]
  switch (mode) {
    case 'source':
      return out.sort(byId)
    case 'contested':
      return out.sort((a, b) => {
        const an = a.earned?.contested_total
        const bn = b.earned?.contested_total
        if (an == null && bn == null) return byId(a, b)
        if (an == null) return 1
        if (bn == null) return -1
        return bn - an || byId(a, b)
      })
    case 'freshness':
      return out.sort(
        (a, b) =>
          compareFreshness(a.computed.freshness_grade, b.computed.freshness_grade) || byId(a, b),
      )
    case 'attention':
    default:
      return out.sort(
        (a, b) =>
          attentionRank(a) - attentionRank(b) ||
          compareFreshness(a.computed.freshness_grade, b.computed.freshness_grade) ||
          byId(a, b),
      )
  }
}

// ---------------------------------------------------------------------------
// Staleness debt.
// ---------------------------------------------------------------------------

/** The strip's one-line headline. Plain counts — nothing derived. */
export function stalenessHeadline(debt: StalenessDebtResponse): string {
  return (
    `${debt.staleness_debt} debt · ${debt.open_flags} open flag${debt.open_flags === 1 ? '' : 's'} ` +
    `across ${debt.flagged_consumers} consumer${debt.flagged_consumers === 1 ? '' : 's'} · ` +
    `${debt.moved_foundations} foundation${debt.moved_foundations === 1 ? '' : 's'} moved · ` +
    `${debt.closed_flags} closed`
  )
}

/**
 * The caveat that must ride ON the staleness numbers.
 *
 * Returns null when `match_verified` is true — and that is the point. A
 * verified match earns SILENCE, not a green checkmark: the only thing worth
 * saying here is when the numbers are unconfirmed.
 */
export function matchVerifiedCaveat(debt: StalenessDebtResponse): string | null {
  if (debt.match_verified) return null
  const run = debt.last_matcher_run_at
    ? `Last matcher run ${debt.last_matcher_run_at}.`
    : 'The matcher has no recorded run.'
  return (
    'UNVERIFIED — the server reports match_verified=false: nothing has confirmed that these ' +
    `flags match the foundations that actually moved. Read every count below as a lower bound. ${run}`
  )
}

/** How wide the open-flag window is, in the reader's terms. */
export function openWindowText(debt: StalenessDebtResponse, now: number = Date.now()): string {
  if (debt.open_flags === 0) return 'no open flags'
  if (!debt.oldest_open_at && !debt.newest_open_at) return 'open since unknown'
  const oldest = debt.oldest_open_at ? relativeTime(debt.oldest_open_at, now) : 'unknown'
  const newest = debt.newest_open_at ? relativeTime(debt.newest_open_at, now) : 'unknown'
  return `oldest ${oldest} · newest ${newest}`
}

export interface ReasonRow {
  reason: string
  open_flags: number
  /** Share of `open_flags`, or null when the total is 0 (never a fabricated 0%). */
  share: number | null
}

export interface ReasonBreakdown {
  rows: ReasonRow[]
  /** Open flags NOT accounted for by the (server-capped, 50-row) list. */
  uncounted: number
  /** True when the list does not cover every open flag — it is capped at 50. */
  truncated: boolean
}

/**
 * `by_reason` arrives already ordered desc and capped at 50, so this neither
 * re-sorts nor re-ranks it. It adds the share against the AUTHORITATIVE
 * `open_flags` total and, when the capped list does not sum to that total, says
 * how many flags the breakdown does not explain.
 */
export function reasonBreakdown(debt: StalenessDebtResponse): ReasonBreakdown {
  const total = debt.open_flags
  const rows = debt.by_reason.map((r) => ({
    reason: r.reason,
    open_flags: r.open_flags,
    share: total > 0 ? r.open_flags / total : null,
  }))
  const covered = rows.reduce((sum, r) => sum + r.open_flags, 0)
  const uncounted = Math.max(0, total - covered)
  return { rows, uncounted, truncated: uncounted > 0 }
}

// ---------------------------------------------------------------------------
// Load faults — "the view isn't provisioned" is not "the sources are broken".
// ---------------------------------------------------------------------------

/** The server's own `detail` when it sent one, else the error's message. */
export function apiErrorDetail(err: unknown): string {
  if (err instanceof ApiError) {
    const body = err.body
    if (body && typeof body === 'object' && 'detail' in body) {
      const d = (body as { detail: unknown }).detail
      if (typeof d === 'string' && d.length > 0) return d
    }
    if (typeof body === 'string' && body.length > 0) return body
    return err.message
  }
  if (err instanceof Error) return err.message
  return String(err)
}

export interface QualityFault {
  /** `not_provisioned` = migration 0115's `source_quality` view is absent. */
  kind: 'not_provisioned' | 'error'
  detail: string
  /** The operator-facing line for this fault. */
  text: string
}

/**
 * Split a `/v3/source-quality` failure into "this deployment never built the
 * view" (503) and everything else. Both are honest; only one of them is a
 * problem with the sources.
 */
export function classifyQualityError(err: unknown): QualityFault | null {
  if (err == null) return null
  const detail = apiErrorDetail(err)
  if (err instanceof ApiError && err.status === 503) {
    return {
      kind: 'not_provisioned',
      detail,
      text:
        'The `source_quality` view is not provisioned in this deployment (migration 0115 has ' +
        'not been applied), so there is nothing to read. This is a missing view, NOT a finding ' +
        'about source quality — no source has been judged either way.',
    }
  }
  return {
    kind: 'error',
    detail,
    text: `Could not read the source-quality rollup — ${detail}`,
  }
}

// ---------------------------------------------------------------------------
// Per-source drill-down.
// ---------------------------------------------------------------------------

/** Newest rating first; a rating with an unparseable stamp sorts last rather
 *  than jumping the queue on a NaN comparison. */
export function sortRatings(ratings: SourceRating[]): SourceRating[] {
  return [...ratings].sort((a, b) => {
    const ta = Date.parse(a.rated_at)
    const tb = Date.parse(b.rated_at)
    const va = Number.isFinite(ta) ? ta : -Infinity
    const vb = Number.isFinite(tb) ? tb : -Infinity
    return vb - va
  })
}

/** One rating, described as the CLAIM it is, with who made it and how. */
export function describeRating(rating: SourceRating): string {
  const grade =
    rating.grade ??
    (rating.admiralty_reliability || rating.admiralty_credibility
      ? `${rating.admiralty_reliability ?? '?'}${rating.admiralty_credibility ?? '?'}`
      : null)
  const parts = [
    grade ? `claims ${grade}` : 'no grade asserted',
    `by ${rating.rater}`,
    rating.visibility_class,
    `method ${rating.method}`,
  ]
  const refs = rating.references?.length ?? 0
  parts.push(refs === 0 ? 'no references cited' : `${refs} reference${refs === 1 ? '' : 's'}`)
  return parts.join(' · ')
}
