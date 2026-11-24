import { describe, it, expect } from 'vitest'
import {
  readScaleStamp,
  scaleStampLabel,
  scaleStampTooltip,
  UNSTAMPED_LABEL,
  UNDECLARED_LABEL,
  STAMP_ERA_START,
} from '@/lib/scaleStamp'
import live from '@/lib/__fixtures__/findings.live-2026-10-08.json'

describe('readScaleStamp', () => {
  it('reads both stamps off a flattened projection row', () => {
    const s = readScaleStamp({
      scale_version: 'intensity/2026-08',
      method_version: 'situation_clustering/2026-09.1',
    })
    expect(s).toEqual({
      scale: 'intensity/2026-08',
      method: 'situation_clustering/2026-09.1',
      state: 'full',
    })
  })

  it('reads both stamps out of a raw payload nested under data', () => {
    const s = readScaleStamp({
      data: {
        scale_version: 'acute_probability/2026-07',
        method_version: 'forecast_acute/2026-09.1',
      },
    })
    expect(s.scale).toBe('acute_probability/2026-07')
    expect(s.method).toBe('forecast_acute/2026-09.1')
    expect(s.state).toBe('full')
  })

  it('prefers the top-level field over the nested one — the projection the server chose', () => {
    const s = readScaleStamp({
      scale_version: 'intensity/2026-08',
      data: { scale_version: 'intensity/2026-07' },
    })
    expect(s.scale).toBe('intensity/2026-08')
  })

  it('is method-only when the row carries a revision but no scale', () => {
    const s = readScaleStamp({ method_version: 'scorecard_banding/2026-09.1' })
    expect(s.state).toBe('method-only')
    expect(s.scale).toBeNull()
  })

  it('is unstamped when the row carries neither — never a guessed version', () => {
    const s = readScaleStamp({ intensity_score: 59 })
    expect(s).toEqual({ scale: null, method: null, state: 'unstamped' })
  })

  it('treats an empty string as absence, not as a version', () => {
    const s = readScaleStamp({ scale_version: '', method_version: '' })
    expect(s.state).toBe('unstamped')
  })

  it('survives a null, undefined or non-object row rather than throwing', () => {
    for (const row of [null, undefined, 42, 'x']) {
      expect(readScaleStamp(row).state).toBe('unstamped')
    }
  })

  it('names a scale-without-method row rather than mislabelling it method-only', () => {
    // Our own writers cannot produce this (the Python guard pins that every
    // module declaring a scale declares a method); it is named so an
    // unexpected row is visible rather than quietly wrong.
    expect(readScaleStamp({ scale_version: 'intensity/2026-08' }).state).toBe('scale-only')
  })
})

describe('scaleStampLabel', () => {
  it('joins both halves with the separator the reader sees', () => {
    expect(
      scaleStampLabel({
        scale: 'intensity/2026-08',
        method: 'situation_clustering/2026-09.1',
        state: 'full',
      }),
    ).toBe('intensity/2026-08 · situation_clustering/2026-09.1')
  })

  it('renders the half that exists, never fabricating the other', () => {
    expect(
      scaleStampLabel({ scale: null, method: 'forecast_acute/2026-09.1', state: 'method-only' }),
    ).toBe('forecast_acute/2026-09.1')
  })

  it('renders an unstamped row as the dated absence, not as a dash or a zero', () => {
    expect(scaleStampLabel({ scale: null, method: null, state: 'unstamped' })).toBe(
      UNSTAMPED_LABEL,
    )
    expect(UNSTAMPED_LABEL).toContain('pre-2026-09')
  })
})

describe('scaleStampTooltip', () => {
  it('explains what a scale change means for a comparison', () => {
    const tip = scaleStampTooltip({
      scale: 'intensity/2026-08',
      method: 'situation_clustering/2026-09.1',
      state: 'full',
    })
    expect(tip).toContain('intensity/2026-08')
    expect(tip).toContain('compared directly')
    expect(tip).toContain('situation_clustering/2026-09.1')
  })

  it('says why an unstamped row has no frame, and that today’s is not assumed', () => {
    const tip = scaleStampTooltip({ scale: null, method: null, state: 'unstamped' })
    expect(tip).toContain('before the platform stamped scales')
    expect(tip).toContain('not assumed')
  })
})

// ---------------------------------------------------------------------------
// W11-F9 (X1/D1) — the /findings row shape, off the live registry
// ---------------------------------------------------------------------------

describe('readScaleStamp on a /findings row (W11-F9)', () => {
  it('reads the stamp where /findings carries it: row.data.data (the finding envelope)', () => {
    // The live indicator_tracker row: `row.data` is the analyst_outputs.data
    // column, i.e. the whole envelope; the instrument's stamp rides the
    // envelope's own `data`. The old reader stopped one level short.
    const row = live.instrument
    expect((row.data as Record<string, unknown>).method_version).toBeUndefined()
    const s = readScaleStamp(row)
    expect(s).toEqual({
      scale: 'indicator_status/2026-07',
      method: 'indicator_tracker/2026-09.1',
      state: 'full',
    })
    expect(scaleStampLabel(s)).toBe('indicator_status/2026-07 · indicator_tracker/2026-09.1')
  })

  it('never calls the live 2026-10-08 Brazil composition "pre-2026-09"', () => {
    // The composition carries no stamp at any depth — its producer is not an
    // instrument on a declared scale. Dated after the stamps began, that is
    // `undeclared`, not the pre-era absence.
    const row = live.composition
    expect(row.produced_at.startsWith('2026-10-08')).toBe(true)
    const s = readScaleStamp(row)
    expect(s).toEqual({ scale: null, method: null, state: 'undeclared' })
    expect(scaleStampLabel(s)).toBe(UNDECLARED_LABEL)
    expect(scaleStampLabel(s)).not.toContain('pre-2026-09')
    expect(scaleStampTooltip(s)).toContain('after the platform began stamping')
  })

  it('keeps the dated pre-era absence for a stampless row written before the stamps', () => {
    const before = new Date(Date.parse(STAMP_ERA_START) - 60_000).toISOString()
    expect(readScaleStamp({ produced_at: before, data: { data: {} } }).state).toBe('unstamped')
    expect(readScaleStamp({ produced_at: STAMP_ERA_START }).state).toBe('undeclared')
  })

  it('a top-level stamp still wins over the envelope one', () => {
    const s = readScaleStamp({
      method_version: 'a/1',
      data: { data: { method_version: 'b/2', scale_version: 'x/1' } },
    })
    expect(s.method).toBe('a/1')
    expect(s.scale).toBe('x/1')
  })
})
