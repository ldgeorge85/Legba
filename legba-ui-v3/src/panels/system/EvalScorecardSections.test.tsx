/**
 * UI-1 — the Accuracy page's sections on fixture responses.
 *
 *  - the cohort paragraph uses the profile's nouns (a second profile renders
 *    its own, never the exemplar's);
 *  - every gold-set stage renders a count AND its denominator, each with an
 *    as-of; a measured 0 is 0; the forecast ledger is its live one home
 *    (`ForecastLedger`), never the old "(no route)" stage table;
 *  - the grader table: a roster that graded nothing is *unmeasured*; a null
 *    correctness share is *unmeasured*; a measured 0.0 stays 0.0;
 *  - the not-published list renders from the generated JSON;
 *  - the page stacks the five sections in the brief's order.
 */
import { describe, expect, it, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import { NOT_PUBLISHED, STAGE_ORDER, type AccuracyInputs } from '@/lib/accuracyPage'
import type { GraderRoster } from '@/lib/graderRosterModel'
import { profileUiFrom } from '@/lib/profileUi'
import type { PanelRegistration } from '@/types'
import EvalScorecardPanel from './EvalScorecard'
import { EvalGraderRoster } from './EvalGraderRoster'
import { BANDS_OPEN_MAX, SubjectBands } from './EvalScorecardBands'
import { CohortSection } from './EvalScorecardCohort'
import { OperatorCorrectness } from './EvalScorecardGoldset'
import { GraderTable } from './EvalScorecardGraders'
import { LedgerSection } from './EvalScorecardLedger'
import { NotPublishedSection } from './EvalScorecardNotPublished'

const SANCTIONS = profileUiFrom({
  schema: 'legba/profile_ui/1',
  profile: 'sanctions_export_control',
  subject: { singular: 'Regime', plural: 'Regimes' },
  unit_noun: { singular: 'question', plural: 'questions' },
  labels: { roster: { ofac: 'OFAC programmes' } },
  rosters: { subject_tags: ['ofac'], scored_subject_tags: ['ofac'], units: [{ id: 'designation_status' }, { id: 'ownership_chain' }] },
})

const LATEST = {
  as_of: '2026-10-04T01:20:01Z',
  n_claims: 4,
  n_contains: 0,
  n_silent: 2,
  single_family: false,
  reference_state: 'current' as const,
  reference_age_days: 1,
}

const ROSTER: GraderRoster = {
  available: true,
  nights: 7,
  grain: 'desk',
  as_of: '2026-10-04T09:20:13Z',
  desks: [
    {
      target_id: 'subject_a',
      analyst_id: 'unit_x',
      latest: { ...LATEST, correctness_share: null, coverage_share: 0.0, n_decided: 0, badge: 'correctness unmeasured' },
      window: { nights_graded: 7, correctness_mean: null, coverage_mean: 0.0 },
    },
    {
      target_id: 'subject_b',
      analyst_id: 'unit_x',
      latest: { ...LATEST, correctness_share: 0.0, coverage_share: 0.5, n_decided: 2, badge: 'correctness 0.0%' },
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
  honesty_note: 'roster note',
}

const UNIT_ZERO = {
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
}

const CORRECTNESS = {
  available: true,
  fleet: { ...UNIT_ZERO, unit: '__fleet__', n_labels: 8, n_scored: 8, display: 'correctness 0.62 (n=8)' },
  units: [UNIT_ZERO],
  scored_at: '2026-10-04T03:30:19Z',
  labeling: { weeks: [{ week: '2026-W40', sampled: 8, labeled: 0 }, { week: '2026-W31', sampled: 8, labeled: 8 }], weeks_pinned: 2 },
  honesty_note: 'never pooled',
  external_truth: null,
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

const SINCE = {
  cursor: '2026-10-04T10:00:00Z',
  server_now: '2026-10-04T10:24:55Z',
  counts: {},
  forecasts_due: { items: [], total: 0, truncated: false },
}

const INPUTS: AccuracyInputs = {
  roster: ROSTER,
  critic: [],
  correctness: CORRECTNESS as unknown as AccuracyInputs['correctness'],
  calibration: CAL,
  since: SINCE as unknown as AccuracyInputs['since'],
  readAt: '2026-10-04T10:00:00Z',
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('1 · cohort', () => {
  it('names the units and rosters in the profile\'s own words', () => {
    render(<CohortSection inputs={INPUTS} />)
    const p = screen.getByTestId('accuracy-cohort-paragraph')
    expect(p).toHaveTextContent('G20 and Watch list rosters')
    expect(p).toHaveTextContent('leadership transition')
    expect(screen.getByTestId('accuracy-cohort-pairs-value')).toHaveTextContent('2')
    expect(screen.getByTestId('accuracy-cohort-pairs-asof')).toHaveTextContent('as of 2026-10-04 09:20Z')
  })

  it('a second profile renders its own nouns, never "countries"', () => {
    render(<CohortSection inputs={INPUTS} ui={SANCTIONS} />)
    const p = screen.getByTestId('accuracy-cohort-paragraph')
    expect(p).toHaveTextContent('2 questions of the sanctions_export_control profile')
    expect(p).toHaveTextContent('designation status and ownership chain')
    expect(p).toHaveTextContent('over the regimes on its OFAC programmes roster.')
    expect(screen.getByTestId('accuracy-cohort-pairs')).toHaveAttribute('data-unit', 'regime × question pairs')
    expect(p.textContent).not.toMatch(/countr/i)
  })

  it('nothing graded reads unmeasured, not zero', () => {
    render(<CohortSection inputs={{}} />)
    expect(screen.getByTestId('accuracy-cohort-grader')).toHaveTextContent('unmeasured, not zero')
    expect(screen.getByTestId('accuracy-cohort-weeks')).toHaveAttribute('data-state', 'not_measured')
  })
})

describe('2 · sealed ledgers', () => {
  it('every stage of the gold-set ledger carries a count and its denominator, each with an as-of', () => {
    render(<LedgerSection inputs={INPUTS} />)
    for (const stage of STAGE_ORDER) {
      const row = screen.getByTestId(`ledger-goldset-${stage}`)
      expect(within(row).getByTestId(`ledger-goldset-${stage}-count-asof`)).toHaveTextContent(/^as of /)
      expect(within(row).getByTestId(`ledger-goldset-${stage}-of-asof`)).toHaveTextContent(/^as of /)
    }
    expect(screen.getByTestId('ledger-goldset-voided-count-value')).toHaveTextContent(/^0 verdicts unresolvable$/)
    expect(screen.getByTestId('ledger-goldset-awaiting-count-value')).toHaveTextContent('8 findings')
  })

  // UI 2026-11-13 — the dead "Forecast ledger" stage table (every cell "not
  // measured (no route)") is gone; the forecast ledger's one home is the
  // Ledger surface's Forecasts region, and this section points there first.
  it('no "(no route)" forecast table; a one-line pointer to the forecasts comes first', () => {
    render(<LedgerSection inputs={INPUTS} />)
    expect(screen.queryByTestId('ledger-forecast')).toBeNull()
    expect(screen.queryByTestId('forecast-ledger')).toBeNull()
    expect(screen.getByTestId('accuracy-ledger').textContent).not.toMatch(/no route/)
    const pointer = screen.getByTestId('accuracy-forecasts-pointer')
    const gold = screen.getByTestId('ledger-goldset')
    expect(pointer.compareDocumentPosition(gold) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })
})

describe('3 · calibration — the banded verdicts per subject', () => {
  const EVAL = { faithfulness: null, correctness_vs_reference: null, n_labeled: 0, faithfulness_flagged: false }
  const card = (id: string) => ({
    target_id: id,
    id: `sc-${id}`,
    produced_at: '2026-09-21T00:00:00Z',
    generated_at: '2026-09-21T06:30:00Z',
    floors: {},
    dimensions: {
      escalation: {
        band: 'high', basis: ['f1'], severity_tag: null, effective_confidence: 0.61, confidence: 0.7,
        critic_score: 0.8, damped: false, reason: 'banded', produced_at: '2026-09-21T05:00:00Z', eval: EVAL,
      },
    },
    composition: { present: false, basis: [] },
    method_version: null,
  })

  it('a long roster folds behind one "show all" in the profile\'s noun; a short one renders open', () => {
    const many = Array.from({ length: BANDS_OPEN_MAX + 1 }, (_, i) => card(`subject_${i}`))
    const { unmount } = render(<SubjectBands scorecards={many as never} />)
    expect(screen.queryByTestId('scorecard-card-subject_0')).toBeNull()
    const toggle = screen.getByTestId('scorecard-show-all')
    expect(toggle).toHaveTextContent(`show all ${BANDS_OPEN_MAX + 1} desks`)
    fireEvent.click(toggle)
    expect(screen.getByTestId('scorecard-card-subject_0')).toBeInTheDocument()
    expect(screen.getByTestId('scorecard-csv')).not.toBeDisabled()
    unmount()
    render(<SubjectBands scorecards={[card('subject_a')] as never} />)
    expect(screen.queryByTestId('scorecard-show-all')).toBeNull()
    expect(screen.getByTestId('scorecard-card-subject_a')).toBeInTheDocument()
    expect(screen.getByTestId('scorecard-country-list')).toHaveTextContent('banded verdicts (per desk)')
  })
})

describe('4 · graders', () => {
  it('per grader: graded, population, kept out — the roster\'s unmeasured pairs counted, not zeroed', () => {
    render(<GraderTable inputs={INPUTS} />)
    expect(screen.getByTestId('grader-machine-graded-value')).toHaveTextContent('1 desk × unit pairs')
    expect(screen.getByTestId('grader-machine-population-value')).toHaveTextContent('2 desk × unit pairs')
    expect(screen.getByTestId('grader-machine-kept-out-value')).toHaveTextContent('1 desk × unit pairs')
    expect(screen.getByTestId('grader-goldset-kept-out-value')).toHaveTextContent(/^0 verdicts$/)
    expect(screen.getByTestId('grader-truth-graded')).toHaveAttribute('data-state', 'unmeasured')
  })

  it('a roster that graded nothing is unmeasured, never a row of zeros', () => {
    render(<GraderTable inputs={{ roster: { ...ROSTER, available: false } }} />)
    expect(screen.getByTestId('grader-machine-graded-value')).toHaveTextContent('unmeasured')
    expect(screen.getByTestId('grader-machine-population-value')).toHaveTextContent('unmeasured')
  })

  it('a null share is unmeasured; a measured 0.0 stays 0.0', () => {
    render(
      <>
        <EvalGraderRoster roster={ROSTER} />
        <OperatorCorrectness correctness={CORRECTNESS as unknown as AccuracyInputs['correctness']} />
      </>,
    )
    const nullRow = screen.getByTestId('grader-roster-row-subject_a:unit_x')
    expect(nullRow).toHaveTextContent('unmeasured')
    expect(nullRow).toHaveTextContent('0.0%') // its coverage: a measured zero
    const zeroRow = screen.getByTestId('grader-roster-row-subject_b:unit_x')
    expect(zeroRow).not.toHaveTextContent('unmeasured')
    expect(zeroRow).toHaveTextContent('0.0%')
    expect(screen.getByTestId('operator-correctness-economic_coercion')).toHaveTextContent('correctness 0.00')
  })
})

describe('5 · what this page does not publish', () => {
  it('renders every generated item — the measurement seams and the route classes', () => {
    render(<NotPublishedSection inputs={INPUTS} />)
    for (const s of NOT_PUBLISHED.seams) {
      const li = screen.getByTestId(`not-published-${s.id}`)
      expect(li).toHaveTextContent(s.source)
    }
    for (const c of NOT_PUBLISHED.route_classes) {
      expect(screen.getByTestId(`not-published-${c.id}`)).toHaveTextContent(c.route)
    }
    // the roster's unmeasured class carries today's count, with its as-of
    expect(screen.getByTestId('not-published-grader_unmeasured-live-value')).toHaveTextContent('1 desk × unit pairs unmeasured this window')
  })

  it('renders exactly the document it is given', () => {
    const doc = {
      schema: 'legba/not_published/1',
      seams: [{ id: 'seam-1', seam: 1, title: 'A seam', summary: 'Not built.', guard: 'Refuses.', source: 'docs/SEAMS.md #1' }],
      route_classes: [],
    }
    render(<NotPublishedSection inputs={{}} doc={doc} />)
    expect(screen.getByTestId('not-published-seam-1')).toHaveTextContent('A seam — Not built. Refuses.')
    expect(screen.getByTestId('accuracy-not-published').querySelectorAll('li')).toHaveLength(1)
  })
})

describe('the page — five sections in order', () => {
  function reg(): PanelRegistration {
    return {
      id: 'p',
      panel_id: 'system_eval_scorecard',
      descriptor_id: 'eval.scorecard',
      descriptor_version: 'v' + 'a'.repeat(63),
      descriptor_family: 'analyst',
      analyst_id: null,
      title: 'Eval Scorecard',
      mode: 'personal',
      layout_slot: 'system.eval.main',
      data_query: {},
      binding: {},
      retired: false,
      created_at: '2026-05-20T00:00:00Z',
      retired_at: null,
    }
  }
  function wrap(ui: ReactElement) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
  }

  // Synthetic sealed-ledger reads for the forecast ledger's one home.
  const LEDGER_FORECASTS = {
    version: '2026-11/st0',
    available: true,
    forecasts: [
      {
        id: '1',
        slug: 'FX1-soon',
        question: 'Fictional soon question',
        resolver_kind: 'official_result',
        due_at: '2026-11-29T23:59:59+00:00',
        status: 'open',
        sealed_at: '2026-11-11T00:00:00+00:00',
        numbers: [{ issuer_kind: 'operator', issuer_ref: 'operator', p: 0.3, sealed: true }],
      },
    ],
  }
  const LEDGER_SCORECARD = {
    available: true,
    scores_withheld: true,
    scores_min_resolved: 5,
    header: {
      n_questions: 3, n_sealed: 3, n_open: 3, n_resolved: 0, n_void: 0, n_overdue: 0,
      min_sample: 5, skill_claim: false, statement: 'fictional statement',
    },
    questions: [],
    arms: [],
  }

  it('cohort → ledgers → calibration → graders → not published, over the live route shapes', async () => {
    const body = (url: string): unknown => {
      if (url.includes('/v3/eval/scorecard')) return []
      if (url.includes('/v3/eval/calibration')) return CAL
      if (url.includes('/v3/eval/correctness')) return CORRECTNESS
      if (url.includes('/v3/eval/grader_roster')) return ROSTER
      if (url.includes('/v3/ledger/forecasts')) return LEDGER_FORECASTS
      if (url.includes('/v3/ledger/scorecard')) return LEDGER_SCORECARD
      return []
    }
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => Promise.resolve({ ok: true, json: async () => body(url) })),
    )
    render(wrap(<EvalScorecardPanel registration={reg()} scope={{}} mode="personal" />))
    // The forecast ledger's one home is the Ledger surface's Forecasts region;
    // section 2 names it in one line and mounts no second copy.
    await waitFor(() => expect(screen.getByTestId('accuracy-forecasts-pointer')).toHaveTextContent('on the Ledger, under Forecasts'))
    const ids = ['accuracy-cohort', 'accuracy-ledger', 'accuracy-calibration', 'accuracy-graders', 'accuracy-not-published']
    const els = ids.map((id) => screen.getByTestId(id))
    for (let i = 1; i < els.length; i++) {
      expect(els[i - 1].compareDocumentPosition(els[i]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    }
    expect(within(screen.getByTestId('accuracy-ledger')).queryByTestId('forecast-ledger')).toBeNull()
    // The dead "(no route)" table is gone.
    expect(screen.queryByTestId('ledger-forecast')).toBeNull()
    expect(screen.getByTestId('accuracy-ledger').textContent).not.toMatch(/no route/)
  })

  it('first paint, before any route answers: the cohort and the stage tables, reading — not a spinner, not "unmeasured"', () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => new Promise(() => {})))
    render(wrap(<EvalScorecardPanel registration={reg()} scope={{}} mode="personal" />))
    const cohort = screen.getByTestId('accuracy-cohort-paragraph')
    expect(cohort).toHaveTextContent('This page reads the 9 units')
    expect(cohort).toHaveTextContent("The correctness grader's roster is being read.")
    expect(cohort).not.toHaveTextContent('unmeasured')
    expect(screen.getByTestId('ledger-goldset')).toBeInTheDocument()
    expect(screen.getByTestId('ledger-goldset-entries-count-value')).toHaveTextContent('—')
    expect(screen.queryByText(/loading scorecard/)).toBeNull()
  })
})
