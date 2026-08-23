/**
 * Unit tests for the `system.source_health` pure layer.
 *
 * These hold the six honesty rules the panel exists to enforce, without a DOM:
 * asserted never merges into earned; no rate renders without its n; the two
 * distinct absences (`earned: null` vs `contested_total: 0`) stay distinct; a
 * null rate is `—` and never `0%`; `match_verified: false` is a caveat and
 * never a checkmark; and a 503 is a missing view, not a verdict on the sources.
 */

import { describe, it, expect } from 'vitest'
import { ApiError } from '@/lib/api'
import type {
  AssertedQuality,
  ComputedQuality,
  SourceEarned,
  SourceQualityRow,
  SourceRating,
  StalenessDebtResponse,
} from '@/lib/api'
import {
  ABSENT,
  apiErrorDetail,
  assertedGrade,
  assertedSummary,
  assertedVsEarned,
  attentionFlags,
  attentionRank,
  classifyQualityError,
  corroborationDisplay,
  describeRating,
  earnedRecordState,
  earnedSummary,
  formatPercent,
  formatRate,
  hasAssertion,
  isFreshnessAbsence,
  lastSignalText,
  matchVerifiedCaveat,
  openWindowText,
  reasonBreakdown,
  signalVolumeText,
  sortRatings,
  sortSourceQuality,
  stalenessHeadline,
  winRateDisplay,
  winRateLowerDisplay,
  winRateRawDisplay,
} from '@/lib/sourceHealth'

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function asserted(over: Partial<AssertedQuality> = {}): AssertedQuality {
  return {
    admiralty_reliability: null,
    admiralty_credibility: null,
    admiralty_grade: null,
    admiralty_rater: null,
    admiralty_method: null,
    admiralty_rated_at: null,
    public_rating_count: 0,
    private_rating_count: 0,
    has_dossier: false,
    dossier_compiled_at: null,
    dossier_compiled_by: null,
    host_matched: null,
    host_score: null,
    host_tier: null,
    host_state_affiliation: null,
    host_rationale: null,
    host_scored_by: null,
    host_scored_at: null,
    ...over,
  }
}

function earned(over: Partial<SourceEarned> = {}): SourceEarned {
  return {
    wins: 10,
    losses: 5,
    contested_total: 15,
    win_rate_raw: 10 / 15,
    win_rate_smoothed: 0.65,
    win_rate_lower: 0.58,
    low_sample: false,
    corroborated: 40,
    corroboration_total: 100,
    corroboration_rate: 0.4,
    lag_hours: 3,
    sample_as_of: '2026-08-01T00:00:00Z',
    computed_at: '2026-08-02T00:00:00Z',
    ...over,
  }
}

function computed(over: Partial<ComputedQuality> = {}): ComputedQuality {
  return {
    freshness_grade: 'ok',
    budget_minutes: 60,
    cadence_raw: '1h',
    last_signal_at: '2026-08-20T11:00:00Z',
    age_seconds: 600,
    signals_24h: 12,
    signals_7d: 80,
    ...over,
  }
}

function row(over: Partial<SourceQualityRow> = {}): SourceQualityRow {
  return {
    source_id: 'src-a',
    registered: true,
    declared_state: 'active',
    declared_kind: 'rss',
    endpoint_host: 'example.org',
    asserted: asserted(),
    earned: earned(),
    computed: computed(),
    ...over,
  }
}

function debt(over: Partial<StalenessDebtResponse> = {}): StalenessDebtResponse {
  return {
    staleness_debt: 9,
    open_flags: 10,
    superseded_consumer_flags: 2,
    flagged_consumers: 4,
    moved_foundations: 3,
    closed_flags: 21,
    oldest_open_at: '2026-08-01T00:00:00Z',
    newest_open_at: '2026-08-20T00:00:00Z',
    by_reason: [
      { reason: 'foundation_superseded', open_flags: 6 },
      { reason: 'foundation_retired', open_flags: 3 },
    ],
    last_matcher_run_at: '2026-08-20T06:00:00Z',
    match_verified: false,
    ...over,
  }
}

function rating(over: Partial<SourceRating> = {}): SourceRating {
  return {
    rating_id: 'r1',
    source_id: 'src-a',
    rater: 'ops-desk',
    visibility_class: 'public',
    method: 'manual',
    admiralty_reliability: 'A',
    admiralty_credibility: '1',
    grade: 'A1',
    rubric: {},
    references: [],
    rated_at: '2026-08-01T00:00:00Z',
    ...over,
  }
}

// ---------------------------------------------------------------------------

