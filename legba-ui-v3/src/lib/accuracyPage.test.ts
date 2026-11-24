/**
 * UI-1 — the Accuracy page's pure model (`lib/accuracyPage.ts`).
 *
 * The contract under test: every figure is a FigureData with a unit and an
 * as-of; a null reading is an absence state (never 0) and a measured 0 stays
 * 0; every ledger stage carries its denominator; the nouns are the profile's;
 * the not-published list parses from the generated JSON and every live key it
 * names has a resolver.
 */
import { describe, expect, it } from 'vitest'
import raw from '@/lib/not_published.json'
import {
  LIVE_RESOLVERS,
  NOT_PUBLISHED,
  STAGE_ORDER,
  cohortModel,
  goldsetLedger,
  graderRows,
  liveReading,
  notPublishedFrom,
  stampOf,
  type AccuracyInputs,
  type RouteKey,
} from '@/lib/accuracyPage'
import type { FigureData } from '@/lib/Figure'
import { profileUiFrom, PROFILE_UI } from '@/lib/profileUi'

const READ_AT = '2026-10-04T10:00:00Z'

/** A second instance's profile: its own nouns, its own rosters. */
const SANCTIONS = profileUiFrom({
  schema: 'legba/profile_ui/1',
  profile: 'sanctions_export_control',
  subject: { singular: 'Regime', plural: 'Regimes' },
  unit_noun: { singular: 'question', plural: 'questions' },
  labels: { roster: { ofac: 'OFAC programmes' } },
  rosters: {
    subject_tags: ['ofac'],
    scored_subject_tags: ['ofac'],
    units: [{ id: 'designation_status' }, { id: 'ownership_chain' }],
  },
})

const ROSTER = {
  available: true,
  nights: 7,
  grain: 'desk' as const,
  as_of: '2026-10-04T09:20:13Z',
  desks: [
    {
      target_id: 'subject_a',
      analyst_id: 'unit_x',
      latest: {
        as_of: '2026-10-04T01:20:01Z',
        correctness_share: null,
        coverage_share: 0.0,
        n_claims: 4,
        n_contains: 0,
        n_decided: 0,
        n_silent: 4,
        single_family: true,
        reference_state: 'current' as const,
        reference_age_days: 6.7,
        badge: 'correctness unmeasured (0 decided) · coverage 0.0% (n=4)',
      },
      window: { nights_graded: 7, correctness_mean: null, coverage_mean: 0.0 },
    },
    {
      target_id: 'subject_b',
      analyst_id: 'unit_x',
      latest: {
        as_of: '2026-10-04T01:20:01Z',
        correctness_share: 0.0,
        coverage_share: 0.5,
        n_claims: 4,
        n_contains: 0,
        n_decided: 2,
        n_silent: 2,
        single_family: false,
        reference_state: 'current' as const,
        reference_age_days: 1,
        badge: 'correctness 0.0% (2 decided) · coverage 50.0% (n=4)',
      },
      window: { nights_graded: 3, correctness_mean: 0.0, coverage_mean: 0.5 },
    },
  ],
  roster: {
    desks_graded: 1,
    desks_unmeasured: 1,
    correctness_mean_of_desks: 0.0,
    coverage_mean_of_desks: 0.25,
    claims_pooled: { n_claims: 8, n_decided: 2, n_contains: 0, correctness_pooled: 0.0, coverage_pooled: 0.25 },
  },
  honesty_note: 'note',
}

