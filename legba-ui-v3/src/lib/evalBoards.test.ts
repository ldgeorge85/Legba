/**
 * Tests for the `system.eval_boards` pure model.
 *
 * These hold the three boards' honesty contracts as executable rules, so the
 * panel cannot quietly lose them in a render refactor:
 *
 *   * a baseline board that was never computed stays DISTINCT from a computed
 *     board that returned nothing, and the server's `note` comes back verbatim;
 *   * a truncated trajectory says its last desk group may be incomplete, and
 *     `total_rows` is labelled as scanned ROWS, never as points;
 *   * every rate carries its denominator, and every null statistic renders as
 *     an absence rather than a 0 — the failure mode these boards invite most.
 */

import { describe, it, expect } from 'vitest'
import { ApiError } from '@/lib/api'
import type {
  AnalystRuntimeRow,
  BandTrajectoryResponse,
  DeskBaselineBoard,
  DeskBaselineRow,
  TrajectoryPoint,
} from '@/lib/api'
import {
  NOT_RECORDED,
  TRAJECTORY_DAYS_MAX,
  TRAJECTORY_DAYS_MIN,
  avgSecondsLabel,
  bandLabel,
  baselineBoardState,
  baselineCountsLine,
  baselineNote,
  baselineRowFacts,
  baselineSampleLabel,
  baselineStateMessage,
  boardErrorText,
  confidenceLabel,
  deviationDirection,
  deviationLabel,
  formatMetric,
  formatSeconds,
  insufficientHistoryLabel,
  isValidTrajectoryDays,
  maxSecondsLabel,
  nonSuccessLabel,
  orderedBaselineRows,
  orderedTrajectoryDimensions,
  pointTitle,
  rateWithN,
  runtimeErrorText,
  runtimeTotalsLine,
  runtimeWindowLabel,
  seriesLabel,
  trajectorySummaryLine,
  trajectoryTotals,
  truncationWarning,
} from './evalBoards'

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function baselineRow(over: Partial<DeskBaselineRow> = {}): DeskBaselineRow {
  return {
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
    ...over,
  }
}

function board(over: Partial<DeskBaselineBoard> = {}): DeskBaselineBoard {
  return {
    available: true,
    computed_at: '2026-08-20T00:00:00Z',
    note: 'Descriptive baseline over our own substrate. Not a forecast.',
    counts: { total: 3, above: 1, below: 0, insufficient_history: 1 },
    rows: [baselineRow()],
    ...over,
  }
}

function point(over: Partial<TrajectoryPoint> = {}): TrajectoryPoint {
  return {
    ts: '2026-08-19T12:00:00Z',
    band: 'elevated',
    effective_confidence: 0.71,
    faithfulness_flagged: false,
    scorecard_row_id: 'row-1',
    ...over,
  }
}

function trajectory(over: Partial<BandTrajectoryResponse> = {}): BandTrajectoryResponse {
  return {
    days: 14,
    server_now: '2026-08-20T00:00:00Z',
    desks: [
      {
        target_id: 'country_g20_br',
        dimensions: {
          escalation: [point(), point({ scorecard_row_id: 'row-2', band: 'high' })],
          energy_security: [
            point({
              scorecard_row_id: 'row-3',
              effective_confidence: null,
              faithfulness_flagged: true,
            }),
          ],
        },
      },
    ],
    total_rows: 120,
    truncated: false,
    ...over,
  }
}

function runtimeRow(over: Partial<AnalystRuntimeRow> = {}): AnalystRuntimeRow {
  return {
    analyst_id: 'escalation',
    runs: 17,
    avg_seconds: 42.5,
    max_seconds: 91.25,
    last_run_at: '2026-08-20T00:00:00Z',
    non_success: 2,
    window_hours: 24,
    ...over,
  }
}

// ---------------------------------------------------------------------------
// Cross-cutting
// ---------------------------------------------------------------------------

describe('rateWithN — a rate never travels without its denominator', () => {
  it('always carries the numerator and denominator alongside the percentage', () => {
    expect(rateWithN(2, 17)).toBe('12% (2/17)')
  })

  it('refuses to report a rate over a zero denominator (0 of 0 is not 0%)', () => {
    expect(rateWithN(0, 0)).toBe('no observations yet (n=0)')
    expect(rateWithN(0, 0, { emptyLabel: 'no runs in window (n=0)' })).toBe(
      'no runs in window (n=0)',
    )
  })

  it('keeps a sub-1% rate visible instead of rounding it to 0%', () => {
    expect(rateWithN(1, 1000)).toBe('0.1% (1/1000)')
  })
})

