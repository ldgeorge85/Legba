/**
 * Tests for the production-gauge pure model.
 *
 * Every one of these exists to pin a clause of the honesty contract rather than
 * a rendering detail:
 *
 *   * `readRatio` has no `ratio` on its unmeasured arm, so an `ungauged` row
 *     CANNOT be rendered as a measured 0.0 — the union is the guard;
 *   * `classCounts` / `groupCounts` read `totals.by_class` and disagree with the
 *     filtered `loops` array on purpose (that disagreement IS the contract:
 *     totals are pre-filter);
 *   * `gaugeReadState` separates `measured: false` (a failed read) from a
 *     genuinely quiet engine, which a naive panel would render identically;
 *   * `meetsAlertFloor` / `pagingNote` / `pagingExplainer` take the floor as an
 *     argument, so the panel can only ever quote the payload's own threshold;
 *   * `gaugeSummaryLine` reports gauged and ungauged separately and emits no
 *     percentage.
 */

import { describe, it, expect } from 'vitest'
import { ApiError } from '@/lib/api'
import type { ProductionGaugeResponse, ProductionGaugeRow, ProductionGaugeTotals } from '@/lib/api'
import {
  EMPTY_FILTER,
  GROUP_ORDER,
  LOOP_CLASSES,
  QUIET_REASON_TEXT,
  RATIO_METER_CAP,
  SEVERITY_RANK,
  classCounts,
  describeFilter,
  evidenceFields,
  formatRatio,
  gaugeErrorText,
  gaugeNotice,
  gaugeQueryOptions,
  gaugeReadState,
  gaugeSummaryLine,
  groupCounts,
  groupGaugeRows,
  isBrick,
  isReadFailureQuietReason,
  loopClassGroup,
  loopClassLabel,
  meetsAlertFloor,
  pagingExplainer,
  pagingNote,
  quietReasonLabel,
  ratioMeter,
  readRatio,
  rowKey,
  severityBuckets,
  severityRank,
  shownLine,
  sortGaugeRows,
  totalsCaption,
} from './productionGauge'

function row(patch: Partial<ProductionGaugeRow> = {}): ProductionGaugeRow {
  return {
    loop_class: 'analyst_cadence',
    loop_id: 'a1',
    label: 'Analyst one',
    state: 'ok',
    severity: 'info',
    ratio: 0.2,
    expected: 'a run every 60m',
    actual: 'last run 12m ago',
    quiet_reason: null,
    last_production_at: '2026-08-21T00:00:00Z',
    pages: false,
    evidence: {},
    ...patch,
  }
}

function totals(patch: Partial<ProductionGaugeTotals> = {}): ProductionGaugeTotals {
  return {
    loops: 0,
    gauged: 0,
    ok: 0,
    deficit: 0,
    ungauged: 0,
    paging: 0,
    by_severity: {},
    by_class: {},
    ...patch,
  }
}

function response(patch: Partial<ProductionGaugeResponse> = {}): ProductionGaugeResponse {
  return {
    generated_at: '2026-08-21T00:00:00Z',
    window_days: 14,
    alert_min_severity: 'medium',
    totals: totals(),
    loops: [],
    measured: true,
    ...patch,
  }
}

// ---------------------------------------------------------------------------

describe('severity ladder', () => {
  it('mirrors the server ladder the alert plane shares', () => {
    expect(SEVERITY_RANK).toEqual({ info: 0, low: 1, medium: 2, high: 3, critical: 4 })
  })

  it('ranks an unknown severity below info instead of guessing it is info', () => {
    expect(severityRank('spicy')).toBe(-1)
    expect(severityRank('info')).toBe(0)
  })

  it('reads the floor from its argument, never from a constant', () => {
    expect(meetsAlertFloor('medium', 'medium')).toBe(true)
    expect(meetsAlertFloor('critical', 'medium')).toBe(true)
    expect(meetsAlertFloor('low', 'medium')).toBe(false)
    // A different floor gives a different answer for the SAME severity.
    expect(meetsAlertFloor('low', 'low')).toBe(true)
    expect(meetsAlertFloor('high', 'critical')).toBe(false)
  })

  it('never claims an unrecognised severity clears the floor', () => {
    expect(meetsAlertFloor('spicy', 'medium')).toBe(false)
    expect(meetsAlertFloor('critical', 'spicy')).toBe(false)
  })
})

