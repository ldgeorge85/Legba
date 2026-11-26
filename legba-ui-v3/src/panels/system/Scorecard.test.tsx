/**
 * Component test for the Scorecard tab: a pre-resolution payload (0
 * resolved, n too small, single arm, no Brier) and a post-resolution one.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import ScorecardPanel from './Scorecard'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 'p',
    panel_id: 'system_ledgers',
    descriptor_id: 'ledgers',
    descriptor_version: 'v' + 'a'.repeat(63),
    descriptor_family: 'analyst',
    analyst_id: null,
    title: 'Ledgers',
    mode: 'personal',
    layout_slot: 'system.ledgers.main',
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-11-10T00:00:00Z',
    retired_at: null,
  }
}
function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}
function stubFetch(body: unknown, status = 200) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: status < 400,
    status,
    statusText: 'x',
    json: async () => body,
    text: async () => JSON.stringify(body),
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}
const mount = () => render(wrap(<ScorecardPanel registration={reg()} scope={{}} mode="personal" />))

const SMALL_ARM = (kind: string) => ({
  issuer_kind: kind,
  n: 0,
  mean_brier: null,
  baseline_mean_brier: null,
  skill_vs_climatology: null,
  below_min_sample: true,
  note: 'n too small (0 < 5)',
})
const NOTE = 'This scorecard reads only the sealed forecast ledger. The legacy figures are untrusted.'

function question(slug: string, extra: Record<string, unknown> = {}) {
  return {
    id: slug,
    slug,
    question: `Fictional question ${slug}`,
    status: 'open',
    resolver_kind: 'official_result',
    due_at: '2026-12-01T00:00:00+00:00',
    overdue: false,
    single_arm: false,
    numbers: [
      { issuer_kind: 'operator', issuer_ref: 'operator', p: 0.85, absent_reason: null, brier: null },
      { issuer_kind: 'tower', issuer_ref: 'tower', p: 0.968, absent_reason: null, brier: null },
    ],
    spread: { n: 2, min: 0.85, max: 0.968, range: 0.118 },
    outcome: null,
    resolution: null,
    proposal: null,
    ...extra,
  }
}

const PRE = {
  available: true,
  header: {
    n_questions: 3, n_sealed: 3, n_open: 3, n_resolved: 0, n_void: 0, n_overdue: 0,
    min_sample: 5, calibration_min_sample: 30, skill_claim: false,
    statement: '0 resolved of 3 sealed questions. No skill claim is made below 5 resolved questions.',
  },
  separation_note: NOTE,
  questions: [
    question('FX1-alpha'),
    question('FX2-beta', {
      numbers: [
        { issuer_kind: 'operator', issuer_ref: 'operator', p: 0.6 },
        { issuer_kind: 'tower', issuer_ref: 'tower', p: null, absent_reason: 'no analyst number' },
      ],
      spread: null,
    }),
    question('FX8-solo', {
      single_arm: true,
      numbers: [{ issuer_kind: 'operator', issuer_ref: 'operator', p: 0.75 }],
      spread: null,
    }),
  ],
  arms: [SMALL_ARM('operator'), SMALL_ARM('tower')],
  single_arm: [{ slug: 'FX8-solo', issuer_kind: 'operator', p: 0.75, brier: null, outcome: null }],
  calibration: { available: false, min_sample: 30, n: 0, reason: 'n too small (0 < 30)', bins: [{ lo: 0, hi: 0.1, n: 0, mean_p: null, observed: null }] },
  due_next: [{ slug: 'FX1-alpha', due_at: '2026-12-01T00:00:00+00:00', grace_until: null, resolver_kind: 'official_result', status: 'open' }],
  void_questions: [],
}

const POST = {
  ...PRE,
  scores_withheld: false,
  header: { ...PRE.header, n_resolved: 6, n_open: 1, n_void: 1, skill_claim: true, statement: '6 resolved of 8 sealed questions.' },
  questions: [
    question('FX1-alpha', {
      status: 'resolved',
      outcome: 'yes',
      numbers: [
        { issuer_kind: 'operator', issuer_ref: 'operator', p: 0.85, brier: 0.04 },
        { issuer_kind: 'tower', issuer_ref: 'tower', p: 0.968, brier: 0.001 },
      ],
      resolution: { id: 'r1', outcome: 'yes', evidence_refs: ['url:https://example.test/result', 'signal:abc123'] },
    }),
    question('FX9-gone', { status: 'void', outcome: 'void' }),
  ],
  arms: [
    { issuer_kind: 'operator', n: 6, mean_brier: 0.1234, baseline_mean_brier: 0.25, skill_vs_climatology: 0.506, below_min_sample: false },
    SMALL_ARM('tower'),
  ],
  calibration: { available: true, min_sample: 30, n: 31, bins: [{ lo: 0.8, hi: 0.9, n: 5, mean_p: 0.85, observed: 0.8 }] },
  void_questions: [{ slug: 'FX9-gone', reason: 'premise failed' }],
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('ScorecardPanel', () => {
  it('before any resolution: 0 resolved, spread, n too small, single arm, no Brier', async () => {
    const fetchMock = stubFetch(PRE)
    mount()
    await waitFor(() => expect(screen.getByTestId('scorecard-body')).toBeInTheDocument())
    expect(String(fetchMock.mock.calls[0][0])).toContain('/v3/ledger/scorecard')
    expect(screen.getByTestId('scorecard-n-resolved')).toHaveTextContent('0 resolved')
    expect(screen.getByTestId('scorecard-no-claim')).toHaveTextContent('scores appear at 5')
    expect(screen.getByTestId('scorecard-separation')).toHaveTextContent('not the legacy calibration figures')
    expect(screen.getByTestId('scorecard-separation')).toHaveTextContent(NOTE)
    expect(screen.getByTestId('scorecard-cell-FX1-alpha-operator')).toHaveTextContent('0.85')
    expect(screen.getByTestId('scorecard-cell-FX2-beta-tower')).toHaveAttribute('title', 'no analyst number')
    expect(screen.getByTestId('scorecard-cell-FX2-beta-tower')).toHaveTextContent('—')
    expect(screen.getByText('0.118')).toBeInTheDocument()
    expect(screen.getByTestId('scorecard-single-FX8-solo')).toHaveTextContent('single arm')
    expect(screen.getByTestId('scorecard-scores-withheld')).toHaveTextContent('0 resolved; scores appear at 5')
    expect(screen.queryByTestId('scorecard-arm-operator')).toBeNull()
    expect(screen.getByTestId('scorecard-calibration-reason')).toHaveTextContent('n too small (0 < 30)')
    expect(screen.queryByText(/Brier/)).toBeNull()
    expect(screen.queryByTestId('scorecard-void')).toBeNull()
    expect(screen.getByTestId('scorecard-due-next')).toHaveTextContent('FX1-alpha')
  })

  it('below 5 resolved: outcomes and evidence show, no Brier figure anywhere', async () => {
    stubFetch({ ...POST, scores_withheld: true, header: { ...POST.header, n_resolved: 4, skill_claim: false } })
    mount()
    await waitFor(() => expect(screen.getByTestId('scorecard-body')).toBeInTheDocument())
    expect(screen.getByTestId('scorecard-outcome-FX1-alpha')).toHaveTextContent('yes')
    expect(screen.getByRole('link', { name: 'link' })).toHaveAttribute('href', 'https://example.test/result')
    expect(screen.getByTestId('scorecard-scores-withheld')).toHaveTextContent('4 resolved; scores appear at 5')
    expect(screen.queryByTestId('scorecard-brier-FX1-alpha-operator')).toBeNull()
    expect(screen.queryByText(/0\.123|Brier/)).toBeNull()
  })

  it('after resolution: outcome, Brier, evidence anchor, arm mean, bins, void list', async () => {
    stubFetch(POST)
    mount()
    await waitFor(() => expect(screen.getByTestId('scorecard-body')).toBeInTheDocument())
    expect(screen.getByTestId('scorecard-n-resolved')).toHaveTextContent('6 resolved')
    expect(screen.queryByTestId('scorecard-no-claim')).toBeNull()
    expect(screen.getByTestId('scorecard-outcome-FX1-alpha')).toHaveTextContent('yes')
    expect(screen.getByTestId('scorecard-brier-FX1-alpha-operator')).toHaveTextContent('Brier 0.040')
    const a = screen.getByRole('link', { name: 'link' })
    expect(a).toHaveAttribute('href', 'https://example.test/result')
    expect(a).toHaveAttribute('target', '_blank')
    expect(a).toHaveAttribute('rel', 'noreferrer')
    expect(screen.getByText('signal:abc123')).toBeInTheDocument()
    expect(screen.getByTestId('scorecard-arm-operator')).toHaveTextContent('0.123 (n=6)')
    expect(screen.getByTestId('scorecard-arm-tower')).toHaveTextContent('n too small')
    expect(screen.queryByTestId('scorecard-calibration-reason')).toBeNull()
    expect(screen.getByText('0.8–0.9')).toBeInTheDocument()
    expect(screen.getByTestId('scorecard-void')).toHaveTextContent('premise failed')
  })

  it('an unreadable ledger is said, not shown as empty', async () => {
    stubFetch({ available: false })
    mount()
    await waitFor(() => expect(screen.getByTestId('scorecard-unavailable')).toBeInTheDocument())
    expect(screen.queryByTestId('scorecard-body')).toBeNull()
  })

  it('a 404 reads scorecard route not deployed', async () => {
    stubFetch({ detail: 'nope' }, 404)
    mount()
    await waitFor(() => expect(screen.getByTestId('scorecard-not-deployed')).toHaveTextContent('scorecard route not deployed'))
  })
})