describe('rate formatting — a rate without its n is not a rate', () => {
  it('renders a null rate as an honest absence, never 0%', () => {
    expect(formatPercent(null)).toBe(ABSENT)
    expect(formatPercent(undefined)).toBe(ABSENT)
    expect(formatPercent(0)).toBe('0%')
    expect(formatPercent(0.6667)).toBe('67%')
    expect(formatPercent(0.6667, 1)).toBe('66.7%')
  })

  it('carries the denominator and names the field it came from', () => {
    const r = formatRate('win_rate_smoothed', 0.5, 200)
    expect(r).toEqual({
      basis: 'win_rate_smoothed',
      value: '50%',
      n: 'n=200',
      denominator: 200,
      absent: false,
    })
  })

  it('marks a null rate as absent so the caller cannot print a zero', () => {
    const r = formatRate('win_rate_raw', null, 0)
    expect(r.absent).toBe(true)
    expect(r.value).toBe(ABSENT)
    expect(r.n).toBe('n=0')
  })

  it('prefers smoothed/lower over raw and labels each one', () => {
    const e = earned({ win_rate_raw: 1, win_rate_smoothed: 0.6, win_rate_lower: 0.2, contested_total: 2 })
    expect(winRateDisplay(e)).toMatchObject({ basis: 'win_rate_smoothed', value: '60%', n: 'n=2' })
    expect(winRateLowerDisplay(e)).toMatchObject({ basis: 'win_rate_lower', value: '20%', n: 'n=2' })
    expect(winRateRawDisplay(e)).toMatchObject({ basis: 'win_rate_raw', value: '100%', n: 'n=2' })
  })

  it('gives corroboration its OWN denominator, not the contest count', () => {
    const e = earned({ contested_total: 15, corroboration_total: 100, corroboration_rate: 0.4 })
    const c = corroborationDisplay(e)
    expect(c).toMatchObject({ basis: 'corroboration_rate', value: '40%', n: 'n=100' })
  })

  it('returns no rate at all when there is no track-record row', () => {
    expect(winRateDisplay(null)).toBeNull()
    expect(winRateLowerDisplay(null)).toBeNull()
    expect(winRateRawDisplay(null)).toBeNull()
    expect(corroborationDisplay(null)).toBeNull()
  })
})

describe('earnedRecordState — two absences that are not the same absence', () => {
  it('calls a missing row "no-record"', () => {
    expect(earnedRecordState(null)).toBe('no-record')
  })

  it('calls an existing row with zero contests "never-contested"', () => {
    expect(earnedRecordState(earned({ contested_total: 0, wins: 0, losses: 0 }))).toBe(
      'never-contested',
    )
  })

  it('never collapses the two into one state', () => {
    expect(earnedRecordState(null)).not.toBe(
      earnedRecordState(earned({ contested_total: 0, wins: 0, losses: 0 })),
    )
  })

  it('honours the server low_sample flag ahead of the raw rate', () => {
    expect(earnedRecordState(earned({ contested_total: 2, low_sample: true }))).toBe('low-sample')
    expect(earnedRecordState(earned({ contested_total: 200, low_sample: false }))).toBe('measured')
  })
})

describe('earnedSummary', () => {
  it('says nothing was measured, not that the score is zero', () => {
    const s = earnedSummary(null)
    expect(s).toMatch(/No contested-claim record exists/)
    expect(s).toMatch(/not a zero/)
    expect(s).not.toMatch(/0%/)
  })

  it('says never contested for an existing row with n=0', () => {
    const s = earnedSummary(earned({ contested_total: 0, wins: 0, losses: 0 }))
    expect(s).toMatch(/never been contested \(n=0\)/)
    expect(s).toMatch(/no win rate is computable/)
  })

  it('a 100% rate over 2 contests reads as too few to mean anything', () => {
    const s = earnedSummary(
      earned({
        wins: 2,
        losses: 0,
        contested_total: 2,
        win_rate_raw: 1,
        win_rate_smoothed: 0.6,
        win_rate_lower: 0.21,
        low_sample: true,
      }),
    )
    expect(s).toContain('n=2')
    expect(s).toMatch(/too few to mean anything/)
    // The flattering raw 100% is NOT the headline.
    expect(s).not.toContain('100%')
  })

  it('a measured record leads with the smoothed rate and its n', () => {
    const s = earnedSummary(earned({ contested_total: 210, win_rate_smoothed: 0.71, win_rate_lower: 0.66 }))
    expect(s).toContain('smoothed 71%')
    expect(s).toContain('lower bound 66%')
    expect(s).toContain('n=210')
  })
})

