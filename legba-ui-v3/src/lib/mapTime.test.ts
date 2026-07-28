import { describe, expect, it } from 'vitest'
import {
  SPAN_PRESETS,
  clampWindow,
  filterByWindow,
  isLiveWindow,
  sliderStep,
  spanPresetFor,
  windowForSpan,
  windowFraction,
  withinWindow,
} from './mapTime'

const HOUR = 3_600_000
const DAY = 24 * HOUR

describe('mapTime — span presets', () => {
  it('exposes 6h/24h/7d/30d shortest→longest', () => {
    expect(SPAN_PRESETS.map((p) => p.id)).toEqual(['6h', '24h', '7d', '30d'])
    for (let i = 1; i < SPAN_PRESETS.length; i++) {
      expect(SPAN_PRESETS[i].ms).toBeGreaterThan(SPAN_PRESETS[i - 1].ms)
    }
  })

  it('spanPresetFor matches an exact span, null otherwise', () => {
    expect(spanPresetFor(DAY)?.id).toBe('24h')
    expect(spanPresetFor(7 * DAY)?.id).toBe('7d')
    expect(spanPresetFor(DAY + 1)).toBeNull()
  })

  it('windowForSpan reaches back exactly spanMs from now', () => {
    const now = 1_000_000_000
    expect(windowForSpan(now, DAY)).toEqual({ startMs: now - DAY, endMs: now })
  })
})

describe('mapTime — clampWindow', () => {
  it('keeps a valid window untouched', () => {
    expect(clampWindow(10, 20, 0, 100)).toEqual({ startMs: 10, endMs: 20 })
  })

  it('clamps both ends into the outer bounds', () => {
    expect(clampWindow(-5, 200, 0, 100)).toEqual({ startMs: 0, endMs: 100 })
  })

  it('swaps thumbs when start is dragged past end', () => {
    expect(clampWindow(80, 30, 0, 100)).toEqual({ startMs: 30, endMs: 80 })
  })
})

describe('mapTime — window membership + filtering', () => {
  it('withinWindow is inclusive on both ends', () => {
    expect(withinWindow(10, 10, 20)).toBe(true)
    expect(withinWindow(20, 10, 20)).toBe(true)
    expect(withinWindow(9, 10, 20)).toBe(false)
    expect(withinWindow(21, 10, 20)).toBe(false)
  })

  it('filterByWindow drives the map slice — keeps only in-window items', () => {
    const items = [
      { ts: 5, id: 'a' },
      { ts: 10, id: 'b' },
      { ts: 15, id: 'c' },
      { ts: 25, id: 'd' },
    ]
    expect(filterByWindow(items, 10, 20).map((i) => i.id)).toEqual(['b', 'c'])
    // an empty window keeps nothing
    expect(filterByWindow(items, 100, 200)).toEqual([])
  })
})

describe('mapTime — slider helpers', () => {
  it('sliderStep is ~span/500, floored at one minute', () => {
    expect(sliderStep(30 * DAY)).toBe(Math.round((30 * DAY) / 500))
    expect(sliderStep(HOUR)).toBe(60_000) // small span floors at 1 min
  })

  it('windowFraction maps a ts to [0,1] within the outer bounds', () => {
    expect(windowFraction(50, 0, 100)).toBe(0.5)
    expect(windowFraction(-10, 0, 100)).toBe(0)
    expect(windowFraction(150, 0, 100)).toBe(1)
    expect(windowFraction(5, 10, 10)).toBe(0) // degenerate bounds
  })

  it('isLiveWindow is true only within tolerance of now', () => {
    const now = 1_000_000
    expect(isLiveWindow(now - 30_000, now, 60_000)).toBe(true)
    expect(isLiveWindow(now - 120_000, now, 60_000)).toBe(false)
  })
})
