/**
 * The coverage strip under the Board's map: covered, looked and could not
 * see, not covered, from the Blanks layer's own figures; never a zero before
 * the reads have answered.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { CoverageStrip, mapCoverage } from './coverage'
import type { PrepOutput } from './prep'

afterEach(() => cleanup())

function out(countries: Record<string, 'dense' | 'sparse' | null>): PrepOutput {
  const c: PrepOutput['countries'] = {}
  for (const [iso, blank] of Object.entries(countries)) c[iso] = { iso, name: iso, subjects: [], changes: 0, blank }
  const dense = Object.values(countries).filter((b) => b === 'dense').length
  const sparse = Object.values(countries).filter((b) => b === 'sparse').length
  return { countries: c, counts: { blanksDense: dense, blanksSparse: sparse } } as unknown as PrepOutput
}

describe('the coverage figures', () => {
  it('splits the places that hold a subject three ways', () => {
    const c = mapCoverage(out({ AA: null, BB: null, CC: 'dense', DD: 'sparse' }), { reads: [], subjects: [] }, { blanks: true }, {})
    expect(c).toEqual({ state: 'read', places: 4, covered: 2, looked: 1, uncovered: 1 })
  })

  it('says why there is no figure: not read yet, the layer off, the route unreadable', () => {
    expect(mapCoverage(null, { reads: null, subjects: null }, { blanks: true }, {}).state).toBe('unread')
    expect(mapCoverage(out({}), { reads: null, subjects: [] }, { blanks: true }, {}).state).toBe('unread')
    expect(mapCoverage(out({}), { reads: [], subjects: [] }, { blanks: false }, {}).state).toBe('off')
    expect(mapCoverage(out({}), { reads: [], subjects: [] }, { blanks: true }, { blanks: true }).state).toBe('unreadable')
  })
})

describe('the strip', () => {
  it('reads as one row of words and figures', () => {
    render(<CoverageStrip coverage={{ state: 'read', places: 46, covered: 10, looked: 36, uncovered: 0 }} />)
    expect(screen.getByTestId('coverage-strip-covered').textContent).toBe('10 of 46 places read')
    expect(screen.getByTestId('coverage-strip-looked').textContent).toBe('36 looked, could not see')
    expect(screen.getByTestId('coverage-strip-uncovered').textContent).toBe('0 not covered')
  })

  it('prints no figure before the reads answer', () => {
    render(<CoverageStrip coverage={null} />)
    expect(screen.getByTestId('coverage-strip')).toHaveAttribute('data-state', 'unread')
    expect(screen.getByTestId('coverage-strip').textContent).not.toMatch(/\d/)
  })
})