describe('loop classes and bricks', () => {
  it('classifies the four production loops as production, not bricks', () => {
    for (const c of ['analyst_cadence', 'analyst_production', 'source_production', 'backlog_drain']) {
      expect(loopClassGroup(c)).toBe('production')
      expect(isBrick(c)).toBe(false)
    }
  })

  it('classifies the six integrity / metering / staleness classes as bricks', () => {
    expect(loopClassGroup('judge_availability')).toBe('integrity')
    expect(loopClassGroup('descriptor_prompt_drift')).toBe('integrity')
    expect(loopClassGroup('descriptor_state_drift')).toBe('integrity')
    expect(loopClassGroup('llm_latency')).toBe('metering')
    expect(loopClassGroup('llm_daily_burn')).toBe('metering')
    expect(loopClassGroup('desk_head_staleness')).toBe('staleness')
    for (const c of [
      'judge_availability',
      'descriptor_prompt_drift',
      'descriptor_state_drift',
      'llm_latency',
      'llm_daily_burn',
      'desk_head_staleness',
    ]) {
      expect(isBrick(c)).toBe(true)
    }
  })

  it('knows exactly the ten server loop classes', () => {
    expect([...LOOP_CLASSES].sort()).toEqual(
      [
        'analyst_cadence',
        'analyst_production',
        'backlog_drain',
        'descriptor_prompt_drift',
        'descriptor_state_drift',
        'desk_head_staleness',
        'judge_availability',
        'llm_daily_burn',
        'llm_latency',
        'source_production',
      ].sort(),
    )
  })

  it('parks an unknown class in its own group and shows its raw id', () => {
    expect(loopClassGroup('quantum_vibes')).toBe('other')
    expect(loopClassLabel('quantum_vibes')).toBe('quantum_vibes')
    expect(GROUP_ORDER[GROUP_ORDER.length - 1]).toBe('other')
  })
})

describe('sortGaugeRows — worst first', () => {
  it('puts paging rows above everything, then deficit, then ungauged, then ok', () => {
    const rows = [
      row({ loop_id: 'ok', state: 'ok', ratio: 0.1 }),
      row({ loop_id: 'quiet', state: 'ungauged', ratio: null, quiet_reason: 'not_active' }),
      row({ loop_id: 'def', state: 'deficit', severity: 'low', ratio: 1.1 }),
      row({ loop_id: 'page', state: 'deficit', severity: 'critical', ratio: 5, pages: true }),
    ]
    expect(sortGaugeRows(rows).map((r) => r.loop_id)).toEqual(['page', 'def', 'quiet', 'ok'])
  })

  it('sorts ungauged ABOVE ok — "we cannot say" is not "it is fine"', () => {
    const rows = [
      row({ loop_id: 'ok', state: 'ok', ratio: 0.9 }),
      row({ loop_id: 'quiet', state: 'ungauged', ratio: null, quiet_reason: 'gather_only' }),
    ]
    expect(sortGaugeRows(rows).map((r) => r.loop_id)).toEqual(['quiet', 'ok'])
  })

  it('orders equal-state rows by severity then ratio', () => {
    const rows = [
      row({ loop_id: 'b', state: 'deficit', severity: 'high', ratio: 2.1 }),
      row({ loop_id: 'a', state: 'deficit', severity: 'critical', ratio: 4.0 }),
      row({ loop_id: 'c', state: 'deficit', severity: 'high', ratio: 3.5 }),
    ]
    expect(sortGaugeRows(rows).map((r) => r.loop_id)).toEqual(['a', 'c', 'b'])
  })

  it('does not mutate its input', () => {
    const rows = [row({ loop_id: 'ok', state: 'ok' }), row({ loop_id: 'def', state: 'deficit' })]
    sortGaugeRows(rows)
    expect(rows.map((r) => r.loop_id)).toEqual(['ok', 'def'])
  })

  it('keys a row by class AND id — the same analyst appears in two classes', () => {
    expect(rowKey(row({ loop_class: 'analyst_cadence', loop_id: 'war_beat' }))).toBe(
      'analyst_cadence:war_beat',
    )
    expect(rowKey(row({ loop_class: 'analyst_production', loop_id: 'war_beat' }))).toBe(
      'analyst_production:war_beat',
    )
  })
})

