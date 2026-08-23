/**
 * Component test for `system.eval_boards` — three boards, one tab each.
 *
 * Mocks the registry at the HTTP boundary and routes by URL:
 *   GET /v3/eval/desk_baselines   → the descriptive baseline board
 *   GET /v3/eval/band_trajectory  → the banded verdict trajectory
 *   GET /v3/eval/analyst_runtime  → the run-time board (NO degradation wrapper)
 *
 * What these tests exist to hold — each one is a claim the panel could
 * accidentally start making:
 *   * an unopened tab has NOT been read, so it must not have fetched;
 *   * the desk-baseline `note` is the SERVER's disclaimer that a band is not a
 *     forecast — it renders verbatim, not paraphrased and not dropped;
 *   * `available: false` (never computed) never renders as an empty board;
 *   * an `insufficient_history` row is discounted rather than reported;
 *   * a truncated trajectory warns that its last desk group may be incomplete,
 *     and the server's `total_rows` is labelled as rows scanned, not points;
 *   * a 500 from the runtime board renders as an error, never as an empty
 *     table — that board has no server-side degradation wrapper, so silence
 *     there would be a fabricated "no analyst ran";
 *   * a null avg_seconds renders as an absence and never as 0.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import EvalBoardsPanel from './EvalBoards'
import { mockErrorResponse } from '@/test/apiMocks'
import type {
  AnalystRuntimeRow,
  BandTrajectoryResponse,
  DeskBaselineBoard,
  DeskBaselineRow,
} from '@/lib/api'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 'b1',
    panel_id: 'system_eval_boards',
    descriptor_id: '(singleton)',
    descriptor_version: '0'.repeat(64),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Eval Boards',
    mode: 'personal',
    layout_slot: 'main',
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-08-01T00:00:00Z',
    retired_at: null,
  }
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

// --- fixtures --------------------------------------------------------------

/** The exact disclaimer the server ships — asserted CHARACTER FOR CHARACTER. */
const SERVER_NOTE =
  'Descriptive statistical baseline over Legba’s own substrate. NOT a forecast, ' +
  'not a prediction, and no skill is claimed for these bands.'

const DEVIATING_ROW: DeskBaselineRow = {
  desk_id: 'country_g20_br',
  metric: 'signals_per_day',
  geo: ['BR'],
  baseline_days: 28,
  n_sigma: 2.5,
  expected: 6,
  center_median: 6,
  robust_sigma: 1.2,
  band_low: 3,
  band_high: 9,
  current: 14,
  deviation: 'above',
  deviation_sigma: 3.42,
  min_current_floor: 3,
  sample_days: 28,
  active_days: 26,
  insufficient_history: false,
  spillover_current: 2,
  features: { holiday: false },
  computed_at: '2026-08-20T00:00:00Z',
}

/** Four active days out of 28 — must never be presented as a finding. */
const THIN_ROW: DeskBaselineRow = {
  ...DEVIATING_ROW,
  desk_id: 'country_watch_sd',
  metric: 'entities_per_day',
  geo: ['SD'],
  current: 30,
  deviation: 'above',
  deviation_sigma: null,
  sample_days: 6,
  active_days: 4,
  insufficient_history: true,
}

const BASELINE_BOARD: DeskBaselineBoard = {
  available: true,
  computed_at: '2026-08-20T00:00:00Z',
  note: SERVER_NOTE,
  counts: { total: 2, above: 2, below: 0, insufficient_history: 1 },
  rows: [DEVIATING_ROW, THIN_ROW],
}

const TRAJECTORY: BandTrajectoryResponse = {
  days: 14,
  server_now: '2026-08-20T00:00:00Z',
  desks: [
    {
      target_id: 'country_g20_br',
      dimensions: {
        escalation: [
          {
            ts: '2026-08-18T00:00:00Z',
            band: 'watch',
            effective_confidence: 0.62,
            faithfulness_flagged: false,
            scorecard_row_id: 'row-1',
          },
          {
            ts: '2026-08-19T00:00:00Z',
            band: 'high',
            effective_confidence: null,
            faithfulness_flagged: true,
            scorecard_row_id: 'row-2',
          },
        ],
      },
    },
  ],
  total_rows: 120,
  truncated: false,
}

const RUNTIME_ROWS: AnalystRuntimeRow[] = [
  {
    analyst_id: 'escalation',
    runs: 17,
    avg_seconds: 42.5,
    max_seconds: 91.25,
    last_run_at: '2026-08-20T00:00:00Z',
    non_success: 2,
    window_hours: 24,
  },
  {
    // Timing never recorded — the absence case.
    analyst_id: 'energy_security',
    runs: 3,
    avg_seconds: null,
    max_seconds: null,
    last_run_at: '2026-08-19T00:00:00Z',
    non_success: 0,
    window_hours: 24,
  },
]