describe('asserted — a claim, described as a claim', () => {
  it('composes reliability+credibility when the grade field is null', () => {
    expect(assertedGrade(asserted({ admiralty_grade: 'B2' }))).toBe('B2')
    expect(
      assertedGrade(asserted({ admiralty_reliability: 'A', admiralty_credibility: '1' })),
    ).toBe('A1')
    expect(assertedGrade(asserted({ admiralty_reliability: 'A' }))).toBe('A?')
    expect(assertedGrade(asserted())).toBeNull()
  })

  it('knows when nothing at all was asserted', () => {
    expect(hasAssertion(asserted())).toBe(false)
    expect(hasAssertion(asserted({ has_dossier: true }))).toBe(true)
    expect(hasAssertion(asserted({ public_rating_count: 1 }))).toBe(true)
    expect(assertedSummary(asserted())).toMatch(/Nothing asserted/)
  })

  it('verbs the grade as a claim and names who claimed it', () => {
    const s = assertedSummary(
      asserted({
        admiralty_grade: 'A1',
        admiralty_rater: 'ops-desk',
        admiralty_method: 'manual',
        public_rating_count: 2,
        private_rating_count: 1,
        has_dossier: true,
        dossier_compiled_by: 'analyst-9',
        host_tier: 'state_media',
        host_score: 0.3,
        host_state_affiliation: true,
      }),
    )
    expect(s).toContain('claims A1 by ops-desk (manual)')
    expect(s).toContain('3 ratings (2 public / 1 private)')
    expect(s).toContain('dossier by analyst-9')
    expect(s).toContain('tier state_media 30%')
    expect(s).toContain('state-affiliated host')
  })
})

describe('assertedVsEarned — the split that is never merged', () => {
  it('keeps the claim and the record as two separate strings', () => {
    const split = assertedVsEarned(
      row({ asserted: asserted({ admiralty_grade: 'A1' }), earned: earned() }),
    )
    expect(split.asserted).toContain('claims A1')
    expect(split.earned).toContain('smoothed')
    expect(split.asserted).not.toContain('smoothed')
    expect(split.earned).not.toContain('claims A1')
  })

  it('flags an A1 claim with no measured record at all', () => {
    const split = assertedVsEarned(
      row({ asserted: asserted({ admiralty_grade: 'A1' }), earned: null }),
    )
    expect(split.tension).toMatch(/Asserts A1 with NO measured contest record/)
    expect(split.tension).toMatch(/untested, not confirmed/)
  })

  it('flags an A1 claim that has never been contested', () => {
    const split = assertedVsEarned(
      row({
        asserted: asserted({ admiralty_grade: 'A1' }),
        earned: earned({ contested_total: 0, wins: 0, losses: 0 }),
      }),
    )
    expect(split.tension).toMatch(/never been contested \(n=0\)/)
  })

  it('flags an A1 claim resting on a low sample, and says the n', () => {
    const split = assertedVsEarned(
      row({
        asserted: asserted({ admiralty_grade: 'A1' }),
        earned: earned({ contested_total: 2, low_sample: true }),
      }),
    )
    expect(split.tension).toContain('only n=2 contests')
  })

  it('flags a source that asserts A1 and loses its contests', () => {
    const split = assertedVsEarned(
      row({
        asserted: asserted({ admiralty_grade: 'A1' }),
        earned: earned({
          wins: 12,
          losses: 48,
          contested_total: 60,
          win_rate_smoothed: 0.21,
          win_rate_lower: 0.13,
          low_sample: false,
        }),
      }),
    )
    expect(split.tension).toMatch(/Loses at least as often as it wins/)
    expect(split.tension).toContain('lower bound 13%')
    expect(split.tension).toContain('n=60')
    expect(split.tension).toContain('while asserting A1')
  })

  it('raises no tension when a solid record backs no claim at all', () => {
    const split = assertedVsEarned(row({ asserted: asserted(), earned: earned() }))
    expect(split.tension).toBeNull()
  })
})

describe('freshness — absences are not faults', () => {
  it('marks empty and ungraded as absences', () => {
    expect(isFreshnessAbsence('empty')).toBe(true)
    expect(isFreshnessAbsence('ungraded')).toBe(true)
    expect(isFreshnessAbsence('ok')).toBe(false)
    expect(isFreshnessAbsence('stale')).toBe(false)
    expect(isFreshnessAbsence('warn')).toBe(false)
  })

  it('says "no signal on record" rather than inventing an age', () => {
    expect(lastSignalText(computed({ last_signal_at: null }))).toBe('no signal on record')
    expect(
      lastSignalText(computed({ last_signal_at: '2026-08-20T11:00:00Z' }), Date.parse('2026-08-20T12:00:00Z')),
    ).toBe('1h ago')
  })

  it('reports signal volume as the plain counts the server gave', () => {
    expect(signalVolumeText(computed({ signals_24h: 0, signals_7d: 3 }))).toBe(
      '0 in 24h · 3 in 7d',
    )
  })
})