describe('groupGaugeRows', () => {
  it('splits the flat loops array into production loops and brick families', () => {
    const groups = groupGaugeRows([
      row({ loop_class: 'llm_latency', loop_id: 'core' }),
      row({ loop_class: 'analyst_cadence', loop_id: 'a1' }),
      row({ loop_class: 'desk_head_staleness', loop_id: 'desk' }),
      row({ loop_class: 'judge_availability', loop_id: 'judge' }),
      row({ loop_class: 'source_production', loop_id: 's1' }),
    ])
    expect(groups.map((g) => g.id)).toEqual(['production', 'integrity', 'metering', 'staleness'])
    expect(groups[0].rows.map((r) => r.loop_id).sort()).toEqual(['a1', 's1'])
    expect(groups[0].classes.sort()).toEqual(['analyst_cadence', 'source_production'])
  })

  it('drops empty groups rather than rendering hollow brick headers', () => {
    expect(groupGaugeRows([row()]).map((g) => g.id)).toEqual(['production'])
    expect(groupGaugeRows([])).toEqual([])
  })

  it('sorts within each group worst-first', () => {
    const groups = groupGaugeRows([
      row({ loop_class: 'llm_latency', loop_id: 'quiet', state: 'ok', ratio: 0.1 }),
      row({ loop_class: 'llm_daily_burn', loop_id: 'hot', state: 'deficit', severity: 'high', ratio: 2.5, pages: true }),
    ])
    expect(groups[0].rows.map((r) => r.loop_id)).toEqual(['hot', 'quiet'])
  })
})

describe('totals are whole-engine and never derived from loops', () => {
  const T = totals({
    loops: 268,
    gauged: 68,
    ok: 60,
    deficit: 8,
    ungauged: 200,
    paging: 3,
    by_severity: { critical: 1, high: 2, low: 5 },
    by_class: {
      analyst_cadence: { gauged: 40, ok: 38, deficit: 2, ungauged: 10 },
      llm_latency: { gauged: 4, ok: 3, deficit: 1, ungauged: 1 },
      llm_daily_burn: { gauged: 2, ok: 2, deficit: 0, ungauged: 0 },
    },
  })

  it('reads per-class counts off totals.by_class', () => {
    expect(classCounts(T, 'analyst_cadence')).toEqual({
      loops: 50,
      gauged: 40,
      ok: 38,
      deficit: 2,
      ungauged: 10,
    })
  })

  it('returns honest zeros for a class the engine did not report', () => {
    expect(classCounts(T, 'backlog_drain')).toEqual({
      loops: 0,
      gauged: 0,
      ok: 0,
      deficit: 0,
      ungauged: 0,
    })
  })

  it('sums a brick family from the server per-class numbers', () => {
    expect(groupCounts(T, 'metering')).toEqual({
      loops: 7,
      gauged: 6,
      ok: 5,
      deficit: 1,
      ungauged: 1,
    })
  })

  it('is INDEPENDENT of the filtered loops array — that is the whole point', () => {
    // A deficits-only read returns 8 rows but the engine still has 268 loops.
    const filtered = response({ totals: T, loops: [row({ state: 'deficit' })] })
    expect(filtered.totals.loops).toBe(268)
    expect(classCounts(filtered.totals, 'analyst_cadence').ok).toBe(38)
    expect(gaugeSummaryLine(filtered)).toContain('268 loops')
  })

  it('orders severity buckets worst-first and marks each against the payload floor', () => {
    expect(severityBuckets(response({ totals: T, alert_min_severity: 'medium' }))).toEqual([
      { severity: 'critical', count: 1, pages: true },
      { severity: 'high', count: 2, pages: true },
      { severity: 'low', count: 5, pages: false },
    ])
  })

  it('re-marks the same buckets when the server publishes a different floor', () => {
    expect(
      severityBuckets(response({ totals: T, alert_min_severity: 'low' })).map((b) => b.pages),
    ).toEqual([true, true, true])
  })

  it('keeps an unrecognised severity rather than dropping its count', () => {
    const b = severityBuckets(
      response({ totals: totals({ by_severity: { spicy: 4, high: 1 } }) }),
    )
    expect(b.map((x) => x.severity)).toEqual(['high', 'spicy'])
    expect(b[1]).toEqual({ severity: 'spicy', count: 4, pages: false })
  })
})

