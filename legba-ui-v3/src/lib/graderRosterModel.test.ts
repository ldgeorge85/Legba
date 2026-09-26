/**
 * Unit tests for the grader-roster model (`@/lib/graderRosterModel`).
 *
 * The arithmetic and the ordering that decide what the Eval Scorecard's
 * roster section SAYS, tested without a DOM. What is pinned:
 *
 *   - a null correctness share prints the word `unmeasured`, never `0.0%`;
 *   - a coverage of 0 prints `0.0%`, because a reference that bore on nothing
 *     is a measurement and not an absence — the two cases are asserted side by
 *     side, because the bug this section exists to prevent is exactly the one
 *     that makes them look alike;
 *   - the percentage formatting matches the server's `_pct` (one decimal), so
 *     the split columns and the verbatim badge cannot disagree;
 *   - the ordering puts the thinnest coverage first, with a null coverage LAST
 *     (a desk that published nothing is not a desk nobody checked);
 *   - `rosterHeadlines` always returns BOTH figures, each carrying its own
 *     denominator.
 */

import { describe, it, expect } from 'vitest'
import {
  UNMEASURED,
  correctnessCell,
  coverageCell,
  pct,
  referenceCell,
  rosterHeadlines,
  sortByCoverageAscending,
  type DeskLatest,
  type DeskRosterRow,
  type RosterTotals,
} from './graderRosterModel'

function latest(over: Partial<DeskLatest> = {}): DeskLatest {
  return {
    as_of: '2026-09-25T09:20:00+00:00',
    correctness_share: 0.9,
    coverage_share: 0.25,
    n_claims: 40,
    n_contains: 9,
    n_decided: 10,
    n_silent: 30,
    single_family: false,
    reference_state: 'current',
    reference_age_days: 0,
    badge:
      'correctness 90.0% (9/10) · coverage 25.0% (n=40) · as of 2026-09-25',
    ...over,
  }
}

function desk(
  target_id: string,
  analyst_id: string,
  over: Partial<DeskLatest> = {},
): DeskRosterRow {
  return {
    target_id,
    analyst_id,
    latest: latest(over),
    window: { nights_graded: 2, correctness_mean: 0.7, coverage_mean: 0.22 },
  }
}

describe('pct', () => {
  it('formats one decimal, matching the server', () => {
    expect(pct(0.9)).toBe('90.0%')
    expect(pct(0.0856)).toBe('8.6%')
  })

  it('never turns an absent share into a number', () => {
    expect(pct(null)).toBe(UNMEASURED)
    expect(pct(undefined)).toBe(UNMEASURED)
  })

  it('prints a measured zero AS zero', () => {
    // The reference bore on nothing. That is a finding, not an absence.
    expect(pct(0)).toBe('0.0%')
  })
})

describe('correctnessCell', () => {
  it('carries the denominator with the number', () => {
    expect(correctnessCell(latest())).toBe('90.0% (9/10)')
  })

  it('says unmeasured — with the decided count — when the share is null', () => {
    const cell = correctnessCell(
      latest({ correctness_share: null, n_contains: 0, n_decided: 0 }),
    )
    expect(cell).toBe('unmeasured (0 decided)')
    expect(cell).not.toContain('0.0%')
    expect(cell).not.toContain('0%')
  })
})

describe('coverageCell', () => {
  it('carries the n the share is over', () => {
    expect(coverageCell(latest())).toBe('25.0% (n=40)')
  })

  it('renders a measured zero as a number, not as unmeasured', () => {
    const cell = coverageCell(latest({ coverage_share: 0, n_claims: 12 }))
    expect(cell).toBe('0.0% (n=12)')
  })
})

describe('referenceCell', () => {
  it('names the three states, with the age on the stale one', () => {
    expect(referenceCell(latest())).toBe('current')
    expect(
      referenceCell(latest({ reference_state: 'stale', reference_age_days: 9 })),
    ).toBe('stale (9.0 d)')
    expect(referenceCell(latest({ reference_state: 'none' }))).toBe(
      'no reference',
    )
  })
})

