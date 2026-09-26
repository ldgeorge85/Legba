/**
 * Unit tests for the layer-divergence model (7b-v).
 *
 * What these hold:
 *   * the five pair states are decided in the documented order, and `thin`
 *     never hides behind `below_threshold`;
 *   * a null z is a null — never 0 — in the format, in the side, and in the
 *     sparkline, where it BREAKS the track instead of bridging it;
 *   * both layer lines are drawn against ONE shared count axis;
 *   * the z track is always scaled wide enough to show the firing threshold,
 *     so a quiet pair and a nearly-firing one cannot draw identically;
 *   * `measured: false` and "no run yet" produce different summary lines.
 */

import { describe, it, expect } from 'vitest'
import {
  UNMEASURED,
  deskLine,
  formatZ,
  lastDayThin,
  lastPoint,
  louderLayer,
  orderDesks,
  pairState,
  pairedSparkline,
  summaryLine,
  zSide,
} from './layerDivergence'
import type {
  LayerDivergenceDesk,
  LayerDivergenceFired,
  LayerDivergencePair,
  LayerDivergenceResponse,
  LayerDivergenceSeriesPoint,
} from '@/lib/api'

function point(
  day: string,
  a: number,
  b: number,
  z: number | null,
  logRatio = -0.36,
): LayerDivergenceSeriesPoint {
  return {
    day,
    a,
    b,
    log_ratio: logRatio,
    blank: a === 0 && b === 0,
    z,
    baseline: { n: 14, centre: -0.44, mad: 0.75, scale: 1.12, scale_floored: false },
  }
}

function evaluablePair(
  over: Partial<LayerDivergencePair> = {},
): LayerDivergencePair {
  return {
    pair_id: 'narrative_control',
    layer_a: 'domestic_press',
    layer_b: 'foreign_press',
    evaluable: true,
    no_fire_reason: 'below_threshold',
    series: [
      point('2026-09-21', 7, 14, null),
      point('2026-09-22', 9, 17, 0.31),
      point('2026-09-23', 11, 12, 0.98),
      point('2026-09-24', 8, 19, 0.07),
    ],
    ...over,
  }
}

function excludedPair(): LayerDivergencePair {
  return {
    pair_id: 'regime_public_gap',
    layer_a: 'official',
    layer_b: 'social_digest',
    evaluable: false,
    no_fire_reason: 'aperture_excluded',
    excluded_layers: [
      {
        layer: 'social_digest',
        state: 'absent',
        reason: 'curated: the Telegram list carries no Argentine channels',
      },
    ],
  }
}

function fired(over: Partial<LayerDivergenceFired> = {}): LayerDivergenceFired {
  return {
    finding_id: 'f-1',
    produced_at: '2026-09-24T05:41:00Z',
    pair_id: 'narrative_control',
    direction: 'widening',
    severity: 'moderate',
    day: '2026-09-24',
    z: -2.77,
    thin: true,
    ...over,
  }
}

function desk(over: Partial<LayerDivergenceDesk> = {}): LayerDivergenceDesk {
  return {
    target_id: 'country_g20_ar',
    country: 'AR',
    map_version: 'layer_map_ar.v1',
    sources_mapped: 49,
    rows_scanned: 227,
    rows_truncated: false,
    aperture: {
      present: ['domestic_press', 'foreign_press', 'physical', 'public_data'],
      absent: [{ layer: 'official', reason: 'no Argentine government feed is registered' }],
      unmeasured: [],
      undeclared: [],
    },
    counts_suppressed_by_aperture: {},
    layers: {},
    pairs: [evaluablePair(), excludedPair()],
    fired: null,
    ...over,
  }
}