describe('readRatio — null is never 0.0', () => {
  it('reads a measured ratio with its meter', () => {
    const r = readRatio(row({ state: 'deficit', ratio: 2.5 }))
    expect(r.measured).toBe(true)
    if (!r.measured) throw new Error('expected measured')
    expect(r.ratio).toBe(2.5)
    expect(r.text).toBe('2.50×')
    expect(r.overBar).toBe(true)
    expect(r.meter.pct).toBeCloseTo(62.5)
    expect(r.meter.thresholdPct).toBeCloseTo(25)
    expect(r.meter.clamped).toBe(false)
  })

  it('gives an ungauged row NO ratio field at all, only its quiet_reason', () => {
    const r = readRatio(
      row({ state: 'ungauged', ratio: null, quiet_reason: 'insufficient_history' }),
    )
    expect(r.measured).toBe(false)
    if (r.measured) throw new Error('expected unmeasured')
    expect(r.quietReason).toBe('insufficient_history')
    expect(r.text).toBe('too little history to form an honest baseline')
    expect(r.readFailure).toBe(false)
    // The union carries no numeric ratio — nothing can render it as 0.0.
    expect('ratio' in r).toBe(false)
    expect('meter' in r).toBe(false)
  })

  it('distinguishes a MEASURED 0.0 from an ungauged row', () => {
    const zero = readRatio(row({ state: 'ok', ratio: 0 }))
    expect(zero.measured).toBe(true)
    if (!zero.measured) throw new Error('expected measured')
    expect(zero.text).toBe('0.00×')
    expect(zero.meter.pct).toBe(0)
    expect(zero.overBar).toBe(false)
  })

  it('falls back to unmeasured if a payload ever breaks the null/ungauged pact', () => {
    // state says ungauged but a ratio slipped through — the safe reading is
    // still "we cannot say".
    const r = readRatio(row({ state: 'ungauged', ratio: 0.4, quiet_reason: 'not_active' }))
    expect(r.measured).toBe(false)
    // …and the mirror case: no state, but a null ratio.
    expect(readRatio(row({ state: 'ok', ratio: null })).measured).toBe(false)
  })

  it('says so when nothing explained the silence', () => {
    const r = readRatio(row({ state: 'ungauged', ratio: null, quiet_reason: null }))
    if (r.measured) throw new Error('expected unmeasured')
    expect(r.quietReason).toBe('unstated')
    expect(r.text).toMatch(/not as healthy/)
  })

  it('flags a failed query as a read failure, not quiet-by-design', () => {
    const r = readRatio(
      row({ loop_class: 'llm_latency', state: 'ungauged', ratio: null, quiet_reason: 'latency_query_failed' }),
    )
    if (r.measured) throw new Error('expected unmeasured')
    expect(r.readFailure).toBe(true)
    expect(r.text).toMatch(/FAILED/)
    expect(isReadFailureQuietReason('not_active')).toBe(false)
    expect(isReadFailureQuietReason('state_drift_query_failed')).toBe(true)
    expect(isReadFailureQuietReason(null)).toBe(false)
  })
})

describe('ratio formatting and meter', () => {
  it('formats as a multiple of the loop own bar', () => {
    expect(formatRatio(1)).toBe('1.00×')
    expect(formatRatio(0.125)).toBe('0.13×')
  })

  it('caps the meter at the critical rung and admits the clamp', () => {
    const m = ratioMeter(9)
    expect(m.cap).toBe(RATIO_METER_CAP)
    expect(m.pct).toBe(100)
    expect(m.clamped).toBe(true)
    expect(ratioMeter(4).clamped).toBe(false)
  })

  it('never goes negative', () => {
    expect(ratioMeter(-3).pct).toBe(0)
  })

  it('puts the 1.0x bar exactly at the medium rung', () => {
    expect(ratioMeter(1).pct).toBeCloseTo(ratioMeter(1).thresholdPct)
  })
})