describe('sortByCoverageAscending', () => {
  it('puts the thinnest coverage first — the point of the section', () => {
    const rows = [
      desk('country_g20_tr', 'escalation', { coverage_share: 0.25 }),
      desk('country_g20_tr', 'economic_stress', { coverage_share: 0 }),
      desk('country_g20_br', 'escalation', { coverage_share: 0.1 }),
    ]
    expect(sortByCoverageAscending(rows).map((d) => d.latest.coverage_share)).toEqual(
      [0, 0.1, 0.25],
    )
  })

  it('sorts a null coverage LAST, not first', () => {
    // A desk that published no claim at all is not a desk nobody checked.
    const rows = [
      desk('country_g20_tr', 'escalation', { coverage_share: null }),
      desk('country_g20_br', 'escalation', { coverage_share: 0.4 }),
    ]
    expect(sortByCoverageAscending(rows).map((d) => d.target_id)).toEqual([
      'country_g20_br',
      'country_g20_tr',
    ])
  })

  it('breaks ties stably on target then desk', () => {
    const rows = [
      desk('country_g20_tr', 'escalation', { coverage_share: 0.1 }),
      desk('country_g20_br', 'water_stress', { coverage_share: 0.1 }),
      desk('country_g20_br', 'escalation', { coverage_share: 0.1 }),
    ]
    expect(
      sortByCoverageAscending(rows).map((d) => `${d.target_id}:${d.analyst_id}`),
    ).toEqual([
      'country_g20_br:escalation',
      'country_g20_br:water_stress',
      'country_g20_tr:escalation',
    ])
  })

  it('does not mutate its input and tolerates absence', () => {
    const rows = [
      desk('country_g20_tr', 'escalation', { coverage_share: 0.9 }),
      desk('country_g20_br', 'escalation', { coverage_share: 0.1 }),
    ]
    const before = rows.map((d) => d.target_id)
    sortByCoverageAscending(rows)
    expect(rows.map((d) => d.target_id)).toEqual(before)
    expect(sortByCoverageAscending(null)).toEqual([])
    expect(sortByCoverageAscending(undefined)).toEqual([])
  })
})

describe('rosterHeadlines', () => {
  /** The live 7-night DESK-grain roster, 2026-09-25: 232 desks, 59 graded. */
  const LIVE: RosterTotals = {
    desks_graded: 59,
    desks_unmeasured: 173,
    correctness_mean_of_desks: 0.4915,
    coverage_mean_of_desks: 0.0775,
    claims_pooled: {
      n_claims: 1209,
      n_decided: 101,
      n_contains: 51,
      correctness_pooled: 0.505,
      coverage_pooled: 0.0835,
    },
  }

  it('returns BOTH figures, each labelled and each with its denominator', () => {
    const [mean, pooled] = rosterHeadlines(LIVE)
    expect(mean.label).toBe('mean of desks')
    expect(mean.correctness).toBe('49.1%')
    expect(mean.coverage).toBe('7.8%')
    expect(mean.basis).toBe('59 graded of 232 desks')

    expect(pooled.label).toBe('pooled claims')
    expect(pooled.correctness).toBe('50.5% (51/101)')
    expect(pooled.coverage).toBe('8.3% (n=1209)')
    expect(pooled.basis).toBe('101 decided of 1209 claims')
  })

  it('never emits a correctness without the coverage beside it', () => {
    for (const h of rosterHeadlines(LIVE)) {
      expect(h.correctness).not.toBe('')
      expect(h.coverage).not.toBe('')
    }
  })

  it('reports an ungraded roster as unmeasured, never as zero', () => {
    const empty: RosterTotals = {
      desks_graded: 0,
      desks_unmeasured: 3,
      correctness_mean_of_desks: null,
      coverage_mean_of_desks: null,
      claims_pooled: {
        n_claims: 40,
        n_decided: 0,
        n_contains: 0,
        correctness_pooled: null,
        coverage_pooled: 0,
      },
    }
    const [mean, pooled] = rosterHeadlines(empty)
    expect(mean.correctness).toBe(UNMEASURED)
    expect(mean.coverage).toBe(UNMEASURED)
    expect(mean.basis).toBe('0 graded of 3 desks')
    expect(pooled.correctness).toBe('unmeasured (0 decided)')
    // The pooled coverage is a real 0 — 40 claims published, none decided.
    expect(pooled.coverage).toBe('0.0% (n=40)')
  })
})
