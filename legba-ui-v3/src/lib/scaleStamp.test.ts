import { describe, it, expect } from 'vitest'
import {
  readScaleStamp,
  scaleStampLabel,
  scaleStampTooltip,
  UNSTAMPED_LABEL,
} from '@/lib/scaleStamp'

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
