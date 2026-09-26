/**
 * Component test for the UI-5 Eval Scorecard.
 *
 * Asserts:
 *  - renders per-analyst cards from the mocked `/v3/eval/scorecard`
 *  - worst-scoring analyst surfaces first
 *  - per-axis rubric bars render
 *  - expanding a card with >1 judgement reveals the critic-score trend chart
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import EvalScorecardPanel from './EvalScorecard'
import type { PanelRegistration } from '@/types'

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

const PAGE = [
  {
    id: 's1',
    analyst_id: 'cred.analyst',
    analyst_version: 'v1',
    scores: { calibration: 0.4, evidence: 0.6 },
    overall_score: 0.5,
    ground_truth_accuracy: 0.45,
    produced_at: '2026-06-01T00:00:00Z',
  },
  {
    id: 's2',
    analyst_id: 'cred.analyst',
    analyst_version: 'v1',
    scores: { calibration: 0.8, evidence: 0.8 },
    overall_score: 0.8,
    ground_truth_accuracy: 0.7,
    produced_at: '2026-06-03T00:00:00Z',
  },
  {
    id: 's3',
    analyst_id: 'coup.analyst',
    analyst_version: 'v2',
    scores: { calibration: 0.2 },
    overall_score: 0.3,
    produced_at: '2026-06-02T00:00:00Z',
  },
]

// Routes by URL rather than a blanket mock: the panel also queries
// /v3/eval/calibration and /v3/eval/country_scorecard, which are NOT
// ScorecardRow-shaped — feeding them the same PAGE fixture crashes the
// (unrelated) country-scorecard render path once its query resolves.
function stubFetch() {
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    if (url.includes('/v3/eval/scorecard')) {
      return Promise.resolve({ ok: true, json: async () => PAGE })
    }
    return Promise.resolve({ ok: true, json: async () => [] })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('EvalScorecardPanel', () => {
  it('renders per-analyst cards, worst-first, with rubric bars', async () => {
    stubFetch()
    render(wrap(<EvalScorecardPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('eval-card-cred.analyst')).toBeInTheDocument())
    const list = screen.getByTestId('eval-scorecard-list')
    const cards = within(list).getAllByTestId(/^eval-card-/)
    // coup.analyst (0.3) is worst → first
    expect(cards[0]).toHaveAttribute('data-testid', 'eval-card-coup.analyst')
    expect(screen.getByTestId('eval-axis-cred.analyst-calibration')).toBeInTheDocument()
  })

  it('expands to show the critic-score trend when >1 judgement exists', async () => {
    stubFetch()
    render(wrap(<EvalScorecardPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('eval-card-cred.analyst')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('eval-card-header-cred.analyst'))
    await waitFor(() => {
      expect(screen.getByTestId('eval-trend-cred.analyst')).toBeInTheDocument()
    })
  })

  it('shows empty state when no judgements', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => [] })
    vi.stubGlobal('fetch', fetchMock)
    render(wrap(<EvalScorecardPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => {
      expect(screen.getByText(/no critic judgements yet/)).toBeInTheDocument()
    })
  })
})

describe('EvalScorecardPanel band calibration section', () => {
  it('shows the honest awaiting state when no band_calibration section is served', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.includes('/v3/eval/calibration')) {
        return Promise.resolve({
          ok: true,
          json: async () => ({ available: false, band_calibration: null }),
        })
      }
      return Promise.resolve({ ok: true, json: async () => [] })
    })
    vi.stubGlobal('fetch', fetchMock)
    render(wrap(<EvalScorecardPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => {
      expect(screen.getByTestId('band-calibration-empty')).toBeInTheDocument()
    })
  })

  it('renders 14d/28d persistence + reversal rates, honestly labeled (never "Brier")', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.includes('/v3/eval/calibration')) {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            available: true,
            produced_at: '2026-07-27T00:00:00Z',
            band_calibration: {
              available: true,
              produced_at: '2026-07-27T00:00:00Z',
              claims_total: 20,
              resolution_spec: 'hard_band_at_horizon_v1',
              horizons: {
                '14d': {
                  resolved: 10,
                  open: 2,
                  outcomes: { held: 6, reverted: 2, worsened: 2 },
                  confirmed: 8,
                  reverted: 2,
                  scored: 10,
                  excluded_insufficient: 0,
                  excluded_unresolvable: 0,
                  persistence_rate: 0.8,
                  reversal_rate: 0.2,
                },
                '28d': {
                  resolved: 6,
                  open: 6,
                  outcomes: { held: 3, reverted: 3 },
                  confirmed: 3,
                  reverted: 3,
                  scored: 6,
                  excluded_insufficient: 0,
                  excluded_unresolvable: 0,
                  persistence_rate: 0.5,
                  reversal_rate: 0.5,
                },
              },
              by_direction: {},
              by_dimension: {},
              no_brier: true,
              honesty_note: 'Band-persistence and reversal rates are ordinal stability measures.',
              refs: ['bc-1'],
            },
          }),
        })
      }
      return Promise.resolve({ ok: true, json: async () => [] })
    })
    vi.stubGlobal('fetch', fetchMock)
    render(wrap(<EvalScorecardPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => {
      expect(screen.getByTestId('band-calibration-horizon-14d')).toBeInTheDocument()
    })
    expect(screen.getByTestId('band-calibration-persistence-14d')).toHaveTextContent('80%')
    expect(screen.getByTestId('band-calibration-reversal-14d')).toHaveTextContent('20%')
    expect(screen.getByTestId('band-calibration-persistence-28d')).toHaveTextContent('50%')
    expect(screen.queryByTestId('band-calibration-empty')).not.toBeInTheDocument()
    // The route's own no-Brier honesty note renders verbatim — the panel
    // never invents its own (potentially drifting) honesty copy.
    expect(screen.getByTestId('band-calibration-honesty-note')).toHaveTextContent(
      'ordinal stability measures',
    )
  })
})

// ── W-6 · the fourth stacked section ─────────────────────────────────────────

const TRUTH_POP = {
  population: 'assembly_span',
  label: 'the record (desk sentences, quoted)',
  display:
    'the record (desk sentences, quoted) 0.71 (n=1,148 decided of 3,290 searched; decided rate 0.35)',
  accuracy: 0.7125,
  supported: 818,
  contradicted: 330,
  not_found: 2142,
  n_decided: 1148,
  n_searched: 3290,
  n_unchecked: 1799,
  n_uncheckable: 343,
  decided_rate: 0.349,
  sufficient: true,
  min_decided: 10,
  strata: {
    regime: { assembly: { accuracy: 0.71, n_decided: 1148 } },
    retrieval_origin: {},
    source_tier: { tier_unknown: { accuracy: null, n_decided: 4 } },
  },
  instrument: { overlap_raw: 0.79, overlap_n: 329, band: 'contingent', instrument_limited: false },
  search: { degraded_share: 0.42, provider_mix: { searxng: 1 } },
  sampled: {},
}

function stubCorrectness(external_truth: unknown) {
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    if (url.includes('/v3/eval/correctness')) {
      return Promise.resolve({
        ok: true,
        json: async () => ({
          available: true,
          fleet: null,
          units: [],
          scored_at: null,
          labeling: {},
          honesty_note: 'operator note',
          external_truth,
        }),
      })
    }
    return Promise.resolve({ ok: true, json: async () => [] })
  })
  vi.stubGlobal('fetch', fetchMock)
}

describe('EvalScorecardPanel standing external truth', () => {
  it('renders the honest empty state before the ledger lands', async () => {
    stubCorrectness({
      available: false,
      window: { days: 7 },
      populations: [],
      headline: {},
      honesty_note: 'never pooled',
    })
    render(wrap(<EvalScorecardPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() =>
      expect(screen.getByTestId('external-truth-empty')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('external-truth-empty')).toHaveTextContent(
      'no claims graded against the world yet',
    )
  })

  it('renders the PAIR — record and voice, never summed — with both n\'s', async () => {
    stubCorrectness({
      available: true,
      window: { days: 7 },
      populations: [TRUTH_POP],
      headline: {
        record: { accuracy: 0.7125, n_decided: 1148 },
        voice: { accuracy: 0.68, n_decided: 56 },
        note: 'Two numbers, never one.',
      },
      honesty_note: 'never pooled with faithfulness',
    })
    render(wrap(<EvalScorecardPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() =>
      expect(screen.getByTestId('external-truth-headline')).toBeInTheDocument(),
    )
    const headline = screen.getByTestId('external-truth-headline')
    expect(headline).toHaveTextContent('record 0.71 (n=1148)')
    expect(headline).toHaveTextContent('voice 0.68 (n=56)')
    // The two are never combined into one figure on the page.
    expect(headline).not.toHaveTextContent('1204')
    expect(screen.getByTestId('external-truth-headline-note')).toHaveTextContent(
      'Two numbers, never one',
    )
    // decided_rate and the excluded counts ride the section, so a 0.71 can
    // never be read as "71% of the read is true".
    expect(screen.getByTestId('external-truth-label-assembly_span')).toHaveTextContent(
      'decided rate 0.35',
    )
    expect(screen.getByTestId('external-truth-instrument-assembly_span')).toHaveTextContent(
      'grader overlap 0.79 (n=329, contingent)',
    )
    // F-3's visible class is rendered as its own stratum cell.
    expect(
      screen.getByTestId('external-truth-stratum-assembly_span-source_tier'),
    ).toHaveTextContent('tier_unknown')
    // ...and an empty stratum says "no rows yet" rather than vanishing.
    expect(
      screen.getByTestId('external-truth-stratum-assembly_span-retrieval_origin'),
    ).toHaveTextContent('no rows yet')
  })

  it('replaces the number with a sentence when the instrument is limited', async () => {
    stubCorrectness({
      available: true,
      window: { days: 7 },
      populations: [
        {
          ...TRUTH_POP,
          instrument: {
            overlap_raw: 0.7451,
            overlap_n: 329,
            band: 'instrument_limited',
            instrument_limited: true,
          },
        },
      ],
      headline: { record: { accuracy: null, n_decided: 1148 }, voice: { accuracy: null, n_decided: 0 } },
      honesty_note: 'never pooled',
    })
    render(wrap(<EvalScorecardPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() =>
      expect(screen.getByTestId('external-truth-label-assembly_span')).toBeInTheDocument(),
    )
    const label = screen.getByTestId('external-truth-label-assembly_span')
    expect(label).toHaveTextContent('did not agree with each other often enough')
    expect(label).not.toHaveTextContent('0.71')
    // The headline shows the em dash, never a 0.00 standing in for absence.
    expect(screen.getByTestId('external-truth-headline')).toHaveTextContent('record —')
  })
})

describe('EvalScorecardPanel band CSV export (7b-iii)', () => {
  const EMPTY_EVAL = {
    faithfulness: null,
    correctness_vs_reference: null,
    n_labeled: 0,
    faithfulness_flagged: false,
  }
  const CARD = {
    target_id: 'country_g20_tr',
    id: 'sc-1',
    produced_at: '2026-09-21T00:00:00Z',
    generated_at: '2026-09-21T06:30:00Z',
    floors: {},
    dimensions: {
      escalation: {
        band: 'high',
        basis: ['f1', 'f2'],
        severity_tag: null,
        effective_confidence: 0.61,
        confidence: 0.7,
        critic_score: 0.8,
        damped: false,
        reason: 'banded',
        produced_at: '2026-09-21T05:00:00Z',
        eval: EMPTY_EVAL,
      },
      energy_security: {
        band: 'insufficient-evidence',
        basis: [],
        severity_tag: null,
        effective_confidence: null,
        confidence: null,
        critic_score: null,
        damped: false,
        reason: 'no_verified_claim',
        produced_at: null,
        eval: EMPTY_EVAL,
      },
    },
    composition: { present: false, basis: [] },
    // Pre-H12 card: no method_version stamp — it must export EMPTY.
    method_version: null,
  }

  /** jsdom's Blob has no `.text()`; FileReader is the supported read. */
  function blobText(b: Blob): Promise<string> {
    return new Promise((resolve, reject) => {
      const r = new FileReader()
      r.onload = () => resolve(String(r.result))
      r.onerror = () => reject(r.error)
      r.readAsText(b)
    })
  }

  function stubCards(cards: unknown[]) {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((url: string) => {
        if (url.includes('/v3/eval/country_scorecard')) {
          return Promise.resolve({ ok: true, json: async () => cards })
        }
        return Promise.resolve({ ok: true, json: async () => [] })
      }),
    )
  }

  it('is disabled while no scorecard has been computed', async () => {
    stubCards([])
    render(wrap(<EvalScorecardPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('scorecard-empty')).toBeInTheDocument())
    expect(screen.getByTestId('scorecard-csv')).toBeDisabled()
  })

  it('downloads the bands on the page — every dimension, absences as empty cells', async () => {
    stubCards([CARD])
    let blob: Blob | null = null
    let downloadName = ''
    vi.stubGlobal('URL', {
      ...URL,
      createObjectURL: vi.fn((b: Blob) => {
        blob = b
        return 'blob:mock'
      }),
      revokeObjectURL: vi.fn(),
    })
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      downloadName = this.download
    })

    render(wrap(<EvalScorecardPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() =>
      expect(screen.getByTestId('scorecard-card-country_g20_tr')).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByTestId('scorecard-csv'))

    expect(downloadName).toBe('legba_bands_country_g20_tr_20260921.csv')
    expect(blob).not.toBeNull()
    const lines = (await blobText(blob!)).trim().split('\r\n')
    expect(lines[0]).toBe(
      'desk_id,desk,dimension,band,verified_basis_count,band_produced_at,' +
        'card_generated_at,method_version',
    )
    // Both dimensions travel — the insufficient one is NOT dropped, its basis
    // count is a real 0, and the two absent stamps are empty cells.
    expect(lines).toHaveLength(3)
    expect(lines[1]).toBe(
      'country_g20_tr,Turkey,energy_security,insufficient-evidence,0,,2026-09-21T06:30:00Z,',
    )
    expect(lines[2]).toBe(
      'country_g20_tr,Turkey,escalation,high,2,2026-09-21T05:00:00Z,2026-09-21T06:30:00Z,',
    )
  })
})