describe('formatSeconds / formatMetric — null is an absence, never a zero', () => {
  it('renders a null duration as an absence', () => {
    expect(formatSeconds(null)).toBe(NOT_RECORDED)
    expect(formatSeconds(undefined)).toBe(NOT_RECORDED)
    expect(formatSeconds(Number.NaN)).toBe(NOT_RECORDED)
  })

  it('renders a measured zero as a zero (a fast run is not a missing run)', () => {
    expect(formatSeconds(0)).toBe('0.00s')
  })

  it('scales the unit with the magnitude and never overflows the seconds field', () => {
    expect(formatSeconds(4.567)).toBe('4.57s')
    expect(formatSeconds(42.5)).toBe('42.5s')
    expect(formatSeconds(119.6)).toBe('2m 00s')
    expect(formatSeconds(3661)).toBe('61m 01s')
  })

  it('formats metrics without inventing precision, and nulls as absences', () => {
    expect(formatMetric(14)).toBe('14')
    expect(formatMetric(1.234)).toBe('1.23')
    expect(formatMetric(null)).toBe(NOT_RECORDED)
  })
})

describe('boardErrorText — a failed read never reads as an empty board', () => {
  it('says a 400 was a rejection and that the server does not clamp', () => {
    const text = boardErrorText(new ApiError(400, { detail: 'days must be 1..90' }), 'Band trajectory')
    expect(text).toContain('REJECTED')
    expect(text).toContain('days must be 1..90')
    expect(text).toContain('validates rather than clamps')
    expect(text).toContain('not an empty board')
  })

  it('surfaces the server detail for any other status', () => {
    expect(boardErrorText(new ApiError(500, { detail: 'pg down' }), 'Desk baselines')).toContain(
      'pg down',
    )
  })

  it('handles a non-ApiError throw without losing the warning', () => {
    expect(boardErrorText(new Error('network down'), 'Desk baselines')).toContain(
      'not an empty board',
    )
  })
})

// ---------------------------------------------------------------------------
// Board 1 — desk baselines
// ---------------------------------------------------------------------------

describe('baselineBoardState — "not computed" is not "computed and empty"', () => {
  it('classifies available:false as unavailable even when rows are present', () => {
    expect(baselineBoardState(board({ available: false }))).toBe('unavailable')
  })

  it('classifies a computed board with no rows as empty, not unavailable', () => {
    expect(baselineBoardState(board({ rows: [] }))).toBe('empty')
  })

  it('classifies a computed board with rows as ready', () => {
    expect(baselineBoardState(board())).toBe('ready')
  })

  it('treats a missing board as unavailable rather than empty', () => {
    expect(baselineBoardState(undefined)).toBe('unavailable')
  })

  it('gives the two states materially different messages', () => {
    expect(baselineStateMessage('unavailable')).toContain('NOT COMPUTED')
    expect(baselineStateMessage('empty')).toContain('measured emptiness')
    expect(baselineStateMessage('unavailable')).not.toBe(baselineStateMessage('empty'))
  })
})

describe('baselineNote — the server disclaimer, verbatim', () => {
  it('returns the server note unchanged and marks it verbatim', () => {
    const b = board({ note: 'These bands are DESCRIPTIVE. They are not predictions.' })
    expect(baselineNote(b)).toEqual({
      text: 'These bands are DESCRIPTIVE. They are not predictions.',
      verbatim: true,
    })
  })

  it('falls back to our own wording when the server sent none — and flags it as ours', () => {
    const fallback = baselineNote(board({ note: '   ' }))
    expect(fallback.verbatim).toBe(false)
    expect(fallback.text).toContain('not a forecast')
  })
})

describe('baselineCountsLine — the server counts, nothing derived', () => {
  it('reports total, above, below and insufficient-history from the server counts', () => {
    expect(baselineCountsLine(board())).toBe(
      '3 desk-metric rows · 1 above band · 0 below band · 1 on insufficient history',
    )
  })

  it('omits a count the server did not send rather than inventing a zero', () => {
    const line = baselineCountsLine(board({ counts: { total: 2 } }))
    expect(line).toBe('2 desk-metric rows')
    expect(line).not.toContain('above')
  })
})