describe('quiet reasons', () => {
  it('covers the whole server vocabulary across all four gauge modules', () => {
    for (const reason of [
      'not_active',
      'no_declared_cadence',
      'unparsable_cadence',
      'gather_only',
      'trace_only_by_observation',
      'never_ran_within_window',
      'activation_grace',
      'insufficient_history',
      'no_overdue_work',
      'owner_not_running',
      'polling_errors',
      'no_calls_in_window',
      'no_burn_threshold',
      'no_spend_data',
      'latency_query_failed',
      'burn_query_failed',
      'no_critiques_in_window',
      'judge_never_configured',
      'prompt_manifest_unavailable',
      'no_live_descriptor_prompts',
      'no_live_descriptors',
      'no_copresent_descriptors',
      'judge_query_failed',
      'drift_query_failed',
      'state_drift_query_failed',
      'no_head_ages_stamp',
      'staleness_query_failed',
    ]) {
      expect(QUIET_REASON_TEXT[reason], reason).toBeTruthy()
    }
  })

  it('renders an unmapped reason verbatim rather than inventing a benign one', () => {
    expect(quietReasonLabel('brand_new_reason')).toBe('brand_new_reason')
  })
})

describe('gaugeReadState — a failed read is not an all-clear', () => {
  it('calls measured:false a read failure even when totals look clean', () => {
    const res = response({ measured: false })
    expect(gaugeReadState(res)).toBe('read_failed')
    const notice = gaugeNotice(res)
    expect(notice?.state).toBe('read_failed')
    expect(notice?.headline).toMatch(/READ FAILED/)
    expect(notice?.detail).toMatch(/no deficit has been ruled out/i)
  })

  it('calls a measured engine with zero loops quiet, and says it differently', () => {
    const res = response({ measured: true, totals: totals({ loops: 0 }) })
    expect(gaugeReadState(res)).toBe('engine_quiet')
    const notice = gaugeNotice(res)
    expect(notice?.state).toBe('engine_quiet')
    expect(notice?.headline).not.toMatch(/READ FAILED/)
    expect(notice?.detail).toMatch(/read succeeded/i)
  })

  it('shows no banner when the gauge is reporting', () => {
    expect(gaugeReadState(response({ totals: totals({ loops: 3 }) }))).toBe('reporting')
    expect(gaugeNotice(response({ totals: totals({ loops: 3 }) }))).toBeNull()
  })
})

describe('gaugeSummaryLine', () => {
  it('reports gauged and ungauged as separate numbers and no percentage', () => {
    const line = gaugeSummaryLine(
      response({
        window_days: 14,
        alert_min_severity: 'medium',
        totals: totals({ loops: 268, gauged: 68, ok: 60, deficit: 8, ungauged: 200, paging: 3 }),
      }),
    )
    expect(line).toContain('268 loops')
    expect(line).toContain('68 gauged (60 ok · 8 deficit)')
    expect(line).toContain('200 ungauged')
    expect(line).toContain('3 paging at medium+')
    expect(line).toContain('14d baseline')
    expect(line).not.toMatch(/%/)
  })

  it('leads with the failure when the read failed', () => {
    expect(gaugeSummaryLine(response({ measured: false }))).toMatch(/read FAILED/)
    expect(gaugeSummaryLine(response({ measured: false }))).not.toMatch(/ok/)
  })

  it('says the read succeeded when the engine is genuinely empty', () => {
    expect(gaugeSummaryLine(response({ totals: totals({ loops: 0 }) }))).toMatch(
      /no producing loop found/,
    )
  })
})

describe('totalsCaption / shownLine', () => {
  it('labels the totals whole-engine even with no filter', () => {
    expect(totalsCaption(null)).toMatch(/Whole-engine totals/)
  })

  it('says LOUDLY that a filtered view still shows pre-filter totals', () => {
    const c = totalsCaption('deficits-only')
    expect(c).toMatch(/BEFORE the deficits-only filter/)
    expect(c).toMatch(/not from the deficits-only rows below/)
  })

  it('states the shown count against the true denominator', () => {
    expect(shownLine(3, totals({ loops: 268 }))).toBe('3 rows shown of 268 gauged loops')
    expect(shownLine(1, totals({ loops: 268 }))).toBe('1 row shown of 268 gauged loops')
  })
})

