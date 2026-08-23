/**
 * Component test for the `system.production_gauge` panel.
 *
 * Mocks the registry at the HTTP boundary and routes by URL, honouring the
 * server's own filter semantics: `deficits_only=true` narrows `loops` but
 * leaves `totals` EXACTLY as it was, because the server computes totals over
 * the full read before any filter.
 *
 * What these tests exist to hold — one per clause of the honesty contract:
 *   * the loops render, grouped, with the bricks visible as bricks;
 *   * `measured: false` renders as a LOUD failed read and never as an
 *     all-clear — no totals strip, no green tiles;
 *   * a genuinely quiet engine (`measured: true`, zero loops) reads DIFFERENTLY
 *     from that failure;
 *   * an `ungauged` row shows its `quiet_reason` and never a 0.0 ratio;
 *   * the paging row is unmistakable and the threshold quoted is the payload's
 *     `alert_min_severity`, not a constant in the panel;
 *   * filtering narrows the rows and leaves the whole-engine totals alone.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import ProductionGaugePanel from './ProductionGauge'
import { mockErrorResponse } from '@/test/apiMocks'
import type { ProductionGaugeResponse, ProductionGaugeRow } from '@/lib/api'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 'pg1',
    panel_id: 'system_production_gauge',
    descriptor_id: '(singleton)',
    descriptor_version: '0'.repeat(64),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Production Gauge',
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

/** A production loop that failed its own bar hard enough to page. */
const PAGING: ProductionGaugeRow = {
  loop_class: 'analyst_cadence',
  loop_id: 'war_beat',
  label: 'War beat — declared cadence',
  state: 'deficit',
  severity: 'critical',
  ratio: 6.2,
  expected: 'a run every 30m (its own cron)',
  actual: 'last run 3h 6m ago',
  quiet_reason: null,
  last_production_at: '2026-08-20T21:00:00Z',
  pages: true,
  evidence: { cron: '*/30 * * * *', bar_minutes: 30, observed_gap_minutes: 186 },
}

/** A deficit that is real but BELOW the alert floor — it must not look paged. */
const SUB_FLOOR: ProductionGaugeRow = {
  loop_class: 'llm_latency',
  loop_id: 'core_plane',
  label: 'Core plane p95 latency vs client timeout',
  state: 'deficit',
  severity: 'low',
  ratio: 1.1,
  expected: 'p95 under the 120s client timeout',
  actual: 'p95 132s over 412 calls',
  quiet_reason: null,
  last_production_at: '2026-08-21T00:00:00Z',
  pages: false,
  evidence: { p95_seconds: 132.0, timeout_seconds: 120, calls: 412 },
}

/** Quiet by design — no expectation exists, so there is NO ratio. */
const UNGAUGED: ProductionGaugeRow = {
  loop_class: 'source_production',
  loop_id: 'src_ru_milblog_07',
  label: 'RU milblog aggregator',
  state: 'ungauged',
  severity: 'info',
  ratio: null,
  expected: '',
  actual: 'no signals in the window',
  quiet_reason: 'insufficient_history',
  last_production_at: null,
  pages: false,
  evidence: { signals_in_window: 0, window_days: 14 },
}

/** Ungauged because a QUERY FAILED — not the same thing as paused-by-design. */
const UNGAUGED_FAILED: ProductionGaugeRow = {
  loop_class: 'desk_head_staleness',
  loop_id: 'desk_heads',
  label: 'Desk-head age at read time',
  state: 'ungauged',
  severity: 'info',
  ratio: null,
  expected: '',
  actual: 'not computed',
  quiet_reason: 'staleness_query_failed',
  last_production_at: null,
  pages: false,
  evidence: {},
}

const HEALTHY: ProductionGaugeRow = {
  loop_class: 'judge_availability',
  loop_id: 'llm_judge',
  label: 'LLM judge availability',
  state: 'ok',
  severity: 'info',
  ratio: 0.02,
  expected: 'under 5% of critiques falling back to the heuristic judge',
  actual: '2% fallback over 918 critiques',
  quiet_reason: null,
  last_production_at: '2026-08-21T00:30:00Z',
  pages: false,
  evidence: { critiques: 918, fallbacks: 18 },
}

const ALL_ROWS = [HEALTHY, UNGAUGED, PAGING, SUB_FLOOR, UNGAUGED_FAILED]