function response(over: Partial<LayerDivergenceResponse> = {}): LayerDivergenceResponse {
  return {
    measured: true,
    generated_at: '2026-09-24T21:00:00Z',
    reader_version: '2026-09/p6-l2',
    unit_sentence: 'Per country, the same stack of source layers is counted per day…',
    as_of: '2026-09-24',
    receipt_run_id: 'run-1',
    run_started_at: '2026-09-24T05:40:00Z',
    method_version: 'layer_divergence/2026-09.1',
    payload_schema: 'layer_divergence.v1',
    classification_audit: 'unaudited: … (SEAMS #60)',
    window_days: 28,
    baseline_days: 14,
    z_threshold: 2.0,
    mad_floor: 0.2,
    consecutive_days: 2,
    thin_min_per_day: 5,
    layer_vocab: [
      'official', 'domestic_press', 'foreign_press', 'social_digest',
      'public_data', 'physical',
    ],
    pairs_declared: [],
    desks: [desk()],
    desks_unresolved: [],
    warnings: [],
    ...over,
  }
}

describe('pair state', () => {
  it('a pair the aperture shut out is never "below threshold"', () => {
    expect(pairState(excludedPair(), null, 5)).toBe('aperture_excluded')
  })

  it('a fired pair wins over every quiet state', () => {
    expect(pairState(evaluablePair(), fired(), 5)).toBe('fired')
  })

  it('thin is decided on the LAST measured day and outranks below_threshold', () => {
    const thin = evaluablePair({
      series: [point('2026-09-23', 9, 12, 0.4), point('2026-09-24', 3, 19, 0.07)],
    })
    expect(lastDayThin(thin, 5)).toBe(true)
    expect(pairState(thin, null, 5)).toBe('thin')
    // Same pair with the floor unknown: not thin, and not "not thin" either.
    expect(lastDayThin(thin, null)).toBeNull()
    expect(pairState(thin, null, null)).toBe('below_threshold')
  })

  it('an evaluated pair with another named reason keeps the generic badge', () => {
    const p = evaluablePair({ no_fire_reason: 'baseline_thin' })
    expect(pairState(p, null, 5)).toBe('evaluable')
    expect(p.no_fire_reason).toBe('baseline_thin')
  })

  it('a fired badge is not stolen by a fire on a DIFFERENT pair', () => {
    expect(
      pairState(evaluablePair(), fired({ pair_id: 'credibility_gap' }), 5),
    ).toBe('below_threshold')
  })
})

describe('honest nulls', () => {
  it('formatZ renders a null as absence, never 0.00', () => {
    expect(formatZ(null)).toBe(UNMEASURED)
    expect(formatZ(undefined)).toBe(UNMEASURED)
    expect(formatZ(NaN)).toBe(UNMEASURED)
    expect(formatZ(-2.7712)).toBe('z -2.77σ')
    expect(formatZ(1.5)).toBe('z +1.50σ')
    // A real zero is a real measurement and prints as one.
    expect(formatZ(0)).toBe('z 0.00σ')
  })

  it('zSide refuses a side for a null or an exact zero', () => {
    expect(zSide(null)).toBeNull()
    expect(zSide(0)).toBeNull()
    expect(zSide(1.2)).toBe('above')
    expect(zSide(-1.2)).toBe('below')
  })

  it('lastPoint is null for a pair with no series', () => {
    expect(lastPoint(excludedPair())).toBeNull()
    expect(lastPoint(evaluablePair())?.day).toBe('2026-09-24')
  })

  it('louderLayer follows the log-ratio sign and declines a flat day', () => {
    expect(louderLayer(evaluablePair())?.layer).toBe('foreign_press')
    const flipped = evaluablePair({
      series: [point('2026-09-24', 30, 4, 0.5, 2.2)],
    })
    expect(louderLayer(flipped)?.layer).toBe('domestic_press')
    const flat = evaluablePair({ series: [point('2026-09-24', 5, 5, 0, 0)] })
    expect(louderLayer(flat)).toBeNull()
  })
})

