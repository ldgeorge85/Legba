/**
 * Unit tests for the UI-6 global-search model (DOM-free).
 */

import { describe, it, expect } from 'vitest'
import {
  DEFAULT_FACETS,
  kindCounts,
  normEntity,
  normFinding,
  normSituation,
  normSource,
  passesFacets,
  queryTerms,
  rankHits,
  scoreHit,
  toggleKind,
  type SearchHit,
} from './searchModel'

function hit(over: Partial<SearchHit> = {}): SearchHit {
  return {
    kind: 'finding',
    id: 'x',
    title: '',
    snippet: '',
    target_id: null,
    owner_tenant: null,
    severity: null,
    produced_at: null,
    score: 0,
    ...over,
  }
}

describe('normalisers', () => {
  it('normFinding coalesces title/body and lifts facets', () => {
    const h = normFinding({
      id: 'f1',
      title: 'Coup signal',
      body: 'details',
      target_id: 'brazil',
      severity: 'high',
      produced_at: '2026-06-03T00:00:00Z',
    })
    expect(h).toMatchObject({ kind: 'finding', id: 'f1', title: 'Coup signal', target_id: 'brazil', severity: 'high' })
  })

  it('normSituation prefers summary for title and opened_at for time', () => {
    const h = normSituation({ id: 's1', summary: 'escalating', opened_at: '2026-01-01T00:00:00Z' })
    expect(h.kind).toBe('situation')
    expect(h.title).toBe('escalating')
    expect(h.produced_at).toBe('2026-01-01T00:00:00Z')
  })

  it('normEntity falls back through name/label and source defaults', () => {
    expect(normEntity({ entity_id: 'e1', label: 'Acme Corp' }).title).toBe('Acme Corp')
    expect(normSource({ descriptor_id: 'src.rss', name: 'RSS', kind: 'rss', owner_tenant: 'acme' })).toMatchObject({
      kind: 'source',
      id: 'src.rss',
      owner_tenant: 'acme',
    })
  })
})

describe('queryTerms', () => {
  it('lowercases, splits, dedups, drops empties', () => {
    expect(queryTerms('  Coup  COUP brazil ')).toEqual(['coup', 'brazil'])
    expect(queryTerms('')).toEqual([])
  })
})

describe('scoreHit', () => {
  it('weighs title > snippet > identifier and caps at 1', () => {
    const h = hit({ title: 'coup risk', snippet: 'army movement', id: 'brazil-1', target_id: 'brazil' })
    expect(scoreHit(h, ['coup'])).toBeCloseTo(0.5) // title hit
    expect(scoreHit(h, ['army'])).toBeCloseTo(0.2) // snippet hit
    expect(scoreHit(h, ['brazil'])).toBeCloseTo(0.1) // identifier-only hit
  })

  it('browse mode (no terms) scores 0.5', () => {
    expect(scoreHit(hit(), [])).toBe(0.5)
  })

  it('caps multi-term title matches at 1.0', () => {
    const h = hit({ title: 'alpha beta gamma delta' })
    expect(scoreHit(h, ['alpha', 'beta', 'gamma', 'delta'])).toBe(1)
  })
})

describe('passesFacets', () => {
  it('filters by kind / target / tenant / severity', () => {
    const h = hit({ kind: 'finding', target_id: 'brazil', owner_tenant: 'acme', severity: 'high' })
    expect(passesFacets(h, DEFAULT_FACETS)).toBe(true)
    expect(passesFacets(h, { ...DEFAULT_FACETS, kinds: new Set(['situation']) })).toBe(false)
    expect(passesFacets(h, { ...DEFAULT_FACETS, target_id: 'iran' })).toBe(false)
    expect(passesFacets(h, { ...DEFAULT_FACETS, owner_tenant: 'acme' })).toBe(true)
    expect(passesFacets(h, { ...DEFAULT_FACETS, severity: 'low' })).toBe(false)
  })
})

describe('rankHits', () => {
  it('dedups by kind:id, drops zero-score on query, sorts by score then recency', () => {
    const hits = [
      hit({ kind: 'finding', id: 'a', title: 'no match here', produced_at: '2026-06-01T00:00:00Z' }),
      hit({ kind: 'finding', id: 'b', title: 'coup imminent', produced_at: '2026-06-02T00:00:00Z' }),
      hit({ kind: 'finding', id: 'b', title: 'coup imminent dup' }), // dup id dropped
      hit({ kind: 'situation', id: 'c', snippet: 'coup mentioned', produced_at: '2026-06-03T00:00:00Z' }),
    ]
    const out = rankHits(hits, 'coup', DEFAULT_FACETS)
    // 'a' dropped (zero score), dup 'b' dropped
    expect(out.map((h) => `${h.kind}:${h.id}`)).toEqual(['finding:b', 'situation:c'])
    expect(out[0].score).toBeGreaterThan(out[1].score)
  })

  it('browse mode keeps everything, sorted by recency', () => {
    const hits = [
      hit({ id: 'old', produced_at: '2026-01-01T00:00:00Z' }),
      hit({ id: 'new', produced_at: '2026-06-01T00:00:00Z' }),
    ]
    const out = rankHits(hits, '', DEFAULT_FACETS)
    expect(out.map((h) => h.id)).toEqual(['new', 'old'])
  })
})

describe('kindCounts + toggleKind', () => {
  it('counts per kind', () => {
    const c = kindCounts([hit({ kind: 'finding' }), hit({ kind: 'finding' }), hit({ kind: 'source' })])
    expect(c).toEqual({ finding: 2, situation: 0, entity: 0, source: 1 })
  })
  it('toggleKind adds/removes immutably', () => {
    const a = toggleKind(new Set(['finding']), 'finding')
    expect(a.has('finding')).toBe(false)
    const b = toggleKind(new Set(['finding']), 'source')
    expect(b.has('source')).toBe(true)
  })
})
