/**
 * Unit tests for the feed filter model (S7-T4).
 *
 * Locks: typed key:value chip parsing (+ aliases), round-trip serialization,
 * single-valued chip replacement, relative time windows, the verification facet
 * derived from the SAME verdict vocabulary the badge uses, the AND-combined row
 * matcher, and view-state / saved-view serialization.
 */
import { describe, it, expect } from 'vitest'
import type { UnifiedRow } from './findingsViews'
import {
  parseFilterInput,
  serializeFilter,
  mergeChips,
  setChip,
  removeChip,
  chipValue,
  parseSince,
  deriveRowVerdict,
  isVerified,
  matchesFilter,
  parseViewState,
  serializeViewState,
  loadFeedViews,
  persistFeedViews,
  upsertFeedView,
  removeFeedView,
  serverFilterParams,
  FINDINGS_SERVER_FACETS,
  SIGNALS_SERVER_FACETS,
} from './feedFilters'

/** Build a minimal finding row for the matcher tests. */
function finding(over: Partial<UnifiedRow> = {}): UnifiedRow {
  return {
    id: over.id ?? 'f1',
    source: 'finding',
    kind: 'finding',
    title: 'Coup risk rises in Iran',
    body: 'body',
    confidence: 0.9,
    severity: 'high',
    target_id: 'country_g20_iran',
    analyst_id: 'escalation.analyst',
    analyst_version: 'v1',
    produced_at: new Date().toISOString(),
    derived_from: [],
    schema_uri: 'x',
    data: {},
    critic_score: null,
    effective_confidence: 0.9,
    verification: { faithfulness_score: 0.9, judge_status: 'llm' },
    ...over,
  }
}

describe('parseFilterInput / serializeFilter', () => {
  it('splits typed chips (with aliases) from free text', () => {
    const p = parseFilterInput('severity:high verified:true country:iran coup risk')
    expect(p.chips).toEqual([
      { key: 'severity', value: 'high' },
      { key: 'verified', value: 'true' },
      { key: 'target', value: 'iran' }, // country → target alias
    ])
    expect(p.text).toBe('coup risk')
  })

  it('leaves an unknown key as free text (never a fabricated facet)', () => {
    const p = parseFilterInput('foo:bar hello')
    expect(p.chips).toEqual([])
    expect(p.text).toBe('foo:bar hello')
  })

  it('round-trips through serializeFilter', () => {
    const s = 'severity:critical last:7d gulf'
    const p = parseFilterInput(s)
    expect(parseFilterInput(serializeFilter(p))).toEqual(p)
  })
})

describe('chip helpers', () => {
  it('setChip replaces a single-valued facet (last-wins) and clears on empty', () => {
    let chips = setChip([], 'severity', 'high')
    chips = setChip(chips, 'severity', 'critical')
    expect(chips).toEqual([{ key: 'severity', value: 'critical' }])
    expect(setChip(chips, 'severity', '')).toEqual([])
  })

  it('mergeChips de-dupes and removeChip drops by key+value', () => {
    const chips = mergeChips([{ key: 'kind', value: 'finding' }], [{ key: 'kind', value: 'finding' }])
    expect(chips).toEqual([{ key: 'kind', value: 'finding' }])
    expect(removeChip(chips, 'kind', 'finding')).toEqual([])
    expect(chipValue([{ key: 'severity', value: 'high' }], 'severity')).toBe('high')
  })
})

describe('parseSince', () => {
  it('parses relative windows to ms', () => {
    expect(parseSince('7d')).toBe(7 * 86_400_000)
    expect(parseSince('24h')).toBe(24 * 3_600_000)
    expect(parseSince('30m')).toBe(30 * 60_000)
    expect(parseSince('2mo')).toBe(2 * 2_592_000_000)
  })
  it('returns null for garbage', () => {
    expect(parseSince('soon')).toBeNull()
    expect(parseSince('0d')).toBeNull()
  })
})