const CORRECTNESS = {
  available: true,
  fleet: {
    unit: '__fleet__',
    correctness: 0.625,
    n_labels: 8,
    n_scored: 8,
    n_unresolvable: 0,
    mix: {},
    sufficient: false,
    min_labels: 30,
    status: 'indicative only — n=8 scored verdicts, below the 30 floor',
    display: 'correctness 0.62 (n=8 scored)',
    correctness_vs_reference: null,
    n_reference_labels: 0,
    reference_status: null,
    faithfulness: null,
    judge_pipeline_version: null,
  },
  units: [
    {
      unit: 'economic_coercion',
      correctness: 0.0,
      n_labels: 1,
      n_scored: 1,
      n_unresolvable: 0,
      mix: { incorrect: 1 },
      sufficient: false,
      min_labels: 10,
      status: 'indicative only',
      display: 'correctness 0.00 (n=1 scored: 0 correct / 0 partial / 1 incorrect)',
      correctness_vs_reference: null,
      n_reference_labels: 0,
      reference_status: null,
      faithfulness: 0.4,
      judge_pipeline_version: 'j2',
    },
  ],
  scored_at: '2026-10-04T03:30:19Z',
  labeling: {
    weeks: [
      { week: '2026-W40', sampled: 8, labeled: 0 },
      { week: '2026-W31', sampled: 8, labeled: 8 },
    ],
    weeks_pinned: 2,
  },
  honesty_note: 'never pooled',
  external_truth: {
    available: false,
    window: { days: 7 },
    populations: [],
    headline: {
      record: { accuracy: null, n_decided: 0, display: 'record — not graded yet' },
      voice: { accuracy: null, n_decided: 0, display: 'voice — not graded yet' },
    },
    honesty_note: 'web-grounded',
  },
}

const CAL = {
  available: true,
  produced_at: '2026-10-04T03:20:02Z',
  brier: 0.39,
  brier_exogenous: 0.39,
  exogenous_sample_size: 537,
  pooled_sample_size: 5386,
  insufficient_exogenous: false,
  self_consistency_only: false,
  brier_forecast_acute: 0.1,
  brier_skill_score: 0.05,
  forecast_acute_sample_size: 133,
  forecast_acute_ready: true,
  forecast_acute_degenerate: true,
  forecast_acute_status: 'degenerate — skill claim withheld',
  forecast_unproven: true,
  calibration_thin: false,
  refs: [],
  band_calibration: null,
}

const SINCE_ZERO = {
  cursor: READ_AT,
  server_now: '2026-10-04T10:24:55Z',
  counts: {},
  new_findings: { items: [], total: 0, truncated: false },
  superseded: { items: [], total: 0, truncated: false },
  band_changes: { items: [], total: 0, truncated: false },
  situations: { items: [], total: 0, truncated: false },
  alerts: { items: [], total: 0, truncated: false },
  forecasts_due: { items: [], total: 0, truncated: false },
}

const CRITIC = [
  { id: '1', analyst_id: 'a', analyst_version: null, scores: {}, overall_score: 0.8, produced_at: '2026-10-04T10:15:20Z', score_state: 'scored' },
  { id: '2', analyst_id: 'a', analyst_version: null, scores: {}, overall_score: 0.5, produced_at: '2026-10-03T17:20:36Z', score_state: 'carried_ungraded' },
  { id: '3', analyst_id: 'b', analyst_version: null, scores: {}, overall_score: 0.0, produced_at: '2026-10-04T01:00:00Z', score_state: 'unassessable' },
  { id: '4', analyst_id: 'b', analyst_version: null, scores: {}, overall_score: 0.7, produced_at: '2026-10-04T02:00:00Z', score_state: null },
]

const FULL: AccuracyInputs = {
  roster: ROSTER,
  critic: CRITIC,
  correctness: CORRECTNESS as unknown as AccuracyInputs['correctness'],
  calibration: CAL,
  since: SINCE_ZERO as unknown as AccuracyInputs['since'],
  readAt: READ_AT,
}

function value(f: FigureData): number | null {
  return f.state === 'measured' ? f.value : null
}

describe('stampOf', () => {
  it('prints a compact UTC stamp, or null for no instant', () => {
    expect(stampOf('2026-10-04T09:20:13.17Z')).toBe('2026-10-04 09:20Z')
    expect(stampOf(null)).toBeNull()
    expect(stampOf('not a date')).toBeNull()
  })
})