describe('attention flags — a sort aid, never a composite score', () => {
  it('flags a measured losing record first', () => {
    const flags = attentionFlags(
      row({ earned: earned({ contested_total: 60, win_rate_lower: 0.2, low_sample: false }) }),
    )
    expect(flags[0]).toBe('losing_contests')
  })

  it('flags an unbacked claim whenever the record is not measured', () => {
    const a = asserted({ admiralty_grade: 'A1' })
    expect(attentionFlags(row({ asserted: a, earned: null }))).toContain('asserted_unbacked')
    expect(
      attentionFlags(row({ asserted: a, earned: earned({ contested_total: 0 }) })),
    ).toContain('asserted_unbacked')
    expect(
      attentionFlags(row({ asserted: a, earned: earned({ contested_total: 2, low_sample: true }) })),
    ).toContain('asserted_unbacked')
    // No claim ⇒ nothing to be unbacked.
    expect(attentionFlags(row({ asserted: asserted(), earned: null }))).not.toContain(
      'asserted_unbacked',
    )
  })

  it('flags the two absences distinctly and never as the same flag', () => {
    expect(attentionFlags(row({ earned: null }))).toContain('no_track_record')
    expect(attentionFlags(row({ earned: earned({ contested_total: 0 }) }))).toContain(
      'never_contested',
    )
    expect(attentionFlags(row({ earned: null }))).not.toContain('never_contested')
  })

  it('flags stale/warn freshness but NOT empty or ungraded', () => {
    expect(attentionFlags(row({ computed: computed({ freshness_grade: 'stale' }) }))).toContain(
      'overdue',
    )
    expect(attentionFlags(row({ computed: computed({ freshness_grade: 'warn' }) }))).toContain(
      'overdue',
    )
    expect(
      attentionFlags(row({ computed: computed({ freshness_grade: 'ungraded' }) })),
    ).not.toContain('overdue')
    expect(attentionFlags(row({ computed: computed({ freshness_grade: 'empty' }) }))).not.toContain(
      'overdue',
    )
  })

  it('leaves a healthy row unflagged, and ranks it last', () => {
    expect(attentionFlags(row())).toEqual([])
    expect(attentionRank(row())).toBe(99)
    expect(
      attentionRank(row({ earned: earned({ contested_total: 60, win_rate_lower: 0.2 }) })),
    ).toBe(0)
  })
})

describe('sortSourceQuality', () => {
  const losing = row({
    source_id: 'losing',
    earned: earned({ contested_total: 60, win_rate_lower: 0.2 }),
  })
  const clean = row({ source_id: 'clean' })
  const noRecord = row({ source_id: 'no-record', earned: null })
  const many = row({ source_id: 'many', earned: earned({ contested_total: 900 }) })

  it('does not mutate the input', () => {
    const input = [clean, losing]
    const out = sortSourceQuality(input, 'attention')
    expect(input).toEqual([clean, losing])
    expect(out).not.toBe(input)
  })

  it('puts the worst flag first under "attention"', () => {
    const out = sortSourceQuality([clean, noRecord, losing], 'attention')
    expect(out.map((r) => r.source_id)).toEqual(['losing', 'no-record', 'clean'])
  })

  it('sorts by contest count desc, and puts NO-RECORD rows last rather than at 0', () => {
    const out = sortSourceQuality([noRecord, clean, many], 'contested')
    expect(out.map((r) => r.source_id)).toEqual(['many', 'clean', 'no-record'])
  })

  it('sorts worst-freshness-first, keeping absences off the top', () => {
    const out = sortSourceQuality(
      [
        row({ source_id: 'ok', computed: computed({ freshness_grade: 'ok' }) }),
        row({ source_id: 'ungraded', computed: computed({ freshness_grade: 'ungraded' }) }),
        row({ source_id: 'warn', computed: computed({ freshness_grade: 'warn' }) }),
      ],
      'freshness',
    )
    expect(out.map((r) => r.source_id)).toEqual(['warn', 'ungraded', 'ok'])
  })

  it('sorts by id alphabetically under "source"', () => {
    const out = sortSourceQuality([many, clean, losing], 'source')
    expect(out.map((r) => r.source_id)).toEqual(['clean', 'losing', 'many'])
  })
})