describe('verification facet', () => {
  it('isVerified reflects a real verify pass', () => {
    const verified = deriveRowVerdict(finding(), 2)
    expect(isVerified(verified)).toBe(true)
    const bare = deriveRowVerdict(finding({ verification: null }), 0)
    expect(isVerified(bare)).toBe(false) // unassessed → unverified
  })

  it('verified:true keeps only verified rows; verified:false only unverified', () => {
    const v = finding({ id: 'v', verification: { faithfulness_score: 0.9, judge_status: 'llm' } })
    const u = finding({ id: 'u', verification: null })
    const vv = deriveRowVerdict(v, 2)
    const uv = deriveRowVerdict(u, 0)
    const onlyVerified = parseFilterInput('verified:true')
    expect(matchesFilter(v, vv, onlyVerified)).toBe(true)
    expect(matchesFilter(u, uv, onlyVerified)).toBe(false)
    const onlyUnverified = parseFilterInput('verified:false')
    expect(matchesFilter(u, uv, onlyUnverified)).toBe(true)
    expect(matchesFilter(v, vv, onlyUnverified)).toBe(false)
  })

  it('P0-4 — deriveRowVerdict flags verify-exempt structural rows', () => {
    // Via the analyst_id registry mirror (live-tail rows carry no stamp)…
    const tail = finding({
      analyst_id: 'graph_mining',
      verification: null,
      verify_exempt: undefined,
    })
    const tv = deriveRowVerdict(tail, 0)
    expect(tv.structural).toBe(true)
    expect(tv.confidence).toBe('unassessed')
    expect(isVerified(tv)).toBe(false)
    // …and via the server verify_exempt stamp (REST rows), even for an id the
    // client mirror does not know.
    const rest = finding({
      analyst_id: 'future_mining_analyst',
      verification: null,
      verify_exempt: 'structural',
    })
    expect(deriveRowVerdict(rest, 0).structural).toBe(true)
    // A verified LLM read is never structural.
    expect(deriveRowVerdict(finding(), 2).structural).toBe(false)
  })
})

describe('matchesFilter', () => {
  const row = finding()
  const verdict = deriveRowVerdict(row, 2)

  it('the "high-sev verified Iran last 7d" query matches an on-target row', () => {
    const f = parseFilterInput('severity:high verified:true country:iran last:7d')
    expect(matchesFilter(row, verdict, f)).toBe(true)
  })

  it('a wrong severity or stale time excludes the row', () => {
    expect(matchesFilter(row, verdict, parseFilterInput('severity:low'))).toBe(false)
    const old = finding({ produced_at: '2000-01-01T00:00:00Z' })
    expect(matchesFilter(old, deriveRowVerdict(old, 2), parseFilterInput('last:7d'))).toBe(false)
  })

  it('free text AND-matches every word against the row haystack', () => {
    expect(matchesFilter(row, verdict, parseFilterInput('coup iran'))).toBe(true)
    expect(matchesFilter(row, verdict, parseFilterInput('coup brazil'))).toBe(false)
  })

  it('kind:signal / kind:finding gate on the stream discriminant', () => {
    const sig = finding({ id: 's', source: 'signal', severity: null })
    expect(matchesFilter(sig, deriveRowVerdict(sig, 0), parseFilterInput('kind:signal'))).toBe(true)
    expect(matchesFilter(row, verdict, parseFilterInput('kind:signal'))).toBe(false)
  })
})

describe('minconf — the effective-confidence floor facet', () => {
  it('keeps rows at or above the floor and drops the ones below it', () => {
    const above = finding({ id: 'a', confidence: 0.9, effective_confidence: 0.62 })
    const below = finding({ id: 'b', confidence: 0.9, effective_confidence: 0.41 })
    const f = parseFilterInput('minconf:0.5')
    expect(matchesFilter(above, deriveRowVerdict(above, 0), f)).toBe(true)
    expect(matchesFilter(below, deriveRowVerdict(below, 0), f)).toBe(false)
  })

  it('gates on the SURFACED (critic-folded) confidence, not the raw one', () => {
    // Raw confidence clears 0.5; the critic folded it to 0.3 — the fold wins.
    const demoted = finding({ confidence: 0.9, effective_confidence: 0.3 })
    expect(matchesFilter(demoted, deriveRowVerdict(demoted, 0), parseFilterInput('minconf:0.5'))).toBe(
      false,
    )
  })

  it('an ungraded row cannot clear a floor, and a garbage floor never over-filters', () => {
    const ungraded = finding({ confidence: null, effective_confidence: null })
    expect(matchesFilter(ungraded, deriveRowVerdict(ungraded, 0), parseFilterInput('minconf:0.5'))).toBe(
      false,
    )
    expect(matchesFilter(ungraded, deriveRowVerdict(ungraded, 0), parseFilterInput('minconf:abc'))).toBe(
      true,
    )
  })

  it('accepts the `floor:` alias and is single-valued (last pick wins)', () => {
    const p = parseFilterInput('floor:0.5 minconf:0.7')
    expect(p.chips).toEqual([{ key: 'minconf', value: '0.7' }])
    expect(serializeFilter(p)).toBe('minconf:0.7')
  })
})

