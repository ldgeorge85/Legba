import { describe, it, expect } from 'vitest'
import {
  toContention,
  toContentions,
  contentionForFact,
  badgeLabel,
  type ContentionRow,
  type ContentionValueRow,
} from './contentionModel'

function value(over: Partial<ContentionValueRow> = {}): ContentionValueRow {
  return {
    value_key: 'alpha',
    representative_fact_id: 'fa',
    distinct_source_count: 3,
    source_credibility_sum: 3,
    confidence_max: 0.9,
    confidence_mean: 0.8,
    source_types: ['curated'],
    arbiter_score: 0.77,
    surfaced_winner: true,
    is_junk: false,
    junk_reason: null,
    latest_asserted_at: '2026-06-27T00:00:00Z',
    ...over,
  }
}

function group(over: Partial<ContentionRow> = {}): ContentionRow {
  return {
    id: 'g1',
    subject_key: 'country x',
    predicate_key: 'capital',
    status: 'surfaced',
    surfaced_value: 'Alpha',
    value_count: 2,
    junk_count: 0,
    opened_at: '2026-06-01T00:00:00Z',
    resolved_at: '2026-06-28T00:00:00Z',
    updated_at: '2026-06-28T00:00:00Z',
    values: [value({ value_key: 'alpha', source_credibility_sum: 3, surfaced_winner: true }),
             value({ value_key: 'beta', source_credibility_sum: 1, surfaced_winner: false, arbiter_score: 0.4 })],
    ...over,
  }
}

describe('toContention', () => {
  it('maps a surfaced group and computes per-value credibility share', () => {
    const v = toContention(group())
    expect(v.id).toBe('g1')
    expect(v.isLive).toBe(true)
    expect(v.abstained).toBe(false)
    expect(v.surfacedValue).toBe('Alpha')
    expect(v.valueCount).toBe(2)
    // 3 / (3 + 1) and 1 / (3 + 1)
    const alpha = v.values.find((x) => x.valueKey === 'alpha')!
    const beta = v.values.find((x) => x.valueKey === 'beta')!
    expect(alpha.credibilityShare).toBeCloseTo(0.75)
    expect(beta.credibilityShare).toBeCloseTo(0.25)
    expect(alpha.surfacedWinner).toBe(true)
    expect(beta.surfacedWinner).toBe(false)
  })

  it('flags an abstained (contested, no winner) group', () => {
    const v = toContention(
      group({
        status: 'contested',
        surfaced_value: null,
        values: [
          value({ value_key: 'alpha', surfaced_winner: false }),
          value({ value_key: 'beta', surfaced_winner: false }),
        ],
      }),
    )
    expect(v.isLive).toBe(true)
    expect(v.abstained).toBe(true)
    expect(v.values.every((x) => !x.surfacedWinner)).toBe(true)
  })

  it('treats a collapsed group as not live', () => {
    const v = toContention(group({ status: 'collapsed' }))
    expect(v.isLive).toBe(false)
    // A collapsed group is resolved → not an abstention either.
    expect(v.abstained).toBe(false)
  })

  it('excludes junk clusters from the credibility-share denominator', () => {
    const v = toContention(
      group({
        junk_count: 1,
        values: [
          value({ value_key: 'alpha', source_credibility_sum: 3, surfaced_winner: true }),
          value({
            value_key: 'berlin',
            source_credibility_sum: 100, // a junk cluster with huge credibility
            is_junk: true,
            junk_reason: 'inverted_relation',
            surfaced_winner: false,
          }),
        ],
      }),
    )
    // The non-junk winner keeps a 100% share — junk does not dilute it.
    const alpha = v.values.find((x) => x.valueKey === 'alpha')!
    const junk = v.values.find((x) => x.valueKey === 'berlin')!
    expect(alpha.credibilityShare).toBeCloseTo(1)
    expect(junk.isJunk).toBe(true)
    expect(junk.junkReason).toBe('inverted_relation')
  })

  it('reads defensively from a partial payload', () => {
    const v = toContention({
      id: 'g2',
      subject_key: 's',
      predicate_key: 'p',
      status: 'surfaced',
      surfaced_value: 'X',
      value_count: 1,
      junk_count: 0,
      opened_at: '',
      resolved_at: null,
      updated_at: '',
      // value missing some numeric fields → coerced to 0, share = 0 when total 0
      values: [value({ source_credibility_sum: 0, arbiter_score: null })],
    } as ContentionRow)
    expect(v.values[0].credibilityShare).toBe(0)
    expect(v.values[0].arbiterScore).toBeNull()
  })
})

describe('contentionForFact', () => {
  it('returns the single group from a fact_id page', () => {
    const view = contentionForFact({ data: [group()], next_cursor: null })
    expect(view?.id).toBe('g1')
  })
  it('returns null for an uncontested fact (empty page)', () => {
    expect(contentionForFact({ data: [], next_cursor: null })).toBeNull()
    expect(contentionForFact(undefined)).toBeNull()
  })
})

describe('badgeLabel', () => {
  it('labels a surfaced dispute by value count', () => {
    expect(badgeLabel(toContention(group()))).toBe('Contested — 2 values')
  })
  it('labels an abstained dispute as no-winner', () => {
    const v = toContention(group({ status: 'contested', surfaced_value: null }))
    expect(badgeLabel(v)).toBe('Contested — 2 values, no winner')
  })
})

describe('toContentions', () => {
  it('maps a list and tolerates an empty/missing input', () => {
    expect(toContentions([group()])).toHaveLength(1)
    expect(toContentions([] as ContentionRow[])).toEqual([])
  })
})