describe('deviation classification', () => {
  it('reads the three known directions off the wire', () => {
    expect(deviationDirection(baselineRow({ deviation: 'above' }))).toBe('above')
    expect(deviationDirection(baselineRow({ deviation: 'below' }))).toBe('below')
    expect(deviationDirection(baselineRow({ deviation: 'within' }))).toBe('within')
  })

  it('never invents a direction for an unrecognised value', () => {
    expect(deviationDirection(baselineRow({ deviation: 'sideways' }))).toBe('unknown')
    expect(deviationLabel(baselineRow({ deviation: 'sideways' }))).toContain('unrecognised')
  })

  it('states the sigma with the direction', () => {
    expect(deviationLabel(baselineRow())).toBe('above band (3.42σ)')
  })

  it('says the sigma is absent rather than printing a 0σ deviation', () => {
    const label = deviationLabel(baselineRow({ deviation_sigma: null }))
    expect(label).toBe('above band (σ not computed)')
    expect(label).not.toContain('0.00σ')
  })
})

describe('band + sample labels — the n behind the band', () => {
  it('shows the band with the centre and spread it was built from', () => {
    expect(bandLabel(baselineRow())).toBe('3 – 9 (median 6 ± 1.20 × 2.50σ)')
  })

  it('carries the active/sample days and the baseline window', () => {
    expect(baselineSampleLabel(baselineRow())).toBe(
      '26/28 active days over a 28d baseline',
    )
  })
})

describe('insufficientHistoryLabel — a thin row is discounted, not reported', () => {
  it('returns null for a row with real history', () => {
    expect(insufficientHistoryLabel(baselineRow())).toBeNull()
  })

  it('names the thin sample and says the row is not a finding', () => {
    const label = insufficientHistoryLabel(
      baselineRow({ insufficient_history: true, active_days: 4, baseline_days: 28 }),
    )
    expect(label).toContain('4 active of 28 baseline days')
    expect(label).toContain('not a finding')
  })
})

describe('baselineRowFacts / orderedBaselineRows', () => {
  it('exposes every remaining wire field, with an honest empty for missing ones', () => {
    const facts = baselineRowFacts(
      baselineRow({ geo: [], features: {}, computed_at: null }),
    )
    const byKey = Object.fromEntries(facts.map((f) => [f.key, f.value]))
    expect(byKey.geo).toBe('(no geo scope)')
    expect(byKey.features).toBe('(none carried)')
    expect(byKey['computed at']).toBe('not stamped')
    expect(byKey['min current floor']).toBe('3')
  })

  it('preserves the server ordering (most-deviating first) and does not mutate it', () => {
    const rows = [baselineRow({ metric: 'a' }), baselineRow({ metric: 'b' })]
    const out = orderedBaselineRows(rows)
    expect(out.map((r) => r.metric)).toEqual(['a', 'b'])
    expect(out).not.toBe(rows)
  })
})

// ---------------------------------------------------------------------------
// Board 2 — band trajectory
// ---------------------------------------------------------------------------

describe('trajectory days — the server validates, it does not clamp', () => {
  it('accepts the documented inclusive range', () => {
    expect(isValidTrajectoryDays(TRAJECTORY_DAYS_MIN)).toBe(true)
    expect(isValidTrajectoryDays(TRAJECTORY_DAYS_MAX)).toBe(true)
    expect(isValidTrajectoryDays(30)).toBe(true)
  })

  it('rejects anything outside it (a 400 server-side, never a clamp)', () => {
    expect(isValidTrajectoryDays(0)).toBe(false)
    expect(isValidTrajectoryDays(91)).toBe(false)
    expect(isValidTrajectoryDays(7.5)).toBe(false)
  })
})

describe('trajectoryTotals — points are points, scanned rows are rows', () => {
  it('counts real points, flagged points and confidence presence separately', () => {
    const t = trajectoryTotals(trajectory())
    expect(t.desks).toBe(1)
    expect(t.dimensions).toBe(2)
    expect(t.points).toBe(3)
    expect(t.flagged).toBe(1)
    expect(t.withConfidence).toBe(2)
    expect(t.scannedRows).toBe(120)
  })

  it('never conflates the server row cap count with the point count', () => {
    const line = trajectorySummaryLine(trajectory())
    expect(line).toContain('3 banded points')
    expect(line).toContain('120 scorecard rows scanned (rows, not points)')
  })

  it('reports the confidence coverage with its n', () => {
    expect(trajectorySummaryLine(trajectory())).toContain(
      'effective confidence recorded on 2/3',
    )
  })

  it('returns an all-zero total for a missing response without throwing', () => {
    expect(trajectoryTotals(undefined).points).toBe(0)
  })
})