/**
 * Whole-engine totals — deliberately much larger than `ALL_ROWS`, exactly as
 * the live engine's are relative to any filtered page.
 */
const TOTALS = {
  loops: 268,
  gauged: 68,
  ok: 59,
  deficit: 9,
  ungauged: 200,
  paging: 3,
  by_severity: { critical: 1, high: 2, low: 6 },
  by_class: {
    analyst_cadence: { gauged: 40, ok: 38, deficit: 2, ungauged: 10 },
    source_production: { gauged: 20, ok: 17, deficit: 3, ungauged: 188 },
    judge_availability: { gauged: 1, ok: 1, deficit: 0, ungauged: 0 },
    llm_latency: { gauged: 5, ok: 3, deficit: 2, ungauged: 1 },
    desk_head_staleness: { gauged: 2, ok: 0, deficit: 2, ungauged: 1 },
  },
}

function gauge(patch: Partial<ProductionGaugeResponse> = {}): ProductionGaugeResponse {
  return {
    generated_at: '2026-08-21T01:00:00Z',
    window_days: 14,
    alert_min_severity: 'medium',
    totals: TOTALS,
    loops: ALL_ROWS,
    measured: true,
    ...patch,
  }
}

let calls: string[] = []

/** Routes by URL and applies the SERVER's filter semantics: rows narrow,
 *  totals do not. */