describe('cohortModel — the profile names the cohort', () => {
  it('reads the geopolitics profile\'s units and scored rosters', () => {
    const m = cohortModel(FULL)
    expect(m.profile).toBe(PROFILE_UI.profile)
    expect(m.units).toEqual(PROFILE_UI.rosters.units!.map((u) => u.replace(/_/g, ' ')))
    expect(m.rosters).toEqual(['G20', 'Watch list'])
    expect(m.nouns.subjects).toBe(PROFILE_UI.subject.plural.toLowerCase())
    expect(value(m.grader!.pairs)).toBe(2)
    expect(value(m.grader!.subjects)).toBe(2)
    expect(m.grader!.pairs.asOf).toBe('2026-10-04 09:20Z')
    expect(value(m.critic!.judgements)).toBe(4)
    expect(m.critic!.oldest).toBe('2026-10-03 17:20Z')
    expect(value(m.goldWeeks)).toBe(2)
  })

  it('a second profile renders its own nouns, never the exemplar\'s', () => {
    const m = cohortModel(FULL, SANCTIONS)
    expect(m.units).toEqual(['designation status', 'ownership chain'])
    expect(m.rosters).toEqual(['OFAC programmes'])
    expect(m.nouns.pairs).toBe('regime × question pairs')
    // the profile's words only (the grader's `grain` is the route's echo, not a noun)
    expect(JSON.stringify([m.nouns, m.units, m.rosters])).not.toMatch(/countr|desk/i)
  })

  it('nothing graded reads as absence, not a zero cohort', () => {
    const m = cohortModel({ readAt: null })
    expect(m.grader).toBeNull()
    expect(m.critic).toBeNull()
    expect(m.goldWeeks.state).toBe('not_measured')
    expect(typeof m.calibrationAsOf).toBe('object')
  })
})

describe('the sealed ledgers — every stage carries its denominator and as-of', () => {
  // The forecast ledger's "(no route)" stage table was removed 2026-11-13: the
  // forecast ledger has live reads and one home (`panels/ledger/ForecastLedger`,
  // its headline argued in `forecastLedgerModel.test.ts`). The gold set stays.
  it('the page model no longer builds a forecast stage table', async () => {
    const mod = await import('./accuracyPage')
    expect('forecastLedger' in mod).toBe(false)
    expect('FORECAST_LEDGER_NO_ROUTE' in mod).toBe(false)
  })

  it('gold-set ledger: five stages in order, each with its denominator and as-of', () => {
    const l = goldsetLedger(FULL)
    expect(l.stages.map((s) => s.id)).toEqual([...STAGE_ORDER])
    for (const s of l.stages) {
      expect(s.of.label).toBeTruthy()
      expect(s.of.figure.unit).toBeTruthy()
      expect(s.count.asOf).toBeTruthy()
    }
  })

  it('gold-set ledger: sampled → labelled → scored → voided → awaiting, each over its own denominator', () => {
    const l = goldsetLedger(FULL)
    const by = Object.fromEntries(l.stages.map((s) => [s.id, s]))
    expect(value(by.entries.count)).toBe(16)
    expect(value(by.entries.of.figure)).toBe(2)
    expect(value(by.resolved.count)).toBe(8)
    expect(value(by.resolved.of.figure)).toBe(16)
    expect(value(by.scored.count)).toBe(8)
    expect(value(by.scored.of.figure)).toBe(8)
    expect(by.voided.count.state).toBe('measured')
    expect(value(by.voided.count)).toBe(0)
    expect(value(by.awaiting.count)).toBe(8)
    expect(by.awaiting.count.asOf).toBe('2026-10-04 10:00Z (read live)')
  })

  it('gold-set ledger with no route: not measured, never 0', () => {
    for (const s of goldsetLedger({ correctness: null }).stages) {
      expect(s.count.state).toBe('not_measured')
      expect(s.of.figure.state).toBe('not_measured')
    }
  })
})

