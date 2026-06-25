/**
 * Unit tests for the source-first helpers — the validation + SourceRef
 * composition that the subscription builder relies on.
 */

import { describe, it, expect } from 'vitest'
import {
  buildSourceRef,
  emptySelector,
  emptySubscription,
  lintPredicate,
  lintSelector,
  lintSourceId,
  lintSubscription,
  parseTokens,
  signalGeo,
  starterSourceDescriptor,
  unwrapFactory,
  type SignalRow,
} from './sourceTypes'

function sig(over: Partial<SignalRow> = {}): SignalRow {
  return {
    id: 's',
    data: {},
    title: '',
    source_id: null,
    source_url: '',
    guid: '',
    category: '',
    event_timestamp: null,
    language: '',
    confidence: 0,
    classification_scores: null,
    target_id: null,
    analyst_id: null,
    produced_at: '2026-06-02T10:00:00Z',
    derived_from: [],
    schema_uri: '',
    descriptor_source_id: 's',
    geo: [],
    tags: [],
    entity_classes: [],
    ...over,
  }
}

describe('lintPredicate', () => {
  it('accepts empty / null as no-residual', () => {
    expect(lintPredicate(null)).toBeNull()
    expect(lintPredicate('')).toBeNull()
    expect(lintPredicate('   ')).toBeNull()
  })

  it('accepts a plausible single boolean expression', () => {
    expect(lintPredicate('severity_at_least("high")')).toBeNull()
    expect(lintPredicate('"protest" in mentions() and confidence > 0.5')).toBeNull()
    expect(lintPredicate('host_ip in cidr("10.0.0.0/8")')).toBeNull()
  })

  it('rejects assignment', () => {
    expect(lintPredicate('x = 1')).toMatch(/assignment/)
  })

  it('allows == and >= comparisons (not flagged as assignment)', () => {
    expect(lintPredicate('severity == "high"')).toBeNull()
    expect(lintPredicate('confidence >= 0.5')).toBeNull()
  })

  it('rejects statements / keywords', () => {
    expect(lintPredicate('def f(): pass')).toMatch(/statements/)
    expect(lintPredicate('import os')).toMatch(/statements/)
    expect(lintPredicate('a; b')).toMatch(/single expression/)
  })

  it('rejects unbalanced brackets', () => {
    expect(lintPredicate('mentions(')).toMatch(/unclosed/)
    expect(lintPredicate('a)')).toMatch(/unbalanced/)
  })
})

describe('lintSourceId', () => {
  it('requires non-empty', () => {
    expect(lintSourceId('')).toMatch(/required/)
  })
  it('accepts dotted snake_case', () => {
    expect(lintSourceId('source.rss.brazil')).toBeNull()
    expect(lintSourceId('gdelt')).toBeNull()
  })
  it('rejects bad shapes', () => {
    expect(lintSourceId('Source.RSS')).not.toBeNull()
    expect(lintSourceId('1bad')).not.toBeNull()
  })
})

describe('lintSubscription', () => {
  it('flags bad geo / tag / language tokens', () => {
    const sub = { ...emptySubscription(), geo: ['br'], tags: ['Bad-Tag'], languages: ['english'] }
    const issues = lintSubscription(sub)
    expect(issues.find((i) => i.field === 'geo')).toBeTruthy()
    expect(issues.find((i) => i.field === 'tags')).toBeTruthy()
    expect(issues.find((i) => i.field === 'languages')).toBeTruthy()
  })

  it('passes a clean structured subscription', () => {
    const sub = { ...emptySubscription(), geo: ['BR'], tags: ['protest'], languages: ['pt-BR'] }
    expect(lintSubscription(sub)).toHaveLength(0)
  })

  it('surfaces a predicate residual error', () => {
    const sub = { ...emptySubscription(), predicate: 'x = 1' }
    expect(lintSubscription(sub).find((i) => i.field === 'predicate')).toBeTruthy()
  })
})

describe('lintSelector', () => {
  it('passes a clean selector', () => {
    const sel = { ...emptySelector(), tags: ['news'], geo: ['US'], kinds: ['rss'] }
    expect(lintSelector(sel)).toHaveLength(0)
  })
  it('flags bad geo', () => {
    const sel = { ...emptySelector(), geo: ['usa1'] }
    expect(lintSelector(sel).find((i) => i.field === 'geo')).toBeTruthy()
  })
})

describe('buildSourceRef', () => {
  it('sets exactly source_id in explicit mode', () => {
    const ref = buildSourceRef('explicit', ' source.rss.brazil ', emptySelector(), emptySubscription())
    expect(ref.source_id).toBe('source.rss.brazil')
    expect(ref.source_selector).toBeNull()
    expect(ref.subscription.canonical_only).toBe(true)
  })

  it('sets exactly source_selector in selector mode', () => {
    const sel = { ...emptySelector(), tags: ['news'] }
    const ref = buildSourceRef('selector', '', sel, emptySubscription())
    expect(ref.source_id).toBeNull()
    expect(ref.source_selector).not.toBeNull()
    expect(ref.source_selector?.tags).toEqual(['news'])
  })

  it('prunes empty predicate to null', () => {
    const ref = buildSourceRef('explicit', 'gdelt', emptySelector(), {
      ...emptySubscription(),
      predicate: '   ',
    })
    expect(ref.subscription.predicate).toBeNull()
  })
})

describe('parseTokens', () => {
  it('splits on commas / whitespace / newlines and trims', () => {
    expect(parseTokens('a, b\nc  d')).toEqual(['a', 'b', 'c', 'd'])
    expect(parseTokens('')).toEqual([])
  })
})

describe('unwrapFactory', () => {
  it('passes through a bare string', () => {
    expect(unwrapFactory('*/15 * * * *')).toBe('*/15 * * * *')
  })
  it('unwraps the property-factory {raw,...} wrapper', () => {
    expect(unwrapFactory({ raw: '*/15 * * * *', ui_hint: {}, factory_kind: 'cron' })).toBe(
      '*/15 * * * *',
    )
  })
  it('returns null for null / non-factory objects', () => {
    expect(unwrapFactory(null)).toBeNull()
    expect(unwrapFactory(undefined)).toBeNull()
    expect(unwrapFactory({ nope: 1 })).toBeNull()
  })
})

describe('signalGeo', () => {
  it('reads the geocode object from data.geo', () => {
    const g = signalGeo(sig({ data: { geo: { country_iso2: 'GB', lat: 1, lon: 2 } } }))
    expect(g?.country_iso2).toBe('GB')
  })
  it('returns null when data.geo is an array (top-level facet, not a geocode)', () => {
    expect(signalGeo(sig({ data: { geo: ['GB'] } }))).toBeNull()
  })
  it('returns null when ungeocoded', () => {
    expect(signalGeo(sig())).toBeNull()
  })
})

describe('starterSourceDescriptor', () => {
  it('produces a draft RSS poll source with the version sentinel', () => {
    const d = starterSourceDescriptor('alice')
    const identity = d.identity as Record<string, unknown>
    expect(identity.owner).toBe('alice')
    expect(identity.state).toBe('draft')
    expect(identity.kind).toBe('rss')
    expect(identity.version).toBe('0'.repeat(16))
    expect(d.acquisition).toBe('poll')
    expect((d.cadence as Record<string, unknown>).schedule).toBeTruthy()
  })
})