function mockFetch(res: ProductionGaugeResponse = gauge()) {
  const fetchMock = vi.fn(async (url: string) => {
    const u = String(url)
    calls.push(u)
    if (u.includes('/v3/system/production-gauge')) {
      let loops = res.loops
      if (u.includes('paging_only=true')) loops = loops.filter((r) => r.pages)
      else if (u.includes('deficits_only=true')) loops = loops.filter((r) => r.state === 'deficit')
      const cls = /loop_class=([^&]+)/.exec(u)?.[1]
      if (cls) loops = loops.filter((r) => r.loop_class === cls)
      return { ok: true, json: async () => ({ ...res, loops }) } as unknown as Response
    }
    return { ok: true, json: async () => ({}) } as unknown as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  calls = []
})

function renderPanel() {
  return render(wrap(<ProductionGaugePanel registration={reg()} scope={{}} mode="personal" />))
}

// ---------------------------------------------------------------------------

describe('ProductionGaugePanel — the loops', () => {
  it('renders every returned loop off GET /v3/system/production-gauge', async () => {
    mockFetch()
    renderPanel()
    await waitFor(() =>
      expect(screen.getByTestId('production-gauge-row-analyst_cadence:war_beat')).toBeInTheDocument(),
    )
    expect(
      screen.getByTestId('production-gauge-row-source_production:src_ru_milblog_07'),
    ).toBeInTheDocument()
    expect(screen.getByTestId('production-gauge-row-llm_latency:core_plane')).toBeInTheDocument()
    expect(
      screen.getByTestId('production-gauge-row-judge_availability:llm_judge'),
    ).toBeInTheDocument()
    expect(
      screen.getByTestId('production-gauge-row-desk_head_staleness:desk_heads'),
    ).toBeInTheDocument()
  })

  it('groups the bricks apart from the ordinary production loops', async () => {
    mockFetch()
    renderPanel()
    await screen.findByTestId('production-gauge-group-production')
    expect(screen.getByTestId('production-gauge-group-integrity')).toBeInTheDocument()
    expect(screen.getByTestId('production-gauge-group-metering')).toBeInTheDocument()
    expect(screen.getByTestId('production-gauge-group-staleness')).toBeInTheDocument()

    // The paging analyst is a PRODUCTION loop; the latency brick is METERING.
    expect(
      screen
        .getByTestId('production-gauge-group-production')
        .querySelector('[data-testid="production-gauge-row-analyst_cadence:war_beat"]'),
    ).not.toBeNull()
    expect(
      screen
        .getByTestId('production-gauge-group-metering')
        .querySelector('[data-testid="production-gauge-row-llm_latency:core_plane"]'),
    ).not.toBeNull()
  })

  it('shows each group its WHOLE-ENGINE counts, not the counts of the rows shown', async () => {
    mockFetch()
    renderPanel()
    // Two production rows are on screen; the engine has 60 production loops
    // (40+10 analyst_cadence, 20+188 source_production → 258 by by_class).
    const counts = await screen.findByTestId('production-gauge-group-counts-production')
    expect(counts).toHaveTextContent('60 gauged')
    expect(counts).toHaveTextContent('198 ungauged')
  })

  it('opens a row to its evidence and its own stated expectation', async () => {
    mockFetch()
    renderPanel()
    await screen.findByTestId('production-gauge-row-analyst_cadence:war_beat')
    fireEvent.click(screen.getByTestId('production-gauge-toggle-analyst_cadence:war_beat'))

    expect(
      await screen.findByTestId('production-gauge-expected-analyst_cadence:war_beat'),
    ).toHaveTextContent('a run every 30m (its own cron)')
    expect(screen.getByTestId('production-gauge-actual-analyst_cadence:war_beat')).toHaveTextContent(
      'last run 3h 6m ago',
    )
    const ev = screen.getByTestId('production-gauge-evidence-analyst_cadence:war_beat')
    expect(ev.textContent).toContain('observed_gap_minutes')
    expect(ev.textContent).toContain('186')
  })
})

describe('ProductionGaugePanel — measured:false is a FAILED read, not an all-clear', () => {
  const DEGRADED = gauge({
    measured: false,
    generated_at: null,
    window_days: 0,
    loops: [],
    totals: {
      loops: 0,
      gauged: 0,
      ok: 0,
      deficit: 0,
      ungauged: 0,
      paging: 0,
      by_severity: {},
      by_class: {},
    },
  })

  it('renders the loud degraded banner', async () => {
    mockFetch(DEGRADED)
    renderPanel()
    const banner = await screen.findByTestId('production-gauge-degraded')
    expect(banner).toHaveTextContent(/READ FAILED/)
    expect(banner).toHaveTextContent(/no deficit has been ruled out/i)
  })

  it('renders NO totals strip and no zero-deficit reassurance', async () => {
    mockFetch(DEGRADED)
    renderPanel()
    await screen.findByTestId('production-gauge-degraded')
    expect(screen.queryByTestId('production-gauge-totals')).not.toBeInTheDocument()
    expect(screen.queryByTestId('production-gauge-total-ok')).not.toBeInTheDocument()
    expect(screen.queryByTestId('production-gauge-total-deficit')).not.toBeInTheDocument()
    expect(screen.queryByTestId('production-gauge-paging-note')).not.toBeInTheDocument()
    expect(screen.queryByTestId('production-gauge-quiet')).not.toBeInTheDocument()
  })

  it('reads DIFFERENTLY from a genuinely quiet engine', async () => {
    mockFetch(gauge({ loops: [], totals: { ...TOTALS, loops: 0 } }))
    renderPanel()
    const quiet = await screen.findByTestId('production-gauge-quiet')
    expect(quiet).toHaveTextContent(/read succeeded/i)
    expect(quiet).not.toHaveTextContent(/READ FAILED/)
    expect(screen.queryByTestId('production-gauge-degraded')).not.toBeInTheDocument()
  })

  it('surfaces a transport failure instead of an empty gauge', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => mockErrorResponse(500, { detail: 'pg pool exhausted' })),
    )
    renderPanel()
    const err = await screen.findByTestId('production-gauge-error')
    expect(err).toHaveTextContent(/pg pool exhausted/)
    expect(err).toHaveTextContent(/not an all-clear/i)
    expect(screen.queryByTestId('production-gauge-totals')).not.toBeInTheDocument()
  })
})

