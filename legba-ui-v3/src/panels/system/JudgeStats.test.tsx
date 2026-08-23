/**
 * Component test for `system.judge_stats`.
 *
 * Mocks the registry at the HTTP boundary. What these hold:
 *   * a `measured: false` payload renders as a FAILED READ, loudly — the server
 *     degrades to an all-defaults 200 rather than 500ing a polling panel, so
 *     this is the only thing separating "we could not look" from "nothing
 *     happened", and getting it wrong turns an outage into an all-clear;
 *   * sentinel buckets render apart from real providers and carry the SERVER's
 *     own explanation;
 *   * a null faithfulness mean shows "unmeasured", never 0.000;
 *   * an under-sampled comparison refuses to report drift;
 *   * a window spanning two judge-pipeline stamps warns before the pooled mean
 *     is read.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import JudgeStatsPanel from './JudgeStats'
import { MIN_COMPARABLE_N } from '@/lib/judgeStats'
import type { JudgeStatsProvider, JudgeStatsResponse } from '@/lib/api'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 'js1',
    panel_id: 'system_judge_stats',
    descriptor_id: '(singleton)',
    descriptor_version: '0'.repeat(64),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Judge Stats',
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

function provider(over: Partial<JudgeStatsProvider> = {}): JudgeStatsProvider {
  return {
    served_by: 'DeepInfra',
    is_sentinel: false,
    n: 0,
    by_status: {},
    adjudicated_n: 0,
    adjudicated_share: null,
    faithfulness_n: 0,
    faithfulness_mean: null,
    judge_calls: 0,
    judge_call_errors: 0,
    latency_p95_ms: null,
    first_call_at: null,
    last_call_at: null,
    ...over,
  }
}

function payload(over: Partial<JudgeStatsResponse> = {}): JudgeStatsResponse {
  return {
    generated_at: '2026-08-21T00:00:00Z',
    window_days: 14,
    measured: true,
    pools_across_pipeline_versions: false,
    totals: {
      critiques: 0,
      by_status: {},
      attributed: 0,
      unattributed: 0,
      providers: 0,
      adjudicated_n: 0,
      adjudicated_share: null,
      faithfulness_n: 0,
      faithfulness_mean: null,
      judge_calls: 0,
      judge_call_errors: 0,
    },
    providers: [],
    pipeline_versions: [],
    cells: [],
    sentinels: {},
    judge_statuses: ['llm', 'deterministic', 'unsampled', '(unknown)'],
    ...over,
  }
}

let served: JudgeStatsResponse
const seenUrls: string[] = []

beforeEach(() => {
  seenUrls.length = 0
  served = payload()
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      seenUrls.push(url)
      return {
        ok: true,
        status: 200,
        json: async () => served,
        text: async () => JSON.stringify(served),
      } as unknown as Response
    }),
  )
})

const BIG = MIN_COMPARABLE_N + 20

describe('JudgeStats panel', () => {
  it('renders a provider with its verdict mix and both statistics carrying n', async () => {
    served = payload({
      totals: {
        ...payload().totals,
        critiques: BIG, attributed: BIG, providers: 1,
      },
      providers: [
        provider({
          served_by: 'DeepInfra',
          n: BIG,
          by_status: { llm: BIG },
          adjudicated_n: BIG,
          adjudicated_share: 1,
          faithfulness_n: BIG,
          faithfulness_mean: 0.812,
          judge_calls: 60,
          latency_p95_ms: 1200,
        }),
      ],
    })
    render(wrap(<JudgeStatsPanel registration={reg()} scope={{}} mode="personal" />))

    await screen.findByTestId('judge-stats-provider-DeepInfra')
    expect(screen.getByTestId('judge-stats-faithfulness-DeepInfra')).toHaveTextContent(
      `0.812 (n=${BIG})`,
    )
    expect(screen.getByTestId('judge-stats-adjudicated-DeepInfra')).toHaveTextContent(
      `100.0% (n=${BIG})`,
    )
    expect(screen.getByTestId('judge-stats-mix-DeepInfra-llm')).toBeInTheDocument()
  })

  it('renders a FAILED READ loudly and never as an all-clear', async () => {
    served = payload({ measured: false })
    render(wrap(<JudgeStatsPanel registration={reg()} scope={{}} mode="personal" />))

    const banner = await screen.findByTestId('judge-stats-unmeasured')
    expect(banner).toHaveTextContent(/could not be read/i)
    expect(banner).toHaveTextContent(/not.*a report that the judge was idle/i)
    // The body is suppressed entirely — an empty table under a failed read is
    // exactly the thing that reads as "all clear".
    expect(screen.queryByTestId('judge-stats-providers')).not.toBeInTheDocument()
  })

  it('a genuinely quiet window reads differently from a failed one', async () => {
    served = payload({ measured: true })
    render(wrap(<JudgeStatsPanel registration={reg()} scope={{}} mode="personal" />))

    await screen.findByTestId('judge-stats-no-providers')
    expect(screen.queryByTestId('judge-stats-unmeasured')).not.toBeInTheDocument()
  })

  it('shows a null mean as unmeasured, not 0.000', async () => {
    served = payload({
      totals: { ...payload().totals, critiques: 5, attributed: 5, providers: 1 },
      providers: [
        provider({
          served_by: 'Nvidia',
          n: 5,
          by_status: { llm: 5 },
          faithfulness_n: 0,
          faithfulness_mean: null,
        }),
      ],
    })
    render(wrap(<JudgeStatsPanel registration={reg()} scope={{}} mode="personal" />))

    const cell = await screen.findByTestId('judge-stats-faithfulness-Nvidia')
    expect(cell).toHaveTextContent('unmeasured')
    expect(cell).not.toHaveTextContent('0.000')
  })

  it('keeps sentinel buckets apart and shows the SERVER explanation', async () => {
    served = payload({
      totals: {
        ...payload().totals, critiques: 900, attributed: 0, unattributed: 900,
      },
      providers: [
        provider({
          served_by: '(no receipt)',
          is_sentinel: true,
          n: 900,
          by_status: { deterministic: 900 },
        }),
      ],
      sentinels: {
        '(no receipt)':
          'No verify_judge receipt on the run. Expected for deterministic verdicts.',
      },
    })
    render(wrap(<JudgeStatsPanel registration={reg()} scope={{}} mode="personal" />))

    await screen.findByTestId('judge-stats-sentinels')
    expect(screen.getByTestId('judge-stats-meaning-(no receipt)')).toHaveTextContent(
      /Expected for deterministic verdicts/,
    )
    // 900 unattributed verdicts must NOT be presented as a provider.
    expect(screen.getByTestId('judge-stats-no-providers')).toBeInTheDocument()
  })

  it('refuses to report drift on an under-sampled comparison', async () => {
    served = payload({
      totals: { ...payload().totals, critiques: 100, attributed: 100, providers: 2 },
      providers: [
        provider({ served_by: 'A', n: BIG, faithfulness_n: BIG, faithfulness_mean: 0.9 }),
        provider({ served_by: 'B', n: 3, faithfulness_n: 3, faithfulness_mean: 0.1 }),
      ],
    })
    render(wrap(<JudgeStatsPanel registration={reg()} scope={{}} mode="personal" />))

    const card = await screen.findByTestId('judge-stats-drift-insufficient')
    expect(card).toHaveTextContent(/not a finding of "no drift"/i)
    expect(screen.queryByTestId('judge-stats-drift-drift')).not.toBeInTheDocument()
  })

  it('reports real drift when both samples are big enough', async () => {
    served = payload({
      totals: { ...payload().totals, critiques: 200, attributed: 200, providers: 2 },
      providers: [
        provider({ served_by: 'A', n: BIG, faithfulness_n: BIG, faithfulness_mean: 0.9, adjudicated_n: BIG, adjudicated_share: 0.95 }),
        provider({ served_by: 'B', n: BIG, faithfulness_n: BIG, faithfulness_mean: 0.7, adjudicated_n: BIG, adjudicated_share: 0.80 }),
      ],
    })
    render(wrap(<JudgeStatsPanel registration={reg()} scope={{}} mode="personal" />))

    const card = await screen.findByTestId('judge-stats-drift-drift')
    expect(card).toHaveTextContent(/disagree/i)
    expect(card).toHaveTextContent('0.200')
  })

  it('warns when the window pools across a judge-pipeline change', async () => {
    served = payload({
      pools_across_pipeline_versions: true,
      totals: { ...payload().totals, critiques: 10 },
      pipeline_versions: [
        { judge_pipeline_version: '2026-08-05/1', n: 5, providers: ['A'], faithfulness_n: 5, faithfulness_mean: 0.9 },
        { judge_pipeline_version: '2026-08-20/1', n: 5, providers: ['A'], faithfulness_n: 5, faithfulness_mean: 0.6 },
      ],
    })
    render(wrap(<JudgeStatsPanel registration={reg()} scope={{}} mode="personal" />))

    const caveat = await screen.findByTestId('judge-stats-pipeline-caveat')
    expect(caveat).toHaveTextContent('2026-08-05/1')
    expect(caveat).toHaveTextContent(/per-stamp rows/)
    expect(screen.getByTestId('judge-stats-version-2026-08-20/1')).toHaveTextContent(
      '0.600 (n=5)',
    )
  })

  it('changing the window refetches with the new days param', async () => {
    render(wrap(<JudgeStatsPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(seenUrls.length).toBeGreaterThan(0))
    expect(seenUrls[0]).toContain('days=14')

    fireEvent.click(screen.getByTestId('judge-stats-window-90'))
    await waitFor(() =>
      expect(seenUrls.some((u) => u.includes('days=90'))).toBe(true),
    )
  })

  it('renders the day grid from the cube cells', async () => {
    served = payload({
      totals: { ...payload().totals, critiques: 5, attributed: 5, providers: 1 },
      providers: [provider({ served_by: 'A', n: 5, faithfulness_n: 5, faithfulness_mean: 0.8 })],
      cells: [
        { day: '2026-08-20', served_by: 'A', judge_status: 'llm', judge_pipeline_version: 'v1', n: 5, faithfulness_n: 5, faithfulness_mean: 0.8 },
      ],
    })
    render(wrap(<JudgeStatsPanel registration={reg()} scope={{}} mode="personal" />))

    const row = await screen.findByTestId('judge-stats-day-2026-08-20')
    expect(row).toHaveTextContent('A 5')
  })
})
