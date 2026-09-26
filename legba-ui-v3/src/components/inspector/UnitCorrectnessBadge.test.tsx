/**
 * Component tests for UnitCorrectnessBadge (G2).
 *
 * The badge's whole job is to be honest about three states, so all three are
 * pinned here:
 *
 *   * a unit WITH a number renders the server's badge string VERBATIM — the
 *     "no invented number" contract lives on the server, and a component that
 *     reformats it has quietly taken that contract back;
 *   * a unit with NO number and a CURRENT reference renders nothing at all —
 *     not an empty chip, not a dash, and above all not a 100%;
 *   * a unit with no number because the country has no reference says exactly
 *     that.
 *
 * Plus the drawer: it fetches the ledger only when opened, and leads with the
 * claim the reference contradicted.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'

const apiGet = vi.fn()
vi.mock('@/lib/api', () => ({
  apiGet: (path: string) => apiGet(path),
}))

import { UnitCorrectnessBadge } from './UnitCorrectnessBadge'
import { resetUnitCorrectnessCache } from '@/lib/unitCorrectnessModel'

const REF_CURRENT = {
  state: 'current',
  id: 'ref-1',
  sha256: '2dc73bf3',
  builder: 'opus-web-lane',
  window_start: '2026-09-02T00:00:00Z',
  window_end: '2026-09-30T00:00:00Z',
  span_verified_rate: 0.95,
  thin_dimensions: ['proliferation_watch'],
  age_days: 0,
  age_source: 'stored',
}

const ROW = {
  id: 'uc-1',
  analyst_id: 'internal_stability',
  target_id: 'country_watch_il',
  head_id: 'h1',
  as_of: '2026-09-16T19:30:00Z',
  rubric_sha: '0a011222',
  grain: 'desk',
  n_claims: 6,
  n_contains: 2,
  n_contradicts: 1,
  n_silent: 3,
  n_split: 0,
  n_unparseable: 0,
  n_single_family: 0,
  n_decided: 3,
  correctness_share: 2 / 3,
  coverage_share: 0.5,
  single_family: false,
  badge: 'correctness 66.7% (2/3) · coverage 50.0% (n=6) · as of 2026-09-16',
  families: {},
  cost_usd: 0,
  created_at: '2026-09-16T19:35:00Z',
  reference: REF_CURRENT,
}

const CLAIMS = [
  {
    id: 'c1',
    claim_id: 'DR-aaaaaaaa',
    grain: 'desk',
    claim_text: 'Gas exports resumed on 12 September.',
    label_by_family: { F0: 'contains', F2: 'contains', F3: 'silent' },
    adjudicated: 'contains',
    n_families: 3,
    single_family: false,
    spans: {},
    created_at: '2026-09-16T19:35:00Z',
  },
  {
    id: 'c2',
    claim_id: 'DR-bbbbbbbb',
    grain: 'desk',
    claim_text: 'A deadlock over Knesset list submissions signals splintering.',
    label_by_family: { F0: 'contradicts', F2: 'silent', F3: 'contradicts' },
    adjudicated: 'contradicts',
    n_families: 3,
    single_family: false,
    spans: { F0: { decisive_span: '38 lists were filed by the deadline' } },
    created_at: '2026-09-16T19:35:00Z',
  },
]

function page(over: Record<string, unknown> = {}) {
  return {
    target_id: 'country_watch_il',
    reference: REF_CURRENT,
    data: [ROW],
    next_cursor: null,
    ...over,
  }
}

beforeEach(() => {
  apiGet.mockReset()
  resetUnitCorrectnessCache()
})

describe('UnitCorrectnessBadge', () => {
  it('renders the server-composed badge string verbatim', async () => {
    apiGet.mockResolvedValue(page())
    render(
      <UnitCorrectnessBadge
        analystId="internal_stability"
        targetId="country_watch_il"
      />,
    )
    const badge = await screen.findByTestId('unit-correctness-badge')
    expect(badge).toHaveTextContent(
      'correctness 66.7% (2/3) · coverage 50.0% (n=6) · as of 2026-09-16',
    )
    expect(apiGet).toHaveBeenCalledWith(
      '/units/country_watch_il/correctness',
    )
  })

  it('renders NOTHING for an ungraded unit under a current reference', async () => {
    apiGet.mockResolvedValue(page())
    const { container } = render(
      <UnitCorrectnessBadge
        analystId="military_posture"
        targetId="country_watch_il"
      />,
    )
    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    expect(screen.queryByTestId('unit-correctness-badge')).toBeNull()
    expect(screen.queryByTestId('unit-correctness-absent')).toBeNull()
    expect(container.textContent).toBe('')
  })

  it('says "no reference" when the country has none', async () => {
    apiGet.mockResolvedValue(
      page({ data: [], reference: { ...REF_CURRENT, state: 'none', id: null } }),
    )
    render(
      <UnitCorrectnessBadge analystId="escalation" targetId="country_watch_il" />,
    )
    expect(
      await screen.findByTestId('unit-correctness-absent'),
    ).toHaveTextContent('no reference')
  })

  it('says "reference stale" when the country\'s reference has lapsed', async () => {
    apiGet.mockResolvedValue(
      page({ data: [], reference: { ...REF_CURRENT, state: 'stale', age_days: 9 } }),
    )
    render(
      <UnitCorrectnessBadge analystId="escalation" targetId="country_watch_il" />,
    )
    expect(
      await screen.findByTestId('unit-correctness-absent'),
    ).toHaveTextContent('reference stale')
  })

  it('never fetches without BOTH halves of the measurement key', async () => {
    const { container } = render(
      <UnitCorrectnessBadge analystId="escalation" targetId={null} />,
    )
    expect(apiGet).not.toHaveBeenCalled()
    expect(container.textContent).toBe('')
  })

  it('renders nothing when the fetch fails', async () => {
    apiGet.mockRejectedValue(new Error('boom'))
    const { container } = render(
      <UnitCorrectnessBadge
        analystId="internal_stability"
        targetId="country_watch_il"
      />,
    )
    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })

  it('opens the claims ledger on demand, contradictions first', async () => {
    apiGet.mockImplementation((path: string) =>
      Promise.resolve(
        path.includes('claims=1')
          ? page({ data: [{ ...ROW, claims: CLAIMS }] })
          : page(),
      ),
    )
    render(
      <UnitCorrectnessBadge
        analystId="internal_stability"
        targetId="country_watch_il"
      />,
    )
    const badge = await screen.findByTestId('unit-correctness-badge')
    // The ledger is NOT pulled until the reader asks for it.
    expect(badge).toHaveAttribute('aria-expanded', 'false')
    expect(apiGet).toHaveBeenCalledTimes(1)

    fireEvent.click(badge)

    const rows = await screen.findAllByTestId('correctness-claim-row')
    expect(rows).toHaveLength(2)
    expect(rows[0]).toHaveAttribute('data-adjudicated', 'contradicts')
    expect(rows[0]).toHaveTextContent('F2: silent')
    expect(screen.getByTestId('correctness-decisive-span')).toHaveTextContent(
      '38 lists were filed by the deadline',
    )
    expect(screen.getByTestId('correctness-thin-dimensions')).toHaveTextContent(
      'proliferation_watch',
    )
    expect(apiGet).toHaveBeenCalledWith(
      '/units/country_watch_il/correctness?claims=1',
    )
  })
})