describe('paired sparkline', () => {
  it('draws both layers against ONE shared count axis', () => {
    const geo = pairedSparkline(evaluablePair().series, {
      width: 100,
      countHeight: 10,
      zHeight: 10,
      zThreshold: 2,
    })!
    expect(geo.countMax).toBe(19) // max over BOTH layers, not each its own
    // Day 3 on layer b is the maximum, so it sits at y = 0.
    expect(geo.bPoints.split(' ')[3]).toBe('100,0')
    // Layer a on the same day is 8/19 of the axis, from the bottom.
    expect(geo.aPoints.split(' ')[3]).toBe('100,5.79')
    expect(geo.days).toHaveLength(4)
  })

  it('breaks the z track across unscored days instead of bridging them', () => {
    const geo = pairedSparkline(
      [
        point('2026-09-21', 4, 4, null),
        point('2026-09-22', 4, 4, 1.0),
        point('2026-09-23', 4, 4, null),
        point('2026-09-24', 4, 4, 1.5),
      ],
      { width: 100, countHeight: 10, zHeight: 10, zThreshold: 2 },
    )!
    expect(geo.zSegments).toHaveLength(2)
    expect(geo.zSegments[0].split(' ')).toHaveLength(1)
    expect(geo.zSegments[1].split(' ')).toHaveLength(1)
  })

  it('has no z track at all when nothing was scored', () => {
    const geo = pairedSparkline(
      [point('2026-09-23', 1, 1, null), point('2026-09-24', 2, 2, null)],
      { zThreshold: 2 },
    )!
    expect(geo.zSegments).toEqual([])
    expect(geo.zMax).toBeNull()
    expect(geo.zThresholdY).toBeNull()
  })

  it('always scales the z track wide enough to show the threshold', () => {
    const quiet = pairedSparkline(
      [point('2026-09-23', 4, 4, 0.02), point('2026-09-24', 4, 4, 0.03)],
      { width: 100, countHeight: 10, zHeight: 10, zThreshold: 2 },
    )!
    // Scaled to the threshold, not to the tiny z — otherwise a 0.03 window and
    // a 1.9 window would draw the same shape.
    expect(quiet.zMax).toBe(2)
    expect(quiet.zThresholdY).toEqual({ hi: 0, lo: 10 })
  })

  it('returns null for a pair with no series at all', () => {
    expect(pairedSparkline(undefined)).toBeNull()
    expect(pairedSparkline([])).toBeNull()
  })

  it('never divides by zero on an all-zero window', () => {
    const geo = pairedSparkline(
      [point('2026-09-23', 0, 0, 0), point('2026-09-24', 0, 0, 0)],
      { width: 100, countHeight: 10, zHeight: 10, zThreshold: null },
    )!
    expect(geo.countMax).toBe(1)
    expect(Number.isFinite(geo.zMax as number)).toBe(true)
    expect(geo.zSegments[0]).toBe('0,5 100,5')
  })
})

describe('summaries and ordering', () => {
  it('a failed read and an empty engine say different things', () => {
    expect(summaryLine(response({ measured: false }))).toContain('failed read')
    const noRun = summaryLine(
      response({ receipt_run_id: null, desks: [], as_of: null }),
    )
    expect(noRun).toContain('no run yet')
    expect(noRun).not.toContain('failed read')
  })

  it('the measured summary carries the desk count, the fires and the as-of', () => {
    const line = summaryLine(
      response({ desks: [desk(), desk({ target_id: 'x', country: 'RU', fired: fired() })] }),
    )
    expect(line).toContain('2 desks')
    expect(line).toContain('1 with a fired pair')
    expect(line).toContain('28d window')
    expect(line).toContain('as of 2026-09-24 (UTC day)')
  })

  it('the desk line states the aperture, the map size and the scan size', () => {
    const line = deskLine(desk(), 5)
    expect(line).toContain('4 of 6 layers present')
    expect(line).toContain('49 sources mapped')
    expect(line).toContain('227 rows scanned')
    expect(line).toContain('1 pair not evaluable')
  })

  it('orders desks worst-first and stably by country underneath', () => {
    const quietRu = desk({ target_id: 'ru', country: 'RU', pairs: [evaluablePair()] })
    const quietCn = desk({ target_id: 'cn', country: 'CN', pairs: [evaluablePair()] })
    const firedIl = desk({ target_id: 'il', country: 'IL', fired: fired() })
    const excludedAr = desk({ target_id: 'ar', country: 'AR' })
    const out = orderDesks([quietRu, quietCn, excludedAr, firedIl], 5)
    expect(out.map((d) => d.target_id)).toEqual(['il', 'ar', 'cn', 'ru'])
  })
})
