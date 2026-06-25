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

function stubFetch() {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => PAGE })
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