describe('truncationWarning — a capped scan is never a full series', () => {
  it('is null when the scan completed', () => {
    expect(truncationWarning(trajectory())).toBeNull()
  })

  it('says the LAST desk group may be incomplete and gives the scanned count', () => {
    const warn = truncationWarning(trajectory({ truncated: true, total_rows: 500 }))
    expect(warn).toContain('TRUNCATED')
    expect(warn).toContain('LAST DESK GROUP')
    expect(warn).toContain('500 scanned scorecard rows')
  })
})

describe('trajectory point rendering helpers', () => {
  it('orders dimension series stably by name', () => {
    expect(orderedTrajectoryDimensions(trajectory().desks[0]).map(([k]) => k)).toEqual([
      'energy_security',
      'escalation',
    ])
  })

  it('renders a null effective_confidence as an absence, never as 0.00', () => {
    expect(confidenceLabel(null)).toBe(NOT_RECORDED)
    expect(confidenceLabel(0.71)).toBe('0.71')
    // A measured zero confidence is still a measurement.
    expect(confidenceLabel(0)).toBe('0.00')
  })

  it('puts the band, the confidence-or-absence and the flag in the point title', () => {
    const title = pointTitle(point({ effective_confidence: null, faithfulness_flagged: true }))
    expect(title).toContain('band elevated')
    expect(title).toContain(`effective confidence ${NOT_RECORDED}`)
    expect(title).toContain('FAITHFULNESS FLAGGED')
  })

  it('gives each series its own n so a short strip cannot pass for a trend', () => {
    expect(seriesLabel([point(), point({ faithfulness_flagged: true, effective_confidence: null })])).toBe(
      '2 points · 1 flagged · confidence on 1/2',
    )
  })
})

// ---------------------------------------------------------------------------
// Board 3 — analyst runtime
// ---------------------------------------------------------------------------

describe('runtimeWindowLabel — the echoed window, stated once', () => {
  it('collapses the per-row echo into a single window', () => {
    expect(runtimeWindowLabel([runtimeRow(), runtimeRow({ analyst_id: 'other' })], 24)).toBe(
      '24h window',
    )
  })

  it('falls back to the REQUESTED window when no row came back to echo one', () => {
    expect(runtimeWindowLabel([], 72)).toBe(
      '72h window (requested — no rows came back to echo it)',
    )
  })

  it('surfaces a disagreement instead of silently picking one row', () => {
    expect(
      runtimeWindowLabel([runtimeRow(), runtimeRow({ analyst_id: 'x', window_hours: 72 })], 24),
    ).toBe('rows disagree on the window: 24h, 72h')
  })
})

describe('runtime rates and means always carry their n', () => {
  it('reports the non-success count against the runs denominator', () => {
    expect(nonSuccessLabel(runtimeRow())).toBe('12% (2/17)')
  })

  it('reports no rate at all for an analyst with zero runs', () => {
    expect(nonSuccessLabel(runtimeRow({ runs: 0, non_success: 0 }))).toBe(
      'no runs in window (n=0)',
    )
  })

  it('states the mean with the number of runs it was taken over', () => {
    expect(avgSecondsLabel(runtimeRow())).toBe('42.5s over 17 runs')
  })

  it('renders a null mean/max as an absence, never as 0s', () => {
    expect(avgSecondsLabel(runtimeRow({ avg_seconds: null }))).toBe(NOT_RECORDED)
    expect(maxSecondsLabel(runtimeRow({ max_seconds: null }))).toBe(NOT_RECORDED)
    expect(avgSecondsLabel(runtimeRow({ avg_seconds: null }))).not.toContain('0')
  })

  it('rolls the fleet up with the failure count against total runs', () => {
    const line = runtimeTotalsLine([runtimeRow(), runtimeRow({ analyst_id: 'x', runs: 3, non_success: 0 })])
    expect(line).toContain('2 analysts')
    expect(line).toContain('20 runs')
    expect(line).toContain('(2/20)')
  })

  it('reports an honest n=0 for an empty board rather than a 0% failure rate', () => {
    const line = runtimeTotalsLine([])
    expect(line).toBe('no analyst runs recorded in this window (n=0)')
    expect(line).not.toContain('0%')
  })
})

describe('runtimeErrorText — the board with no degradation wrapper', () => {
  it('says the read failed and that no row count can be inferred from it', () => {
    const text = runtimeErrorText(new ApiError(500, { detail: 'relation does not exist' }))
    expect(text).toContain('relation does not exist')
    expect(text).toContain('no server-side degradation wrapper')
    expect(text).toContain('no row count can be inferred')
    expect(text).toContain('not an empty board')
  })
})
