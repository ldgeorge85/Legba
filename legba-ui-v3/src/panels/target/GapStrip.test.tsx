/**
 * Component test for the 7b-ii desk gap strip.
 *
 * A nine-unit fixture drives all four states at once (current / insufficient /
 * stale / none), pins the test ids the brief names (`desk-gap-strip`,
 * `desk-gap-cell-<unit id>`), and checks the click-to-Inspector wiring goes
 * through `selectRow` — never a bare window event.
 *
 * Timestamps are built relative to `Date.now()` at test-run time (1h ago /
 * 40h ago) rather than a faked system clock — `waitFor`/react-query's own
 * timers stay real, and the offsets are far enough either side of the unit's
 * 12h × 1.5 grace threshold (18h) that the exact run time never matters.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

const selectRow = vi.fn()
vi.mock('@/state/selection', () => ({ selectRow: (...args: unknown[]) => selectRow(...args) }))

import { GapStrip } from './GapStrip'
import { GAP_STRIP_UNITS } from '@/lib/gapStripModel'

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

function hoursAgo(h: number): string {
  return new Date(Date.now() - h * 60 * 60 * 1000).toISOString()
}

const FRESH = hoursAgo(1) // well inside any unit's 18h grace threshold
const OLD = hoursAgo(40) // well past it

// One fixture that exercises all four states across the nine units:
//   leadership_transition — current (fresh, real band)
//   energy_security       — stale (old, real band)
//   escalation            — insufficient (fresh, but excluded)
//   everyone else         — none (no finding at all)
const FINDINGS = {
  data: [
    { id: 'f-lt', analyst_id: 'leadership_transition', produced_at: FRESH, severity: 'high' },
    { id: 'f-es', analyst_id: 'energy_security', produced_at: OLD, severity: 'medium' },
    { id: 'f-esc', analyst_id: 'escalation', produced_at: FRESH, severity: 'low' },
  ],
}

const SCORECARD = [
  {
    target_id: 'israel',
    id: 'sc1',
    produced_at: FRESH,
    generated_at: FRESH,
    floors: {},
    dimensions: {
      leadership_transition: {
        band: 'high',
        basis: ['f-lt'],
        severity_tag: 'high',
        effective_confidence: 0.8,
        confidence: 0.8,
        critic_score: 0.87,
        damped: false,
        reason: '',
        produced_at: FRESH,
        eval: { faithfulness: 0.87, correctness_vs_reference: null, n_labeled: 2, faithfulness_flagged: false },
      },
      energy_security: {
        band: 'medium',
        basis: ['f-es'],
        severity_tag: 'medium',
        effective_confidence: 0.7,
        confidence: 0.7,
        critic_score: 0.75,
        damped: false,
        reason: '',
        produced_at: OLD,
        eval: { faithfulness: 0.75, correctness_vs_reference: null, n_labeled: 1, faithfulness_flagged: false },
      },
      escalation: {
        band: 'insufficient-evidence',
        basis: [],
        severity_tag: null,
        effective_confidence: null,
        confidence: null,
        critic_score: 0.31,
        damped: false,
        reason: 'low-faithfulness',
        produced_at: null,
        eval: { faithfulness: 0.31, correctness_vs_reference: null, n_labeled: 1, faithfulness_flagged: true },
      },
    },
    composition: { present: false, basis: [] },
  },
]

function routedFetch() {
  return vi.fn(async (url: string) => {
    if (url.includes('country_scorecard')) {
      return { ok: true, json: async () => SCORECARD }
    }
    if (url.includes('analyst_id_in')) {
      return { ok: true, json: async () => FINDINGS }
    }
    return { ok: true, json: async () => ({ data: [] }) }
  })
}

beforeEach(() => {
  vi.restoreAllMocks()
  selectRow.mockReset()
  localStorage.clear()
})

describe('GapStrip', () => {
  it('renders one cell per bounded unit', async () => {
    vi.stubGlobal('fetch', routedFetch())
    render(wrap(<GapStrip targetId="israel" />))
    const strip = await screen.findByTestId('desk-gap-strip')
    expect(strip).toBeInTheDocument()
    for (const unit of GAP_STRIP_UNITS) {
      expect(await screen.findByTestId(`desk-gap-cell-${unit.id}`)).toBeInTheDocument()
    }
  })

  it('colours current / stale / insufficient / none correctly from one fixture', async () => {
    vi.stubGlobal('fetch', routedFetch())
    render(wrap(<GapStrip targetId="israel" />))

    await waitFor(() =>
      expect(screen.getByTestId('desk-gap-cell-leadership_transition')).toHaveAttribute(
        'data-state',
        'current',
      ),
    )
    expect(screen.getByTestId('desk-gap-cell-energy_security')).toHaveAttribute('data-state', 'stale')
    expect(screen.getByTestId('desk-gap-cell-escalation')).toHaveAttribute('data-state', 'insufficient')
    expect(screen.getByTestId('desk-gap-cell-internal_stability')).toHaveAttribute('data-state', 'none')
    expect(screen.getByTestId('desk-gap-cell-disruption_status')).toHaveAttribute('data-state', 'none')
  })

  it('clicking a cell with a read selects it into the Inspector', async () => {
    vi.stubGlobal('fetch', routedFetch())
    render(wrap(<GapStrip targetId="israel" />))

    const cell = await screen.findByTestId('desk-gap-cell-leadership_transition')
    await waitFor(() => expect(cell).toHaveAttribute('data-state', 'current'))
    fireEvent.click(cell)
    expect(selectRow).toHaveBeenCalledWith(
      'finding',
      'f-lt',
      'Leadership transition',
      expect.objectContaining({ origin: 'desk-gap-strip' }),
    )
  })

  // 7b/k5 — the "Why absent" drill. Before it, a `none` cell was inert: the
  // one state with no record to open was also the one a reader could not
  // interrogate at all, which is the blank this lane exists to abolish.
  it('clicking a `none` cell drills into the typed absence, not a record', async () => {
    vi.stubGlobal('fetch', routedFetch())
    render(wrap(<GapStrip targetId="israel" />))

    const cell = await screen.findByTestId('desk-gap-cell-internal_stability')
    await waitFor(() => expect(cell).toHaveAttribute('data-state', 'none'))
    expect(cell).not.toBeDisabled()
    fireEvent.click(cell)
    expect(selectRow).toHaveBeenCalledWith(
      'absence',
      'israel|not_collected|internal_stability',
      'Internal stability — why absent',
      expect.objectContaining({ origin: 'desk-gap-strip' }),
    )
  })

  it('a stale cell drills into the absence; an insufficient one into the floor', async () => {
    vi.stubGlobal('fetch', routedFetch())
    render(wrap(<GapStrip targetId="israel" />))

    const stale = await screen.findByTestId('desk-gap-cell-energy_security')
    await waitFor(() => expect(stale).toHaveAttribute('data-state', 'stale'))
    fireEvent.click(stale)
    expect(selectRow).toHaveBeenLastCalledWith(
      'absence',
      'israel|source_stale|energy_security',
      'Energy security — why absent',
      expect.objectContaining({ origin: 'desk-gap-strip' }),
    )

    const insufficient = screen.getByTestId('desk-gap-cell-escalation')
    expect(insufficient).toHaveAttribute('data-state', 'insufficient')
    fireEvent.click(insufficient)
    expect(selectRow).toHaveBeenLastCalledWith(
      'absence',
      'israel|below_floor|escalation',
      'Escalation — why absent',
      expect.objectContaining({ origin: 'desk-gap-strip' }),
    )
  })

  it('renders nothing without a target id', () => {
    vi.stubGlobal('fetch', routedFetch())
    const { container } = render(wrap(<GapStrip targetId={undefined} />))
    expect(container.textContent).toBe('')
  })
})
