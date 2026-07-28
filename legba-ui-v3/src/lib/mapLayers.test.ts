import { describe, expect, it } from 'vitest'
import { coMentionArcs, densityPoints, type ArcSignalInput } from './mapLayers'
import type { Severity } from '@/v4/world/types'

function sig(lat: number, lon: number, severity: Severity) {
  return { lat, lon, severity }
}

describe('mapLayers — densityPoints', () => {
  it('projects to [lon,lat] with severity weight', () => {
    const out = densityPoints([sig(10, 20, 'critical'), sig(0, 0, 'info')])
    expect(out).toEqual([
      { position: [20, 10], weight: 5, severity: 'critical' },
      { position: [0, 0], weight: 1, severity: 'info' },
    ])
  })

  it('drops non-finite coordinates', () => {
    const out = densityPoints([
      sig(Number.NaN, 20, 'high'),
      sig(10, Number.POSITIVE_INFINITY, 'high'),
      sig(1, 2, 'high'),
    ])
    expect(out).toHaveLength(1)
    expect(out[0].position).toEqual([2, 1])
  })
})

describe('mapLayers — coMentionArcs', () => {
  // A trivial resolver: place each ISO2 at a deterministic point.
  const resolve = (iso2: string): { lat: number; lon: number } | null => {
    const table: Record<string, { lat: number; lon: number }> = {
      US: { lat: 38, lon: -97 },
      IR: { lat: 32, lon: 53 },
      IQ: { lat: 33, lon: 44 },
      ZZ: { lat: 0, lon: 0 },
    }
    return table[iso2] ?? null
  }

  it('builds an arc per co-mentioned country pair, weighted by count', () => {
    const signals: ArcSignalInput[] = [
      { countries: ['US', 'IR'] },
      { countries: ['IR', 'US'] }, // same unordered pair — count 2
      { countries: ['US', 'IQ'] },
    ]
    const arcs = coMentionArcs(signals, resolve, { minCount: 1 })
    const usIr = arcs.find((a) => a.fromIso2 === 'IR' && a.toIso2 === 'US')
    expect(usIr?.count).toBe(2)
    expect(usIr?.source).toEqual([53, 32]) // [lon,lat] of IR
    expect(usIr?.target).toEqual([-97, 38])
    // strongest first
    expect(arcs[0].count).toBe(2)
  })

  it('ignores single-country signals and sub-threshold pairs', () => {
    const signals: ArcSignalInput[] = [
      { countries: ['US'] }, // no pair
      { countries: ['US', 'IR'] }, // count 1
    ]
    expect(coMentionArcs(signals, resolve, { minCount: 2 })).toEqual([])
  })

  it('drops a pair with an unplaceable endpoint (no fabricated point)', () => {
    const signals: ArcSignalInput[] = [{ countries: ['US', 'XX'] }]
    expect(coMentionArcs(signals, resolve, { minCount: 1 })).toEqual([])
  })

  it('dedupes repeated codes within one signal and ignores non-ISO2 tokens', () => {
    const signals: ArcSignalInput[] = [{ countries: ['us', 'US', 'zzz', 'IR'] }]
    const arcs = coMentionArcs(signals, resolve, { minCount: 1 })
    // 'us'/'US' fold to one; 'zzz' rejected → only the US|IR pair
    expect(arcs).toHaveLength(1)
    expect([arcs[0].fromIso2, arcs[0].toIso2].sort()).toEqual(['IR', 'US'])
  })

  it('caps the arc count', () => {
    const signals: ArcSignalInput[] = [
      { countries: ['US', 'IR'] },
      { countries: ['US', 'IQ'] },
      { countries: ['IR', 'IQ'] },
    ]
    expect(coMentionArcs(signals, resolve, { minCount: 1, maxArcs: 2 })).toHaveLength(2)
  })
})
