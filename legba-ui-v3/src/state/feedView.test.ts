/**
 * Unit tests for the Live Feed's own view store.
 *
 * Locks: the `query` ⇄ `filter` lockstep, desk seeding as an ORDINARY removable
 * chip (the whole point of splitting the feed's filter off the global
 * selection), per-session persistence, and the defensive parse of a stale or
 * hostile session blob.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { chipValue, parseFilterInput, serializeFilter } from '@/lib/feedFilters'
import { parsePersistedFeedView, resetFeedView, useFeedView } from './feedView'

beforeEach(() => {
  sessionStorage.clear()
  resetFeedView()
})

describe('feed view store', () => {
  it('starts on the defaults: intelligence · recency · no filter', () => {
    const s = useFeedView.getState()
    expect(s.stream).toBe('intelligence')
    expect(s.sort).toBe('recency')
    expect(s.filter.chips).toEqual([])
    expect(s.hideSuperseded).toBe(true)
  })

  it('keeps the serialized `query` in lockstep with the parsed `filter`', () => {
    useFeedView.getState().setFilter(parseFilterInput('severity:high verified:true coup'))
    const s = useFeedView.getState()
    expect(s.query).toBe(serializeFilter(s.filter))
    expect(s.query).toBe('severity:high verified:true coup')
  })

  it('setFacet sets, replaces and (with an empty value) CLEARS one facet', () => {
    const { setFacet } = useFeedView.getState()
    setFacet('severity', 'high')
    expect(chipValue(useFeedView.getState().filter.chips, 'severity')).toBe('high')
    setFacet('severity', 'critical')
    expect(chipValue(useFeedView.getState().filter.chips, 'severity')).toBe('critical')
    setFacet('severity', '')
    expect(chipValue(useFeedView.getState().filter.chips, 'severity')).toBe('')
  })

  it('seedDeskFilter writes an ORDINARY target chip the operator can clear', () => {
    useFeedView.getState().seedDeskFilter('country_g20_br')
    expect(useFeedView.getState().query).toBe('target:country_g20_br')
    // Nothing marks it as "seeded" — clearing it works exactly like any chip.
    useFeedView.getState().setFacet('target', '')
    expect(useFeedView.getState().filter.chips).toEqual([])
  })

  it('seedDeskFilter ignores a blank target rather than writing an empty chip', () => {
    useFeedView.getState().seedDeskFilter('   ')
    expect(useFeedView.getState().filter.chips).toEqual([])
  })

  it('a manual facet set after a seed WINS (the seed is not sticky)', () => {
    useFeedView.getState().seedDeskFilter('country_g20_br')
    useFeedView.getState().setFacet('target', 'country_watch_ir')
    expect(chipValue(useFeedView.getState().filter.chips, 'target')).toBe('country_watch_ir')
  })

  it('persists the posture to sessionStorage on every write', () => {
    useFeedView.getState().setStream('signals')
    useFeedView.getState().setFacet('severity', 'high')
    useFeedView.getState().setScrollTop(240)
    const stored = parsePersistedFeedView(sessionStorage.getItem('legba_feed_view_v1'))
    expect(stored.stream).toBe('signals')
    expect(stored.query).toBe('severity:high')
    expect(stored.scrollTop).toBe(240)
  })

  it('applyView replaces stream + sort + filter in one write', () => {
    useFeedView.getState().applyView({ stream: 'signals', sort: 'severity', query: 'target:brazil' })
    const s = useFeedView.getState()
    expect(s.stream).toBe('signals')
    expect(s.sort).toBe('severity')
    expect(chipValue(s.filter.chips, 'target')).toBe('brazil')
  })
})

describe('parsePersistedFeedView — a stale blob never breaks the panel', () => {
  it('degrades field-by-field instead of throwing', () => {
    expect(parsePersistedFeedView(null).stream).toBe('intelligence')
    expect(parsePersistedFeedView('{oops').sort).toBe('recency')
    expect(parsePersistedFeedView('[]').query).toBe('')
    expect(
      parsePersistedFeedView(JSON.stringify({ stream: 'wat', sort: 7, query: 5, scrollTop: -3 })),
    ).toEqual({
      stream: 'intelligence',
      sort: 'recency',
      query: '',
      hideSuperseded: true,
      live: true,
      scrollTop: 0,
    })
  })

  it('round-trips a good blob', () => {
    const blob = JSON.stringify({
      stream: 'signals',
      sort: 'confidence',
      query: 'target:brazil minconf:0.5',
      hideSuperseded: false,
      live: false,
      scrollTop: 512,
    })
    expect(parsePersistedFeedView(blob)).toEqual({
      stream: 'signals',
      sort: 'confidence',
      query: 'target:brazil minconf:0.5',
      hideSuperseded: false,
      live: false,
      scrollTop: 512,
    })
  })
})
