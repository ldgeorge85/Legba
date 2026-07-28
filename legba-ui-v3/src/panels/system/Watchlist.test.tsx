/**
 * P5-6 — Watchlist panel pure helpers: the form→pattern builder and the
 * stored-pattern summary. (The CRUD + matching behavior is covered by the
 * backend suite `tests/data_pkg/test_watchlist.py`; the panel is a thin
 * management surface over it.)
 */

import { describe, it, expect } from 'vitest'
import { buildPattern, patternSummary } from './Watchlist'

describe('buildPattern', () => {
  it('entity → {name}', () => {
    expect(buildPattern('entity', ' Wagner Group ')).toEqual({ name: 'Wagner Group' })
    expect(buildPattern('entity', '')).toBeNull()
  })

  it('text → {query}, honest 2-char minimum (mirrors the route)', () => {
    expect(buildPattern('text', 'Strait of Hormuz')).toEqual({ query: 'Strait of Hormuz' })
    expect(buildPattern('text', 'x')).toBeNull()
  })

  it('geo ISO2 list → {countries[]} uppercased', () => {
    expect(buildPattern('geo', 'ir, IQ')).toEqual({ countries: ['IR', 'IQ'] })
  })

  it('geo three numbers → {lat, lon, radius_km}', () => {
    expect(buildPattern('geo', '36.3, 43.1, 50')).toEqual({ lat: 36.3, lon: 43.1, radius_km: 50 })
  })

  it('geo junk → null (never a silently-empty watch)', () => {
    expect(buildPattern('geo', 'Iran')).toBeNull()
    expect(buildPattern('geo', '36.3, 43.1')).toBeNull()
  })
})

describe('patternSummary', () => {
  it('summarizes each stored kind', () => {
    expect(patternSummary('entity', { name: 'Wagner Group' })).toBe('Wagner Group')
    expect(patternSummary('text', { query: 'hormuz' })).toBe('“hormuz”')
    expect(patternSummary('geo', { countries: ['IR', 'IQ'] })).toBe('IR, IQ')
    expect(patternSummary('geo', { lat: 36.3, lon: 43.1, radius_km: 50 })).toBe('36.3, 43.1 ±50 km')
  })

  it('degrades honestly on junk', () => {
    expect(patternSummary('entity', {})).toBe('(empty)')
    expect(patternSummary('geo', {})).toBe('(empty)')
  })
})
