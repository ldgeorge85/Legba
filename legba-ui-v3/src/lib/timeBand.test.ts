import { describe, expect, it } from 'vitest'
import {
  BAND_PRESETS,
  MIN_WINDOW_MS,
  activePreset,
  activityHistogram,
  centreWindow,
  forecastTicks,
  keyStepMs,
  moveWindow,
  pastDomain,
  playStep,
  resizeWindow,
  windowWords,
} from './timeBand'
import type { TemporalRow } from './temporalLayers'

const HOUR = 3_600_000
const DAY = 24 * HOUR
const NOW = Date.UTC(2026, 10, 25, 9, 0)

describe('the band offers three spans', () => {
  it('24h, 7d and 30d; no others', () => {
    expect(BAND_PRESETS.map((p) => p.id)).toEqual(['24h', '7d', '30d'])
  })
  it('a preset is active only when the window has its width and ends now', () => {
    expect(activePreset({ startMs: NOW - 7 * DAY, endMs: NOW }, NOW)?.id).toBe('7d')
    expect(activePreset({ startMs: NOW - 8 * DAY, endMs: NOW - DAY }, NOW)).toBeNull()
    expect(activePreset({ startMs: NOW - 5 * DAY, endMs: NOW }, NOW)).toBeNull()
  })
})

describe('the past side', () => {
  it('reaches at least a week, four spans, and always holds the window start', () => {
    expect(pastDomain(DAY, NOW - DAY, NOW)).toEqual({ lo: NOW - 7 * DAY, hi: NOW })
    expect(pastDomain(30 * DAY, NOW - 30 * DAY, NOW).lo).toBe(NOW - 120 * DAY)
    expect(pastDomain(DAY, NOW - 20 * DAY, NOW).lo).toBe(NOW - 20 * DAY)
  })
  it('the keyboard step is whole hours', () => {
    expect(keyStepMs(NOW - 7 * DAY, NOW) % HOUR).toBe(0)
    expect(keyStepMs(NOW - HOUR, NOW)).toBe(HOUR)
  })
})

describe('brush moves', () => {
  const lo = NOW - 7 * DAY
  const w = { startMs: NOW - 2 * DAY, endMs: NOW - DAY }
  it('moving keeps the width and stays inside the band', () => {
    expect(moveWindow(w, -HOUR, lo, NOW)).toEqual({ startMs: w.startMs - HOUR, endMs: w.endMs - HOUR })
    expect(moveWindow(w, 10 * DAY, lo, NOW)).toEqual({ startMs: NOW - DAY, endMs: NOW })
    expect(moveWindow(w, -10 * DAY, lo, NOW)).toEqual({ startMs: lo, endMs: lo + DAY })
  })
  it('resizing moves one edge and never goes below the minimum width', () => {
    expect(resizeWindow(w, HOUR, lo, NOW)).toEqual({ startMs: w.startMs, endMs: w.endMs + HOUR })
    expect(resizeWindow(w, -HOUR, lo, NOW, 'start')).toEqual({ startMs: w.startMs - HOUR, endMs: w.endMs })
    const thin = resizeWindow(w, -5 * DAY, lo, NOW)
    expect(thin.endMs - thin.startMs).toBe(MIN_WINDOW_MS)
  })
  it('centring puts the middle of the window at the click', () => {
    const c = centreWindow(w, NOW - 4 * DAY, lo, NOW)
    expect((c.startMs + c.endMs) / 2).toBe(NOW - 4 * DAY)
  })
})

describe('the window in words', () => {
  it('a window that ends now reads as the last stretch', () => {
    expect(windowWords({ startMs: NOW - 7 * DAY, endMs: NOW }, NOW)).toBe('last 7 days, to now')
    expect(windowWords({ startMs: NOW - DAY, endMs: NOW }, NOW)).toBe('last 24 hours, to now')
  })
  it('a past window names both ends', () => {
    expect(windowWords({ startMs: NOW - 4 * DAY, endMs: NOW - DAY }, NOW)).toBe('21 Nov 09:00 to 24 Nov 09:00 UTC (3 days)')
  })
})

describe('the histogram', () => {
  const row = (id: string, validMs: number | null, recordMs: number | null): TemporalRow => ({
    id,
    source: 'signals',
    kind: 'signal',
    title: id,
    validMs,
    recordMs,
    subtitle: '',
  })
  it('counts by when it happened, or by when recorded, and leaves undated rows out', () => {
    const rows = [row('a', NOW - HOUR, NOW - HOUR), row('b', NOW - 3 * DAY, NOW - HOUR), row('c', null, NOW - 2 * HOUR)]
    const happened = activityHistogram(rows, NOW - 7 * DAY, NOW, 'valid', 7)
    expect(happened.reduce((a, b) => a + b, 0)).toBe(2)
    expect(happened[6]).toBe(1)
    expect(happened[4]).toBe(1)
    const recorded = activityHistogram(rows, NOW - 7 * DAY, NOW, 'record', 7)
    expect(recorded).toEqual([0, 0, 0, 0, 0, 0, 3])
  })
})

describe('forecast ticks', () => {
  const f = (slug: string, dueMs: number, status = 'open', subject_keys: string[] = []) => ({
    slug,
    question: `Q ${slug}?`,
    due_at: new Date(dueMs).toISOString(),
    status,
    subject_keys,
  })
  it('open questions due inside the horizon, soonest first; the rest are counted as later', () => {
    const { ticks, later } = forecastTicks(
      [f('b', NOW + 10 * DAY), f('a', NOW + 2 * DAY), f('x', NOW + 2 * DAY, 'resolved'), f('old', NOW - DAY), f('far', NOW + 90 * DAY)],
      NOW,
    )
    expect(ticks.map((t) => t.slug)).toEqual(['a', 'b'])
    expect(ticks[0].frac).toBeCloseTo(2 / 30)
    expect(later).toBe(1)
  })
  it('a subject narrows the ticks to its own questions', () => {
    const { ticks } = forecastTicks([f('a', NOW + DAY, 'open', ['brazil']), f('b', NOW + DAY, 'open', ['chile'])], NOW, { subject: 'brazil' })
    expect(ticks.map((t) => t.slug)).toEqual(['a'])
  })
})

describe('play', () => {
  it('steps a day later and stops at now', () => {
    const w = { startMs: NOW - 3 * DAY, endMs: NOW - 2 * DAY }
    expect(playStep(w, NOW)).toEqual({ window: { startMs: NOW - 2 * DAY, endMs: NOW - DAY }, done: false })
    expect(playStep({ startMs: NOW - 2 * DAY, endMs: NOW - HOUR }, NOW)).toEqual({
      window: { startMs: NOW - 2 * DAY + HOUR, endMs: NOW },
      done: true,
    })
  })
})
