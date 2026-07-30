/**
 * Tests for provenance (P4-5) — the live|fallback|absent enum resolution + the
 * ProvenanceCard grammar shaping. Pure logic, no DOM.
 */
import { describe, it, expect } from 'vitest'
import {
  PROVENANCE_META,
  describeProvenance,
  provenanceOf,
  resolveNumberProvenance,
} from './provenance'

describe('resolveNumberProvenance', () => {
  it('present value with no fallback signal → live', () => {
    expect(resolveNumberProvenance({ value: 42 })).toBe('live')
    expect(resolveNumberProvenance({ value: 0 })).toBe('live') // zero is a real number
  })
  it('missing / non-finite value → absent', () => {
    expect(resolveNumberProvenance({ value: null })).toBe('absent')
    expect(resolveNumberProvenance({ value: undefined })).toBe('absent')
    expect(resolveNumberProvenance({ value: NaN })).toBe('absent')
  })
  it('present value + explicit fallback signal → fallback', () => {
    expect(resolveNumberProvenance({ value: 42, fallback: true })).toBe('fallback')
  })
  it('NEVER fabricates fallback — a present value with fallback undefined/false is live', () => {
    expect(resolveNumberProvenance({ value: 42, fallback: false })).toBe('live')
    expect(resolveNumberProvenance({ value: 42, fallback: undefined })).toBe('live')
  })
  it('an absent value dominates even an explicit fallback flag', () => {
    expect(resolveNumberProvenance({ value: null, fallback: true })).toBe('absent')
  })
  it('treatAsAbsent forces absent for a sentinel value', () => {
    expect(resolveNumberProvenance({ value: 42, treatAsAbsent: true })).toBe('absent')
  })
})

describe('provenanceOf', () => {
  it('is the bare-value convenience for present→live / empty→absent', () => {
    expect(provenanceOf(5)).toBe('live')
    expect(provenanceOf(null)).toBe('absent')
    expect(provenanceOf(5, true)).toBe('fallback')
  })
})

describe('PROVENANCE_META', () => {
  it('has a label + tone for every state', () => {
    expect(PROVENANCE_META.live.tone).toBe('ok')
    expect(PROVENANCE_META.fallback.tone).toBe('warn')
    expect(PROVENANCE_META.absent.tone).toBe('muted')
    for (const s of ['live', 'fallback', 'absent'] as const) {
      expect(PROVENANCE_META[s].label).toBeTruthy()
      expect(PROVENANCE_META[s].title).toBeTruthy()
    }
  })
})

describe('describeProvenance', () => {
  it('maps source/freshness/confidence and is live when dated', () => {
    const f = describeProvenance({
      purpose: 'per-analyst critic scores',
      source: 'eval_country_scorecard',
      produced_at: '2026-07-24T00:00:00Z',
      effective_confidence: 0.82,
      derived_from: ['abc'],
    })
    expect(f.purpose).toBe('per-analyst critic scores')
    expect(f.source).toBe('eval_country_scorecard')
    expect(f.freshnessAt).toBe('2026-07-24T00:00:00Z')
    expect(f.confidence).toBe('confidence 82%')
    expect(f.state).toBe('live')
    expect(f.limitations).toEqual([]) // has lineage, verified, no caveats
  })

  it('falls back source→analyst_id and freshness→fetched_at→created_at', () => {
    const f = describeProvenance({ analyst_id: 'country_assessor', fetched_at: '2026-07-01T00:00:00Z' })
    expect(f.source).toBe('country_assessor')
    expect(f.freshnessAt).toBe('2026-07-01T00:00:00Z')
  })

  it('surfaces the faithfulness verify reading over a raw confidence', () => {
    const f = describeProvenance({
      produced_at: '2026-07-24T00:00:00Z',
      confidence: 0.9,
      verification: { faithfulness_score: 0.71, judge_status: 'supported' },
    })
    expect(f.confidence).toBe('faithfulness 71% · supported')
  })

  it('stamps the honest unverified caveat for a structural analyst', () => {
    const f = describeProvenance({
      produced_at: '2026-07-24T00:00:00Z',
      verify_exempt: 'structural',
      derived_from: ['x'],
    })
    expect(f.confidence).toBe('unverified — structural')
    expect(f.limitations).toContain(
      'not routed through the faithfulness verify pass (structural analyst)',
    )
  })

  it('stamps the grounding-verified caveat for a structural-verified analyst (C2b)', () => {
    const f = describeProvenance({
      produced_at: '2026-07-27T00:00:00Z',
      verify_exempt: 'structural-verified',
      derived_from: ['x'],
    })
    expect(f.confidence).toBe('structural — recomputation-verified')
    expect(f.limitations).toContain(
      'deterministic structural claims re-derived and matched — not routed through the ' +
        'faithfulness verify pass (structural analyst)',
    )
  })

  it('records a no-lineage limitation when derived_from is empty + appends extras', () => {
    const f = describeProvenance({
      produced_at: '2026-07-24T00:00:00Z',
      derived_from: [],
      extraLimitations: ['preview route — not the everyday surface'],
    })
    expect(f.limitations).toContain('no upstream lineage recorded')
    expect(f.limitations).toContain('preview route — not the everyday surface')
  })

  it('is absent when nothing dates the datum, fallback only on explicit signal', () => {
    expect(describeProvenance({}).state).toBe('absent')
    expect(describeProvenance({ absent: true, produced_at: '2026-07-24T00:00:00Z' }).state).toBe('absent')
    expect(describeProvenance({ produced_at: '2026-07-24T00:00:00Z', fallback: true }).state).toBe('fallback')
  })
})