describe('ProductionGaugePanel — ungauged is never rendered as 0.0', () => {
  it('shows the quiet_reason where the ratio would be, and no numeric ratio', async () => {
    mockFetch()
    renderPanel()
    const quiet = await screen.findByTestId(
      'production-gauge-quiet-source_production:src_ru_milblog_07',
    )
    expect(quiet).toHaveTextContent('insufficient_history')
    expect(quiet).toHaveTextContent(/no ratio — ungauged/)
    expect(quiet.textContent).not.toMatch(/0\.00/)

    // …and there is no ratio meter on that row at all.
    expect(
      screen.queryByTestId('production-gauge-ratio-source_production:src_ru_milblog_07'),
    ).not.toBeInTheDocument()

    // The whole row carries no "0.00x" anywhere.
    const row = screen.getByTestId('production-gauge-row-source_production:src_ru_milblog_07')
    expect(row.textContent).not.toMatch(/0\.00×/)
    expect(screen.getByTestId('production-gauge-state-source_production:src_ru_milblog_07'))
      .toHaveTextContent('ungauged')
  })

  it('explains the reason in plain language when the row is opened', async () => {
    mockFetch()
    renderPanel()
    await screen.findByTestId('production-gauge-row-source_production:src_ru_milblog_07')
    fireEvent.click(
      screen.getByTestId('production-gauge-toggle-source_production:src_ru_milblog_07'),
    )
    expect(
      await screen.findByTestId(
        'production-gauge-quiet-reason-source_production:src_ru_milblog_07',
      ),
    ).toHaveTextContent(/too little history to form an honest baseline/)
  })

  it('says when the silence is a FAILED query rather than quiet-by-design', async () => {
    mockFetch()
    renderPanel()
    await screen.findByTestId('production-gauge-row-desk_head_staleness:desk_heads')
    fireEvent.click(screen.getByTestId('production-gauge-toggle-desk_head_staleness:desk_heads'))
    expect(
      await screen.findByTestId('production-gauge-quiet-reason-desk_head_staleness:desk_heads'),
    ).toHaveTextContent(/FAILED/)
  })

  it('still renders a MEASURED ratio as a number', async () => {
    mockFetch()
    renderPanel()
    expect(
      await screen.findByTestId('production-gauge-ratio-analyst_cadence:war_beat'),
    ).toHaveTextContent('6.20×')
  })
})

describe('ProductionGaugePanel — what would page', () => {
  it('marks the paging row unmistakably and leaves the sub-floor deficit unmarked', async () => {
    mockFetch()
    renderPanel()
    expect(
      await screen.findByTestId('production-gauge-pages-analyst_cadence:war_beat'),
    ).toHaveTextContent('PAGES')
    expect(
      screen.queryByTestId('production-gauge-pages-llm_latency:core_plane'),
    ).not.toBeInTheDocument()
    // The sub-floor row is still visibly a deficit — it just does not page.
    expect(screen.getByTestId('production-gauge-state-llm_latency:core_plane')).toHaveTextContent(
      'deficit',
    )
  })

  it('quotes the floor from the payload, not a hardcoded threshold', async () => {
    mockFetch()
    renderPanel()
    expect(await screen.findByTestId('production-gauge-paging-note')).toHaveTextContent(
      '3 of 9 deficits clear the alert floor (severity medium and above) and would page.',
    )
  })

  it('follows the server when the alert floor moves', async () => {
    mockFetch(gauge({ alert_min_severity: 'critical' }))
    renderPanel()
    expect(await screen.findByTestId('production-gauge-paging-note')).toHaveTextContent(
      /severity critical and above/,
    )
    // The severity chips re-mark themselves against that floor too.
    expect(screen.getByTestId('production-gauge-severity-high')).not.toHaveTextContent('pages')
    expect(screen.getByTestId('production-gauge-severity-critical')).toHaveTextContent('pages')
  })

  it('explains per-row why a deficit does or does not page', async () => {
    mockFetch()
    renderPanel()
    await screen.findByTestId('production-gauge-row-llm_latency:core_plane')
    fireEvent.click(screen.getByTestId('production-gauge-toggle-llm_latency:core_plane'))
    expect(await screen.findByTestId('production-gauge-paging-llm_latency:core_plane')).toHaveTextContent(
      'does not page — low is below the medium alert floor',
    )
  })
})

