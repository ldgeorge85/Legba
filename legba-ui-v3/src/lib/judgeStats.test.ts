/**
 * Judge-stats model tests.
 *
 * The instrument exists to detect a ~13.6% verdict-flip effect across a provider
 * change. Everything here guards the two ways such an instrument goes wrong: it
 * invents drift that is not there (small samples, sentinel buckets folded into
 * real providers), or it hides drift that is (a null mean read as 0, a pooled
 * judge-pipeline mean presented as one grader's).
 */
import { describe, it, expect } from 'vitest'
import {
  MIN_COMPARABLE_N,
  UNMEASURED,
  cellsByDay,
  driftReadout,
  formatMeasure,
  pipelineCaveat,
  realProviders,
  sentinelMeaning,
  sentinelRows,
  statusMix,
  summaryLine,
} from './judgeStats'
import type {
  JudgeStatsCell,
  JudgeStatsProvider,
  JudgeStatsResponse,
} from '@/lib/api'

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

function response(over: Partial<JudgeStatsResponse> = {}): JudgeStatsResponse {
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

describe('formatMeasure — a statistic never travels without its n', () => {
  it('prints the value with its sample', () => {
    expect(formatMeasure(0.8123, 42)).toBe('0.812 (n=42)')
  })

  it('renders a null mean as unmeasured, never 0', () => {
    expect(formatMeasure(null, 42)).toBe(UNMEASURED)
    expect(formatMeasure(null, 42)).not.toContain('0.000')
  })

  it('renders a zero sample as unmeasured even when a value is present', () => {
    // A mean over zero rows is not a mean. If the server ever sent one, the
    // panel must still refuse to print it as a measurement.
    expect(formatMeasure(0.9, 0)).toBe(UNMEASURED)
  })

  it('formats a share as a percentage with its n', () => {
    expect(formatMeasure(0.8, 10, { percent: true })).toBe('80.0% (n=10)')
  })

  it('distinguishes a real 0.0 from an absence', () => {
    expect(formatMeasure(0, 12)).toBe('0.000 (n=12)')
  })
})

describe('sentinel handling — attribution failures are not providers', () => {
  const rows = [
    provider({ served_by: 'DeepInfra', n: 10 }),
    provider({ served_by: '(mixed)', is_sentinel: true, n: 4 }),
    provider({ served_by: '(no receipt)', is_sentinel: true, n: 900 }),
  ]

  it('splits real providers from sentinels', () => {
    expect(realProviders(rows).map((p) => p.served_by)).toEqual(['DeepInfra'])
    expect(sentinelRows(rows).map((p) => p.served_by)).toEqual([
      '(mixed)', '(no receipt)',
    ])
  })

  it('reads a sentinel meaning from the SERVER payload, not a local copy', () => {
    const map = { '(mixed)': 'the run named more than one provider' }
    expect(sentinelMeaning('(mixed)', map)).toBe(
      'the run named more than one provider',
    )
    expect(sentinelMeaning('DeepInfra', map)).toBeNull()
  })
})

describe('statusMix', () => {
  it('reports every declared status so two providers share one axis', () => {
    const mix = statusMix(
      provider({ n: 10, by_status: { llm: 6, unsampled: 4 } }),
      ['llm', 'deterministic', 'unsampled'],
    )
    expect(mix.map((s) => s.status)).toEqual(['llm', 'deterministic', 'unsampled'])
    // `deterministic` is present at zero — an absent bucket and an empty one
    // would otherwise be indistinguishable.
    expect(mix.find((s) => s.status === 'deterministic')).toMatchObject({
      n: 0, share: 0,
    })
    expect(mix.find((s) => s.status === 'llm')?.share).toBeCloseTo(0.6)
  })

  it('keeps a status the server did not declare rather than dropping it', () => {
    const mix = statusMix(
      provider({ n: 2, by_status: { surprising: 2 } }),
      ['llm'],
    )
    expect(mix.map((s) => s.status)).toContain('surprising')
  })

  it('yields a null share when the provider has no verdicts', () => {
    const mix = statusMix(provider({ n: 0, by_status: {} }), ['llm'])
    expect(mix[0].share).toBeNull()
  })
})

describe('driftReadout — a delta must be earned', () => {
  const big = MIN_COMPARABLE_N + 20

  it('reports drift when both samples clear the floor and the means differ', () => {
    const out = driftReadout({
      providers: [
        provider({ served_by: 'A', n: big, faithfulness_n: big, faithfulness_mean: 0.9, adjudicated_share: 0.9, adjudicated_n: big }),
        provider({ served_by: 'B', n: big, faithfulness_n: big, faithfulness_mean: 0.7, adjudicated_share: 0.8, adjudicated_n: big }),
      ],
    })
    expect(out.verdict).toBe('drift')
    expect(out.faithfulnessDelta).toBeCloseTo(0.2)
    expect(out.adjudicatedDelta).toBeCloseTo(0.1)
    expect(out.summary).toContain('n=' + big)
  })

  it('reports steady when both clear the floor and the means agree', () => {
    const out = driftReadout({
      providers: [
        provider({ served_by: 'A', n: big, faithfulness_n: big, faithfulness_mean: 0.80 }),
        provider({ served_by: 'B', n: big, faithfulness_n: big, faithfulness_mean: 0.81 }),
      ],
    })
    expect(out.verdict).toBe('steady')
  })

  it('REFUSES a delta when either side is under-sampled', () => {
    // The failure mode this guards: two means over a handful of verdicts differ
    // wildly by chance, and reporting that as drift teaches the operator to
    // ignore the readout — losing the instrument.
    const out = driftReadout({
      providers: [
        provider({ served_by: 'A', n: big, faithfulness_n: big, faithfulness_mean: 0.9 }),
        provider({ served_by: 'B', n: 3, faithfulness_n: 3, faithfulness_mean: 0.1 }),
      ],
    })
    expect(out.verdict).toBe('insufficient')
    expect(out.faithfulnessDelta).toBeNull()
    expect(out.summary).toContain('B has 3 of ' + MIN_COMPARABLE_N)
  })

  it('says under-sampled is NOT a finding of no drift', () => {
    const out = driftReadout({
      providers: [
        provider({ served_by: 'A', n: 5, faithfulness_n: 5, faithfulness_mean: 0.9 }),
        provider({ served_by: 'B', n: 5, faithfulness_n: 5, faithfulness_mean: 0.2 }),
      ],
    })
    expect(out.summary).toContain('not a finding of "no drift"')
  })

  it('never compares against a sentinel bucket', () => {
    const out = driftReadout({
      providers: [
        provider({ served_by: 'A', n: big, faithfulness_n: big, faithfulness_mean: 0.9 }),
        provider({ served_by: '(no receipt)', is_sentinel: true, n: 5000, faithfulness_n: 5000, faithfulness_mean: 0.2 }),
      ],
    })
    expect(out.verdict).toBe('single-provider')
    expect(out.compared).toEqual(['A'])
  })

  it('says so plainly when only one provider served anything', () => {
    const out = driftReadout({
      providers: [provider({ served_by: 'A', n: big, faithfulness_n: big, faithfulness_mean: 0.9 })],
    })
    expect(out.verdict).toBe('single-provider')
    expect(out.summary).toContain('not measurable against a single provider')
  })

  it('handles a window with nothing attributed at all', () => {
    const out = driftReadout({ providers: [] })
    expect(out.verdict).toBe('single-provider')
    expect(out.summary).toContain('nothing to compare')
  })

  it('compares the two HIGHEST-VOLUME providers, not the first two seen', () => {
    const out = driftReadout({
      providers: [
        provider({ served_by: 'small', n: 1, faithfulness_n: 1, faithfulness_mean: 0.5 }),
        provider({ served_by: 'big1', n: 500, faithfulness_n: big, faithfulness_mean: 0.9 }),
        provider({ served_by: 'big2', n: 400, faithfulness_n: big, faithfulness_mean: 0.9 }),
      ],
    })
    expect(out.compared).toEqual(['big1', 'big2'])
    expect(out.verdict).toBe('steady')
  })
})

describe('cellsByDay', () => {
  const cells: JudgeStatsCell[] = [
    { day: '2026-08-19', served_by: 'A', judge_status: 'llm', judge_pipeline_version: 'v1', n: 2, faithfulness_n: 2, faithfulness_mean: 0.8 },
    { day: '2026-08-20', served_by: 'A', judge_status: 'llm', judge_pipeline_version: 'v1', n: 3, faithfulness_n: 3, faithfulness_mean: 0.7 },
    { day: '2026-08-20', served_by: 'B', judge_status: 'llm', judge_pipeline_version: 'v1', n: 5, faithfulness_n: 5, faithfulness_mean: 0.6 },
  ]

  it('folds cells into days, newest first', () => {
    const rows = cellsByDay(cells)
    expect(rows.map((r) => r.day)).toEqual(['2026-08-20', '2026-08-19'])
    expect(rows[0].total).toBe(8)
    expect(rows[0].byProvider).toEqual({ A: 3, B: 5 })
  })

  it('sums multiple statuses of the same provider on one day', () => {
    const rows = cellsByDay([
      ...cells,
      { day: '2026-08-20', served_by: 'A', judge_status: 'unsampled', judge_pipeline_version: 'v1', n: 7, faithfulness_n: 0, faithfulness_mean: null },
    ])
    expect(rows[0].byProvider.A).toBe(10)
    expect(rows[0].total).toBe(15)
  })
})

describe('summaryLine — a failed read never reads as a quiet judge', () => {
  it('says plainly when the read FAILED', () => {
    const line = summaryLine(response({ measured: false }))
    expect(line).toContain('failed read')
    expect(line).toContain('not a quiet judge')
  })

  it('distinguishes a genuinely empty window', () => {
    expect(summaryLine(response({ measured: true }))).toContain(
      'no faithfulness verdicts',
    )
  })

  it('reports attributed against the total', () => {
    const line = summaryLine(response({
      totals: { ...response().totals, critiques: 100, attributed: 30, providers: 2 },
    }))
    expect(line).toContain('30 of 100 attributed')
    expect(line).toContain('2 providers')
  })
})

describe('pipelineCaveat — a pooled mean is never passed off as one grader', () => {
  it('is null inside a single stamp', () => {
    expect(pipelineCaveat(response())).toBeNull()
  })

  it('names the stamps and tells the reader to use the per-stamp rows', () => {
    const note = pipelineCaveat(response({
      pools_across_pipeline_versions: true,
      pipeline_versions: [
        { judge_pipeline_version: '2026-08-05/1', n: 5, providers: ['A'], faithfulness_n: 5, faithfulness_mean: 0.9 },
        { judge_pipeline_version: '2026-08-20/1', n: 5, providers: ['A'], faithfulness_n: 5, faithfulness_mean: 0.6 },
      ],
    }))
    expect(note).toContain('2026-08-05/1')
    expect(note).toContain('2026-08-20/1')
    expect(note).toContain('per-stamp rows')
  })
})