describe('staleness debt', () => {
  it('renders the counts verbatim without deriving anything', () => {
    expect(stalenessHeadline(debt())).toBe(
      '9 debt · 10 open flags across 4 consumers · 3 foundations moved · 21 closed',
    )
  })

  it('turns match_verified=false into a caveat on the numbers', () => {
    const c = matchVerifiedCaveat(debt())
    expect(c).toMatch(/UNVERIFIED/)
    expect(c).toMatch(/lower bound/)
    expect(c).toContain('2026-08-20T06:00:00Z')
  })

  it('says so when the matcher has never run', () => {
    expect(matchVerifiedCaveat(debt({ last_matcher_run_at: null }))).toMatch(
      /matcher has no recorded run/,
    )
  })

  it('emits NO green checkmark when match_verified is true — just silence', () => {
    expect(matchVerifiedCaveat(debt({ match_verified: true }))).toBeNull()
  })

  it('gives each reason its share of the authoritative open-flag total', () => {
    const b = reasonBreakdown(debt())
    expect(b.rows).toEqual([
      { reason: 'foundation_superseded', open_flags: 6, share: 0.6 },
      { reason: 'foundation_retired', open_flags: 3, share: 0.3 },
    ])
    expect(b.truncated).toBe(true)
    expect(b.uncounted).toBe(1)
  })

  it('never fabricates a 0% share when there are no open flags', () => {
    const b = reasonBreakdown(
      debt({ open_flags: 0, by_reason: [{ reason: 'x', open_flags: 0 }] }),
    )
    expect(b.rows[0].share).toBeNull()
    expect(b.truncated).toBe(false)
  })

  it('reports the open window, or that there is none', () => {
    expect(openWindowText(debt({ open_flags: 0 }))).toBe('no open flags')
    expect(openWindowText(debt(), Date.parse('2026-08-20T12:00:00Z'))).toBe(
      'oldest 3w ago · newest 12h ago',
    )
  })
})

describe('load faults — a missing view is not a verdict on the sources', () => {
  it('classifies a 503 as not-provisioned and says no source was judged', () => {
    const fault = classifyQualityError(new ApiError(503, { detail: 'source_quality view absent' }))
    expect(fault?.kind).toBe('not_provisioned')
    expect(fault?.text).toMatch(/not provisioned/)
    expect(fault?.text).toMatch(/no source has been judged either way/i)
    expect(fault?.detail).toBe('source_quality view absent')
  })

  it('classifies anything else as a plain error', () => {
    const fault = classifyQualityError(new ApiError(500, { detail: 'pg down' }))
    expect(fault?.kind).toBe('error')
    expect(fault?.text).toContain('pg down')
  })

  it('returns null when there is no error', () => {
    expect(classifyQualityError(null)).toBeNull()
    expect(classifyQualityError(undefined)).toBeNull()
  })

  it('prefers the server detail over the generic ApiError message', () => {
    expect(apiErrorDetail(new ApiError(503, { detail: 'view absent' }))).toBe('view absent')
    expect(apiErrorDetail(new ApiError(503, {}))).toBe('API error 503')
    expect(apiErrorDetail(new Error('boom'))).toBe('boom')
    expect(apiErrorDetail('raw')).toBe('raw')
  })
})

describe('drill-down helpers', () => {
  it('sorts ratings newest-first and does not let a bad stamp jump the queue', () => {
    const out = sortRatings([
      rating({ rating_id: 'old', rated_at: '2026-01-01T00:00:00Z' }),
      rating({ rating_id: 'bad', rated_at: 'not-a-date' }),
      rating({ rating_id: 'new', rated_at: '2026-08-01T00:00:00Z' }),
    ])
    expect(out.map((r) => r.rating_id)).toEqual(['new', 'old', 'bad'])
  })

  it('describes a rating as a claim, with its author, method and references', () => {
    expect(describeRating(rating({ references: [{ url: 'x' }] }))).toBe(
      'claims A1 · by ops-desk · public · method manual · 1 reference',
    )
  })

  it('falls back to the reliability/credibility pair, and says when no grade was asserted', () => {
    expect(describeRating(rating({ grade: null }))).toContain('claims A1')
    expect(
      describeRating(
        rating({ grade: null, admiralty_reliability: null, admiralty_credibility: null }),
      ),
    ).toContain('no grade asserted')
  })

  it('says "no references cited" rather than showing a bare 0', () => {
    expect(describeRating(rating())).toContain('no references cited')
  })
})