describe('ProductionGaugePanel — filtering never moves the totals', () => {
  it('shows whole-engine totals with no filter', async () => {
    mockFetch()
    renderPanel()
    expect(await screen.findByTestId('production-gauge-total-loops')).toHaveTextContent('268')
    expect(screen.getByTestId('production-gauge-total-gauged')).toHaveTextContent('68')
    expect(screen.getByTestId('production-gauge-total-ok')).toHaveTextContent('59')
    expect(screen.getByTestId('production-gauge-total-ungauged')).toHaveTextContent('200')
    expect(screen.getByTestId('production-gauge-total-paging')).toHaveTextContent('3')
    expect(screen.getByTestId('production-gauge-totals-caption')).toHaveTextContent(
      /Whole-engine totals — computed server-side over every loop\./,
    )
  })

  it('keeps those totals when deficits-only narrows the rows', async () => {
    mockFetch()
    renderPanel()
    await screen.findByTestId('production-gauge-row-judge_availability:llm_judge')

    fireEvent.click(screen.getByTestId('production-gauge-filter-deficits'))

    await waitFor(() => expect(calls.some((c) => c.includes('deficits_only=true'))).toBe(true))
    // Two deficit rows remain and the ok row is gone.
    await waitFor(() => {
      expect(
        screen.getByTestId('production-gauge-row-analyst_cadence:war_beat'),
      ).toBeInTheDocument()
      expect(
        screen.queryByTestId('production-gauge-row-judge_availability:llm_judge'),
      ).not.toBeInTheDocument()
    })
    expect(screen.getByTestId('production-gauge-row-llm_latency:core_plane')).toBeInTheDocument()
    // …and the totals are UNMOVED — still the whole engine, not the 2 rows shown.
    expect(screen.getByTestId('production-gauge-total-loops')).toHaveTextContent('268')
    expect(screen.getByTestId('production-gauge-total-ok')).toHaveTextContent('59')
    expect(screen.getByTestId('production-gauge-total-ungauged')).toHaveTextContent('200')
    expect(screen.getByTestId('production-gauge-total-deficit')).toHaveTextContent('9')
    expect(screen.getByTestId('production-gauge-totals-caption')).toHaveTextContent(
      /BEFORE the deficits-only filter/,
    )
  })

  it('keeps them when paging-only narrows the rows to one', async () => {
    mockFetch()
    renderPanel()
    await screen.findByTestId('production-gauge-row-llm_latency:core_plane')

    fireEvent.click(screen.getByTestId('production-gauge-filter-paging'))

    await waitFor(() => expect(calls.some((c) => c.includes('paging_only=true'))).toBe(true))
    await waitFor(() =>
      expect(screen.getByTestId('production-gauge-shown')).toHaveTextContent(
        '1 row shown of 268 gauged loops',
      ),
    )
    expect(
      screen.queryByTestId('production-gauge-row-llm_latency:core_plane'),
    ).not.toBeInTheDocument()
    expect(screen.getByTestId('production-gauge-total-loops')).toHaveTextContent('268')
    expect(screen.getByTestId('production-gauge-total-ungauged')).toHaveTextContent('200')
  })

  it('keeps them when a loop class narrows the rows', async () => {
    mockFetch()
    renderPanel()
    await screen.findByTestId('production-gauge-row-analyst_cadence:war_beat')

    fireEvent.change(screen.getByTestId('production-gauge-class-filter'), {
      target: { value: 'llm_latency' },
    })

    await waitFor(() => expect(calls.some((c) => c.includes('loop_class=llm_latency'))).toBe(true))
    await waitFor(() =>
      expect(screen.getByTestId('production-gauge-totals-caption')).toHaveTextContent(
        /BEFORE the LLM latency filter/,
      ),
    )
    expect(
      screen.queryByTestId('production-gauge-row-analyst_cadence:war_beat'),
    ).not.toBeInTheDocument()
    expect(screen.getByTestId('production-gauge-total-loops')).toHaveTextContent('268')
    // The whole-engine group counts also stay whole-engine.
    expect(screen.getByTestId('production-gauge-group-counts-metering')).toHaveTextContent(
      '5 gauged',
    )
  })

  it('says a filter matched nothing without pretending the engine is empty', async () => {
    mockFetch(gauge({ loops: [HEALTHY, UNGAUGED] }))
    renderPanel()
    await screen.findByTestId('production-gauge-row-judge_availability:llm_judge')

    fireEvent.click(screen.getByTestId('production-gauge-filter-paging'))

    const empty = await screen.findByTestId('production-gauge-empty-filter')
    expect(empty).toHaveTextContent(/No loop matches paging-only/)
    expect(empty).toHaveTextContent(/computed before this filter/)
    expect(screen.getByTestId('production-gauge-total-loops')).toHaveTextContent('268')
    expect(screen.queryByTestId('production-gauge-quiet')).not.toBeInTheDocument()
  })

  it('sends the baseline-window override to the server', async () => {
    mockFetch()
    renderPanel()
    await screen.findByTestId('production-gauge-totals')
    fireEvent.change(screen.getByTestId('production-gauge-window-filter'), {
      target: { value: '90' },
    })
    await waitFor(() => expect(calls.some((c) => c.includes('window_days=90'))).toBe(true))
    // A baseline override is not a row filter — the totals caption stays clean.
    await waitFor(() =>
      expect(screen.getByTestId('production-gauge-totals-caption')).toHaveTextContent(
        /over every loop\./,
      ),
    )
  })
})