describe('graderRows — graded, population, kept out, as-of', () => {
  it('counts each grader against its own population', () => {
    const rows = Object.fromEntries(graderRows(FULL).map((r) => [r.id, r]))
    expect(value(rows.machine.graded)).toBe(1)
    expect(value(rows.machine.population.figure)).toBe(2)
    expect(value(rows.machine.keptOut!.figure)).toBe(1)
    expect(value(rows.goldset.graded)).toBe(8)
    expect(value(rows.goldset.keptOut!.figure)).toBe(0)
    // critic: scored + pre-field rows are graded; carried and unassessable are not
    expect(value(rows.critic.graded)).toBe(2)
    expect(value(rows.critic.population.figure)).toBe(4)
    expect(rows.critic.keptOut!.label).toBe('carried, ungraded')
    expect(value(rows.critic.keptOut!.figure)).toBe(1)
    expect(rows.critic.graded.asOf).toBe('2026-10-04 10:15Z')
    expect(value(rows.calibration.graded)).toBe(537)
    expect(value(rows.calibration.population.figure)).toBe(5386)
    // external truth not graded yet: unmeasured with its window, never 0
    expect(rows.truth.graded.state).toBe('unmeasured')
    expect((rows.truth.graded.asOf as { absent: string }).absent).toMatch(/7-day window/)
  })

  it('a roster that graded nothing is unmeasured, not a roster of zeros', () => {
    const rows = graderRows({ roster: { ...ROSTER, available: false } })
    const machine = rows.find((r) => r.id === 'machine')!
    expect(machine.graded.state).toBe('unmeasured')
    expect(machine.population.figure.state).toBe('unmeasured')
  })

  it('names the pair with the profile\'s nouns', () => {
    const machine = graderRows(FULL, SANCTIONS).find((r) => r.id === 'machine')!
    expect(machine.graded.unit).toBe('regime × question pairs')
  })
})

describe('what this page does not publish — generated', () => {
  it('parses the committed JSON: the measurement seams and the route classes', () => {
    expect(NOT_PUBLISHED.schema).toBe('legba/not_published/1')
    expect(NOT_PUBLISHED.seams.length).toBeGreaterThan(0)
    expect(NOT_PUBLISHED.route_classes.length).toBeGreaterThan(0)
    for (const s of NOT_PUBLISHED.seams) expect(s.source).toBe(`docs/SEAMS.md #${s.seam}`)
    for (const r of NOT_PUBLISHED.route_classes) expect(r.route).toMatch(/^\/v3\//)
    expect(NOT_PUBLISHED).toEqual(notPublishedFrom(raw))
  })

  it('every live key the generator names has a resolver here, and nothing else', () => {
    const keys = NOT_PUBLISHED.route_classes.map((r) => r.live).filter((k): k is string => !!k)
    expect(new Set(keys)).toEqual(new Set(Object.keys(LIVE_RESOLVERS)))
  })

  it('live readings: an unmeasured count is a figure with its as-of; no route → a sentence', () => {
    const grader = NOT_PUBLISHED.route_classes.find((r) => r.live === 'grader_roster.unmeasured')!
    const live = liveReading(grader, FULL)!
    expect(value(live.figure!)).toBe(1)
    expect(live.figure!.asOf).toBe('2026-10-04 09:20Z')
    expect(liveReading(grader, {})!.text).toMatch(/unmeasured, not zero/)
    const floor = NOT_PUBLISHED.route_classes.find((r) => r.live === 'correctness.below_floor')!
    expect(value(liveReading(floor, FULL)!.figure!)).toBe(1)
  })

  it('reads a malformed document as empty, never throws', () => {
    expect(notPublishedFrom(null)).toEqual({ schema: '', seams: [], route_classes: [] })
    expect(notPublishedFrom({ seams: [{ seam: 'x' }], route_classes: [{}] }).seams).toEqual([])
  })
})

describe('a route still in flight reads "reading", never an answer', () => {
  const ALL: Partial<Record<RouteKey, boolean>> = { roster: true, critic: true, correctness: true, calibration: true, since: true }

  it('the cohort, the gold-set ledger and the grader table say "reading <route>" while pending', () => {
    const i: AccuracyInputs = { pending: ALL }
    const m = cohortModel(i)
    expect(m.pending).toEqual({ grader: true, critic: true })
    expect(m.goldWeeks.state).toBe('dash')
    expect(m.calibrationAsOf).toEqual({ absent: 'reading /v3/eval/calibration' })
    for (const s of goldsetLedger(i).stages) {
      expect(s.count.state).toBe('dash')
      expect(s.of.figure.state).toBe('dash')
    }
    for (const r of graderRows(i)) {
      expect(r.graded.state).toBe('dash')
      expect(r.population.figure.state).toBe('dash')
    }
    for (const c of NOT_PUBLISHED.route_classes.filter((r) => r.live)) {
      expect(liveReading(c, i)!.text).toMatch(/^reading \/v3\//)
    }
  })
})