describe('serverFilterParams — the facets the REST routes answer themselves', () => {
  const NOW = Date.parse('2026-06-10T00:00:00Z')

  it('pushes an EXACT desk/producer, and refuses a hand-typed partial', () => {
    const opts = {
      supports: FINDINGS_SERVER_FACETS,
      exactTargets: new Set(['country_g20_br']),
      exactAnalysts: new Set(['energy_security']),
      now: NOW,
    }
    expect(
      serverFilterParams(parseFilterInput('target:country_g20_br analyst:energy_security'), opts),
    ).toEqual({ target_id: 'country_g20_br', analyst_id: 'energy_security' })
    // `target:braz` is a client-side SUBSTRING match; pushing it as an exact
    // `target_id=braz` would return an empty page and read as "no results".
    expect(serverFilterParams(parseFilterInput('target:braz analyst:escal'), opts)).toEqual({})
  })

  it('pushes only the real severity vocabulary — `severity:none` stays client-side', () => {
    const opts = { supports: FINDINGS_SERVER_FACETS, now: NOW }
    expect(serverFilterParams(parseFilterInput('severity:critical'), opts)).toEqual({
      severity: 'critical',
    })
    expect(serverFilterParams(parseFilterInput('severity:none'), opts)).toEqual({})
  })

  it('converts a relative window into an absolute `since`, and drops an unparseable one', () => {
    const opts = { supports: FINDINGS_SERVER_FACETS, now: NOW }
    expect(serverFilterParams(parseFilterInput('last:7d'), opts)).toEqual({
      since: new Date(NOW - 7 * 86_400_000).toISOString(),
    })
    expect(serverFilterParams(parseFilterInput('last:forever'), opts)).toEqual({})
  })

  it('honours what each route actually supports (signals take no analyst/severity)', () => {
    const filter = parseFilterInput('target:brazil analyst:energy_security severity:high last:1d')
    const shared = {
      exactTargets: new Set(['brazil']),
      exactAnalysts: new Set(['energy_security']),
      now: NOW,
    }
    expect(Object.keys(serverFilterParams(filter, { ...shared, supports: FINDINGS_SERVER_FACETS })).sort())
      .toEqual(['analyst_id', 'severity', 'since', 'target_id'])
    expect(Object.keys(serverFilterParams(filter, { ...shared, supports: SIGNALS_SERVER_FACETS })).sort())
      .toEqual(['since', 'target_id'])
  })

  it('never pushes free text (the route matches whole tokens; typing would blank the list)', () => {
    expect(
      serverFilterParams(parseFilterInput('cou'), { supports: FINDINGS_SERVER_FACETS, now: NOW }),
    ).toEqual({})
  })

  it('every pushed param is a narrowing the client matcher ALSO enforces', () => {
    // A row the server would exclude must also fail the client pass, so the two
    // halves of the filter can never disagree.
    const opts = {
      supports: FINDINGS_SERVER_FACETS,
      exactTargets: new Set(['country_g20_br']),
      now: NOW,
    }
    const f = parseFilterInput('target:country_g20_br severity:critical')
    expect(serverFilterParams(f, opts)).toEqual({
      target_id: 'country_g20_br',
      severity: 'critical',
    })
    const wrongTarget = finding({ target_id: 'country_g20_us', severity: 'critical' })
    const wrongSeverity = finding({ target_id: 'country_g20_br', severity: 'low' })
    const match = finding({ target_id: 'country_g20_br', severity: 'critical' })
    expect(matchesFilter(wrongTarget, deriveRowVerdict(wrongTarget, 0), f, NOW)).toBe(false)
    expect(matchesFilter(wrongSeverity, deriveRowVerdict(wrongSeverity, 0), f, NOW)).toBe(false)
    expect(matchesFilter(match, deriveRowVerdict(match, 0), f, NOW)).toBe(true)
  })
})

describe('view-state + saved views', () => {
  it('serializeViewState round-trips through parseViewState', () => {
    const v = { stream: 'signals' as const, sort: 'severity' as const, query: 'severity:high' }
    expect(parseViewState(serializeViewState(v))).toEqual(v)
    expect(parseViewState('not json')).toBeNull()
  })

  it('saved views persist + upsert + remove through localStorage', () => {
    localStorage.clear()
    let views = upsertFeedView([], {
      name: 'iran-hot',
      stream: 'intelligence',
      sort: 'severity',
      query: 'severity:high verified:true country:iran',
    })
    persistFeedViews(views)
    expect(loadFeedViews()[0].name).toBe('iran-hot')
    views = removeFeedView(views, 'iran-hot')
    persistFeedViews(views)
    expect(loadFeedViews()).toEqual([])
  })
})