// --- fetch routing ---------------------------------------------------------

interface Boards {
  baselines?: DeskBaselineBoard | Response
  trajectory?: BandTrajectoryResponse | Response
  runtime?: AnalystRuntimeRow[] | Response
}

function ok(body: unknown): Response {
  return { ok: true, json: async () => body } as unknown as Response
}

function mockFetch(boards: Boards = {}) {
  const fetchMock = vi.fn(async (url: string) => {
    const u = String(url)
    if (u.includes('/eval/desk_baselines')) {
      const b = boards.baselines ?? BASELINE_BOARD
      return b instanceof Object && 'ok' in b ? (b as Response) : ok(b)
    }
    if (u.includes('/eval/band_trajectory')) {
      const b = boards.trajectory ?? TRAJECTORY
      return b instanceof Object && 'ok' in b ? (b as Response) : ok(b)
    }
    if (u.includes('/eval/analyst_runtime')) {
      const b = boards.runtime ?? RUNTIME_ROWS
      return Array.isArray(b) ? ok(b) : (b as Response)
    }
    return ok({})
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function urls(f: ReturnType<typeof mockFetch>): string[] {
  return f.mock.calls.map((c) => String(c[0]))
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

function renderPanel() {
  return render(wrap(<EvalBoardsPanel registration={reg()} scope={{}} mode="personal" />))
}

// ---------------------------------------------------------------------------

describe('EvalBoardsPanel — only the active tab is read', () => {
  it('fetches ONLY the desk baselines on mount', async () => {
    const f = mockFetch()
    renderPanel()
    await screen.findByTestId('eval-boards-baselines-rows')

    expect(urls(f).some((u) => u.includes('/eval/desk_baselines'))).toBe(true)
    expect(urls(f).some((u) => u.includes('/eval/band_trajectory'))).toBe(false)
    expect(urls(f).some((u) => u.includes('/eval/analyst_runtime'))).toBe(false)
  })

  it('loads the band trajectory only once its tab is activated', async () => {
    const f = mockFetch()
    renderPanel()
    await screen.findByTestId('eval-boards-baselines-rows')

    fireEvent.click(screen.getByTestId('eval-boards-tab-trajectory'))
    await waitFor(() => {
      expect(urls(f).some((u) => u.includes('/eval/band_trajectory'))).toBe(true)
    })
    expect(await screen.findByTestId('eval-boards-trajectory-desks')).toBeInTheDocument()
    // Still untouched — the third tab has not been opened.
    expect(urls(f).some((u) => u.includes('/eval/analyst_runtime'))).toBe(false)
  })

  it('loads the analyst runtime only once its tab is activated', async () => {
    const f = mockFetch()
    renderPanel()
    await screen.findByTestId('eval-boards-baselines-rows')

    fireEvent.click(screen.getByTestId('eval-boards-tab-runtime'))
    expect(await screen.findByTestId('eval-boards-runtime-table')).toBeInTheDocument()
    expect(urls(f).some((u) => u.includes('/eval/analyst_runtime'))).toBe(true)
  })
})

describe('EvalBoardsPanel — desk baselines', () => {
  it('renders the server note VERBATIM, so a band cannot be read as a forecast', async () => {
    mockFetch()
    renderPanel()
    const note = await screen.findByTestId('eval-boards-baselines-note')
    // Character for character — no paraphrase, no truncation.
    expect(note.textContent).toContain(SERVER_NOTE)
  })

  it('lists rows in the server order and states each deviation with its sigma', async () => {
    mockFetch()
    renderPanel()
    const rows = await screen.findByTestId('eval-boards-baselines-rows')
    expect(rows.children).toHaveLength(2)
    expect(
      screen.getByTestId('eval-boards-baseline-deviation-country_g20_br:signals_per_day'),
    ).toHaveTextContent('above band (3.42σ)')
  })

  it('says the sigma is absent rather than showing a 0σ deviation', async () => {
    mockFetch()
    renderPanel()
    const pill = await screen.findByTestId(
      'eval-boards-baseline-deviation-country_watch_sd:entities_per_day',
    )
    expect(pill).toHaveTextContent('σ not computed')
    expect(pill.textContent).not.toContain('0.00σ')
  })

  it('visibly discounts an insufficient-history row instead of reporting it', async () => {
    mockFetch()
    renderPanel()
    const thin = await screen.findByTestId(
      'eval-boards-baseline-thin-country_watch_sd:entities_per_day',
    )
    expect(thin).toHaveTextContent(/insufficient history/i)
    expect(thin).toHaveTextContent(/not a finding/i)
    expect(
      screen.getByTestId('eval-boards-baseline-row-country_watch_sd:entities_per_day').className,
    ).toContain('opacity-60')
  })

  it('expands a row into its wire fields', async () => {
    mockFetch()
    renderPanel()
    await screen.findByTestId('eval-boards-baselines-rows')
    fireEvent.click(
      screen.getByTestId('eval-boards-baseline-toggle-country_g20_br:signals_per_day'),
    )
    const row = screen.getByTestId('eval-boards-baseline-row-country_g20_br:signals_per_day')
    expect(row.textContent).toContain('min current floor')
    expect(row.textContent).toContain('26/28 active days over a 28d baseline')
  })

  it('reads available:false as NOT COMPUTED — distinct from a computed empty board', async () => {
    mockFetch({ baselines: { ...BASELINE_BOARD, available: false, rows: [], counts: {} } })
    renderPanel()
    const block = await screen.findByTestId('eval-boards-baselines-unavailable')
    expect(block).toHaveTextContent(/NOT COMPUTED/)
    expect(screen.queryByTestId('eval-boards-baselines-empty')).not.toBeInTheDocument()
    expect(screen.queryByTestId('eval-boards-baselines-rows')).not.toBeInTheDocument()
    // The disclaimer still renders — an absent board is not an excuse to drop it.
    expect(screen.getByTestId('eval-boards-baselines-note').textContent).toContain(SERVER_NOTE)
  })

  it('reads a computed board with no rows as a measured emptiness', async () => {
    mockFetch({
      baselines: { ...BASELINE_BOARD, rows: [], counts: { total: 0 } },
    })
    renderPanel()
    const empty = await screen.findByTestId('eval-boards-baselines-empty')
    expect(empty).toHaveTextContent(/measured emptiness/)
    expect(screen.queryByTestId('eval-boards-baselines-unavailable')).not.toBeInTheDocument()
  })

  it('surfaces a baseline read failure instead of an empty board', async () => {
    mockFetch({ baselines: mockErrorResponse(500, { detail: 'pg down' }) })
    renderPanel()
    const err = await screen.findByTestId('eval-boards-baselines-error')
    expect(err).toHaveTextContent(/pg down/)
    expect(err).toHaveTextContent(/not an empty board/)
    expect(screen.queryByTestId('eval-boards-baselines-rows')).not.toBeInTheDocument()
  })

  it('re-queries when the deviating-only filter is toggled', async () => {
    const f = mockFetch()
    renderPanel()
    await screen.findByTestId('eval-boards-baselines-rows')
    fireEvent.click(screen.getByTestId('eval-boards-deviating-toggle'))
    await waitFor(() => {
      expect(urls(f).some((u) => u.includes('deviating_only=true'))).toBe(true)
    })
  })
})

describe('EvalBoardsPanel — band trajectory', () => {
  async function openTrajectory(boards: Boards = {}) {
    const f = mockFetch(boards)
    renderPanel()
    await screen.findByTestId('eval-boards-baselines-rows')
    fireEvent.click(screen.getByTestId('eval-boards-tab-trajectory'))
    return f
  }

  it('renders a banded strip per desk-dimension with its own n', async () => {
    await openTrajectory()
    const strip = await screen.findByTestId(
      'eval-boards-trajectory-strip-country_g20_br-escalation',
    )
    expect(strip.children).toHaveLength(2)
    expect(
      screen.getByTestId('eval-boards-trajectory-series-country_g20_br-escalation'),
    ).toHaveTextContent('2 points · 1 flagged · confidence on 1/2')
  })

  it('marks a faithfulness-flagged point and renders a null confidence as an absence', async () => {
    await openTrajectory()
    await screen.findByTestId('eval-boards-trajectory-desks')
    expect(screen.getByTestId('eval-boards-trajectory-flagged-row-2')).toBeInTheDocument()
    expect(screen.queryByTestId('eval-boards-trajectory-flagged-row-1')).not.toBeInTheDocument()

    const flagged = screen.getByTestId('eval-boards-trajectory-point-row-2')
    expect(flagged.getAttribute('title')).toContain('effective confidence not recorded')
    expect(flagged.textContent).not.toContain('0.00')
  })

  it('labels total_rows as SCORECARD ROWS scanned, never as points', async () => {
    await openTrajectory()
    const summary = await screen.findByTestId('eval-boards-trajectory-summary')
    expect(summary).toHaveTextContent('2 banded points')
    expect(summary).toHaveTextContent('120 scorecard rows scanned (rows, not points)')
  })

  it('warns prominently that a TRUNCATED scan may have cut the last desk group', async () => {
    await openTrajectory({ trajectory: { ...TRAJECTORY, truncated: true, total_rows: 500 } })
    const warn = await screen.findByTestId('eval-boards-trajectory-truncated')
    expect(warn).toHaveTextContent(/TRUNCATED/)
    expect(warn).toHaveTextContent(/LAST DESK GROUP BELOW MAY BE INCOMPLETE/i)
  })

  it('does not warn when the scan completed', async () => {
    await openTrajectory()
    await screen.findByTestId('eval-boards-trajectory-desks')
    expect(screen.queryByTestId('eval-boards-trajectory-truncated')).not.toBeInTheDocument()
  })

  it('keeps the days control inside the server-validated [1, 90] range', async () => {
    const f = await openTrajectory()
    const select = (await screen.findByTestId('eval-boards-days')) as HTMLSelectElement
    const values = Array.from(select.options).map((o) => Number(o.value))
    expect(values.every((v) => v >= 1 && v <= 90)).toBe(true)

    fireEvent.change(select, { target: { value: '30' } })
    await waitFor(() => {
      expect(urls(f).some((u) => u.includes('days=30'))).toBe(true)
    })
  })

  it('surfaces a 400 honestly, saying the server rejected rather than clamped', async () => {
    await openTrajectory({
      trajectory: mockErrorResponse(400, { detail: 'days must be between 1 and 90' }),
    })
    const err = await screen.findByTestId('eval-boards-trajectory-error')
    expect(err).toHaveTextContent(/REJECTED/)
    expect(err).toHaveTextContent(/days must be between 1 and 90/)
    expect(err).toHaveTextContent(/validates rather than clamps/)
  })

  it('reports an empty scan as a measured emptiness, not a failure', async () => {
    await openTrajectory({ trajectory: { ...TRAJECTORY, desks: [], total_rows: 0 } })
    expect(await screen.findByTestId('eval-boards-trajectory-empty')).toHaveTextContent(
      /measured emptiness/,
    )
  })
})

describe('EvalBoardsPanel — analyst runtime', () => {
  async function openRuntime(boards: Boards = {}) {
    const f = mockFetch(boards)
    renderPanel()
    await screen.findByTestId('eval-boards-baselines-rows')
    fireEvent.click(screen.getByTestId('eval-boards-tab-runtime'))
    return f
  }

  it('shows the echoed window ONCE, not per row', async () => {
    await openRuntime()
    expect(await screen.findByTestId('eval-boards-runtime-window')).toHaveTextContent(
      '24h window',
    )
    expect(screen.getAllByTestId('eval-boards-runtime-window')).toHaveLength(1)
    expect(
      screen.getByTestId('eval-boards-runtime-row-escalation').textContent,
    ).not.toContain('24h')
  })

  it('shows non-success against its runs denominator', async () => {
    await openRuntime()
    expect(
      await screen.findByTestId('eval-boards-runtime-nonsuccess-escalation'),
    ).toHaveTextContent('12% (2/17)')
    expect(screen.getByTestId('eval-boards-runtime-totals')).toHaveTextContent('(2/20)')
  })

  it('states the mean runtime with the n it was taken over', async () => {
    await openRuntime()
    expect(await screen.findByTestId('eval-boards-runtime-avg-escalation')).toHaveTextContent(
      '42.5s over 17 runs',
    )
  })

  it('renders a null avg/max as an absence and NEVER as 0', async () => {
    await openRuntime()
    const avg = await screen.findByTestId('eval-boards-runtime-avg-energy_security')
    expect(avg).toHaveTextContent('not recorded')
    expect(avg.textContent).not.toMatch(/0/)
    expect(screen.getByTestId('eval-boards-runtime-max-energy_security')).toHaveTextContent(
      'not recorded',
    )
  })

  it('surfaces a 500 as an ERROR and renders no table at all', async () => {
    await openRuntime({ runtime: mockErrorResponse(500, { detail: 'relation does not exist' }) })
    const err = await screen.findByTestId('eval-boards-runtime-error')
    expect(err).toHaveTextContent(/relation does not exist/)
    expect(err).toHaveTextContent(/no server-side degradation wrapper/)
    expect(screen.queryByTestId('eval-boards-runtime-table')).not.toBeInTheDocument()
    expect(screen.queryByTestId('eval-boards-runtime-empty')).not.toBeInTheDocument()
  })

  it('distinguishes a successful read that found no runs from a failed one', async () => {
    await openRuntime({ runtime: [] })
    expect(await screen.findByTestId('eval-boards-runtime-empty')).toHaveTextContent(
      /read successfully and reported none/,
    )
    expect(screen.queryByTestId('eval-boards-runtime-error')).not.toBeInTheDocument()
  })

  it('re-queries when the window control changes', async () => {
    const f = await openRuntime()
    await screen.findByTestId('eval-boards-runtime-table')
    fireEvent.change(screen.getByTestId('eval-boards-window'), { target: { value: '72' } })
    await waitFor(() => {
      expect(urls(f).some((u) => u.includes('window_hours=72'))).toBe(true)
    })
  })
})
