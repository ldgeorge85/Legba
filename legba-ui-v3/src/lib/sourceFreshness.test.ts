import { describe, it, expect } from 'vitest'
import { freshnessTone, freshnessTitle, compareFreshness } from './sourceFreshness'

describe('freshnessTone', () => {
  it('maps every closed grade to its tone', () => {
    expect(freshnessTone('ok')).toBe('ok')
    expect(freshnessTone('stale')).toBe('watch')
    expect(freshnessTone('warn')).toBe('bad')
    expect(freshnessTone('empty')).toBe('muted')
    expect(freshnessTone('ungraded')).toBe('muted')
  })

  it('an unrecognized grade reads muted, never a fabricated ok', () => {
    expect(freshnessTone('some_future_grade')).toBe('muted')
  })
})

describe('freshnessTitle', () => {
  it('folds the cadence budget into ok/stale/warn titles', () => {
    expect(freshnessTitle('ok', 45)).toMatch(/within its cadence-derived budget \(budget 45m\)/)
    expect(freshnessTitle('stale', 45)).toMatch(/over its cadence-derived budget \(budget 45m\)/)
    expect(freshnessTitle('warn', 45)).toMatch(/badly overdue.*\(budget 45m\)/)
  })

  it('omits the budget parenthetical when none was derivable', () => {
    expect(freshnessTitle('ungraded', null)).not.toContain('budget')
    expect(freshnessTitle('empty', null)).not.toContain('budget')
  })

  it('empty and ungraded carry their own honest explanation', () => {
    expect(freshnessTitle('empty', 45)).toMatch(/never produced a signal/)
    expect(freshnessTitle('ungraded', null)).toMatch(/no parsable cadence/)
  })

  it('an unrecognized grade falls back to the raw string, never fabricated prose', () => {
    expect(freshnessTitle('some_future_grade', null)).toBe('some_future_grade')
  })
})

describe('compareFreshness', () => {
  it('sorts worst-first: warn, empty, stale, ungraded, ok', () => {
    const grades = ['ok', 'ungraded', 'stale', 'empty', 'warn']
    expect([...grades].sort(compareFreshness)).toEqual([
      'warn',
      'empty',
      'stale',
      'ungraded',
      'ok',
    ])
  })

  it('an unknown grade sorts alongside ungraded (next to ok, never worst)', () => {
    const grades = ['ok', 'mystery', 'warn']
    expect([...grades].sort(compareFreshness)).toEqual(['warn', 'mystery', 'ok'])
  })
})