describe('paging — the same predicate as the operator phone', () => {
  it('explains the paging set against the payload floor', () => {
    expect(
      pagingExplainer(
        response({ alert_min_severity: 'medium', totals: totals({ deficit: 8, paging: 3 }) }),
      ),
    ).toBe(
      '3 of 8 deficits clear the alert floor (severity medium and above) and would page.',
    )
  })

  it('says plainly when deficits exist but none page', () => {
    expect(
      pagingExplainer(
        response({ alert_min_severity: 'medium', totals: totals({ deficit: 5, paging: 0 }) }),
      ),
    ).toMatch(/stay off the operator's phone/)
  })

  it('says plainly when there are no deficits at all', () => {
    expect(pagingExplainer(response({ totals: totals({ deficit: 0, paging: 0 }) }))).toMatch(
      /No loop is in deficit/,
    )
  })

  it('annotates a paging row with the severity AND the floor', () => {
    const note = pagingNote(row({ state: 'deficit', severity: 'high', pages: true }), 'medium')
    expect(note).toBe('pages — deficit at high, at or above the medium alert floor')
  })

  it('annotates a sub-floor deficit as surfaced-but-silent', () => {
    expect(pagingNote(row({ state: 'deficit', severity: 'low' }), 'medium')).toBe(
      'does not page — low is below the medium alert floor',
    )
  })

  it('annotates nothing on an ok or ungauged row', () => {
    expect(pagingNote(row({ state: 'ok' }), 'medium')).toBeNull()
    expect(pagingNote(row({ state: 'ungauged', ratio: null }), 'medium')).toBeNull()
  })
})

describe('filter', () => {
  it('describes nothing when nothing is narrowed', () => {
    expect(describeFilter(EMPTY_FILTER)).toBeNull()
    // window_days is a baseline override, not a filter — it never narrows rows.
    expect(describeFilter({ ...EMPTY_FILTER, windowDays: 90 })).toBeNull()
  })

  it('names the active narrowing', () => {
    expect(describeFilter({ ...EMPTY_FILTER, scope: 'deficits' })).toBe('deficits-only')
    expect(describeFilter({ ...EMPTY_FILTER, scope: 'paging' })).toBe('paging-only')
    expect(describeFilter({ ...EMPTY_FILTER, loopClass: 'llm_latency' })).toBe('LLM latency')
    expect(describeFilter({ scope: 'paging', loopClass: 'judge_availability', windowDays: null })).toBe(
      'paging-only + judge availability',
    )
  })

  it('maps to the fetch options the API expects', () => {
    expect(gaugeQueryOptions(EMPTY_FILTER, 500)).toEqual({ limit: 500 })
    expect(gaugeQueryOptions({ scope: 'deficits', loopClass: null, windowDays: null }, 500)).toEqual({
      limit: 500,
      deficitsOnly: true,
    })
    expect(
      gaugeQueryOptions({ scope: 'paging', loopClass: 'llm_daily_burn', windowDays: 30 }, 200),
    ).toEqual({ limit: 200, pagingOnly: true, loopClass: 'llm_daily_burn', windowDays: 30 })
  })
})

describe('evidenceFields', () => {
  it('renders whatever arrived, key-sorted, without assuming a shape', () => {
    expect(
      evidenceFields({
        observed_gap_minutes: 812.44449,
        bar_minutes: 300,
        cron: '*/30 * * * *',
        stale: true,
        last_ok: null,
        window: { days: 14 },
      }),
    ).toEqual([
      { key: 'bar_minutes', value: '300' },
      { key: 'cron', value: '*/30 * * * *' },
      { key: 'last_ok', value: 'null' },
      { key: 'observed_gap_minutes', value: '812.444' },
      { key: 'stale', value: 'true' },
      { key: 'window', value: '{"days":14}' },
    ])
  })

  it('handles an empty evidence bag', () => {
    expect(evidenceFields({})).toEqual([])
  })
})

describe('gaugeErrorText', () => {
  it('surfaces the server detail on an ApiError', () => {
    expect(gaugeErrorText(new ApiError(500, { detail: 'pg down' }))).toBe('HTTP 500 — pg down')
    expect(gaugeErrorText(new ApiError(503, 'unavailable'))).toBe('HTTP 503 — unavailable')
    expect(gaugeErrorText(new ApiError(401, null))).toBe('HTTP 401')
  })

  it('falls back to the message of a plain error', () => {
    expect(gaugeErrorText(new Error('network down'))).toBe('network down')
  })
})
