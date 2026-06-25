import { describe, it, expect } from 'vitest'
import {
  toClaim,
  toClaims,
  claimSeverities,
  corroborationScore,
  corroborationSources,
  type FindingRow,
} from './claimsModel'

function finding(over: Partial<FindingRow> = {}): FindingRow {
  return {
    id: 'f1',
    kind: 'finding',
    title: 'Troops massing near the border',
    body: 'Multiple sources report movement.',
    confidence: 0.72,
    severity: 'high',
    data: {},
    target_id: 'brazil',
    analyst_id: 'inline.brazil',
    produced_at: '2026-06-02T00:00:00Z',
    derived_from: ['s1', 's2'],
    ...over,
  }
}

describe('corroboration extractors', () => {
  it('reads corroboration_score', () => {
    expect(corroborationScore({ corroboration_score: 0.8 })).toBe(0.8)
    expect(corroborationScore({})).toBeNull()
  })
  it('reads source count from any known key', () => {
    expect(corroborationSources({ corroboration_sources: 3 })).toBe(3)
    expect(corroborationSources({ independent_sources: 4 })).toBe(4)
    expect(corroborationSources({ corroboration_count: 2 })).toBe(2)
    expect(corroborationSources({})).toBeNull()
  })
  it('coerces numeric strings and rejects junk', () => {
    expect(corroborationSources({ corroboration_sources: '5' })).toBe(5)
    expect(corroborationScore({ corroboration_score: 'NaN' })).toBeNull()
  })
})

describe('toClaim', () => {
  it('maps a finding to a claim with corroboration', () => {
    const c = toClaim(finding({ data: { corroboration_score: 0.6, corroboration_sources: 3 } }))
    expect(c.statement).toBe('Troops massing near the border')
    expect(c.confidence).toBe(0.72)
    expect(c.corroborationScore).toBe(0.6)
    expect(c.corroborationSources).toBe(3)
    expect(c.derived_from).toEqual(['s1', 's2'])
  })

  it('leaves corroboration null when unscored', () => {
    const c = toClaim(finding({ data: {} }))
    expect(c.corroborationScore).toBeNull()
    expect(c.corroborationSources).toBeNull()
  })

  it('defaults a null severity to unknown', () => {
    expect(toClaim(finding({ severity: null })).severity).toBe('unknown')
  })
})

describe('claimSeverities', () => {
  it('returns distinct sorted severities', () => {
    const claims = toClaims([
      finding({ id: 'a', severity: 'high' }),
      finding({ id: 'b', severity: 'critical' }),
      finding({ id: 'c', severity: 'high' }),
    ])
    expect(claimSeverities(claims)).toEqual(['critical', 'high'])
  })
})
