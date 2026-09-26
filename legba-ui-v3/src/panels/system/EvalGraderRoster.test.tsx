/**
 * Component test for the Eval Scorecard's grader-roster section.
 *
 * The fixture is shaped from a real read of the production `unit_correctness`
 * table (2026-09-25, 7-night window, desk grain): 232 desks, 59 of them
 * carrying a correctness share and 173 whose reference decided nothing; a mean
 * of desks of 49.1% at 7.8% coverage against a claim-pooled 50.5% at 8.3%. The
 * two roster figures genuinely differ on the live instance, which is the whole
 * reason both ship.
 *
 * What is asserted is the honesty contract, not the styling:
 *   - BOTH roster figures render, each labelled and each with its denominator;
 *   - a desk with a null correctness share reads `unmeasured`, never `0%`, and
 *     its coverage of 0.0 STILL renders as a number (the two absences are
 *     different facts);
 *   - the thinnest coverage sorts first;
 *   - an unavailable roster is "no grader rows yet" with no as-of and no
 *     figures — never a table of zeros;
 *   - the route's own `honesty_note` renders verbatim.
 */

import { describe, it, expect } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { EvalGraderRoster } from './EvalGraderRoster'
import type { DeskLatest, DeskRosterRow, GraderRoster } from '@/lib/graderRosterModel'

const HONESTY_NOTE =
  'Correctness is the share of the claims the independent reference BEARS ON that it ' +
  'bore out; coverage is how much of what the desk said the reference bears on at all.'

function deskRow(
  target_id: string,
  analyst_id: string,
  latest: Partial<DeskLatest>,
  nights_graded = 5,
): DeskRosterRow {
  const base: DeskLatest = {
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
    badge: 'correctness 90.0% (9/10) · coverage 25.0% (n=40) · as of 2026-09-25',
    ...latest,
  }
  return {
    target_id,
    analyst_id,
    latest: base,
    window: {
      nights_graded,
      correctness_mean: base.correctness_share,
      coverage_mean: base.coverage_share,
    },
  }
}

/** Three desks: one graded and thick, one graded and thin, one UNMEASURED. */
const LIVE: GraderRoster = {
  available: true,
  nights: 7,
  grain: 'desk',
  as_of: new Date(Date.now() - 2 * 3600 * 1000).toISOString(),
  desks: [
    deskRow('country_g20_tr', 'escalation', {
      correctness_share: 0.9,
      coverage_share: 0.25,
    }),
    deskRow('country_g20_br', 'water_stress', {
      correctness_share: 0.5,
      coverage_share: 0.1,
      n_claims: 20,
      n_contains: 1,
      n_decided: 2,
      single_family: true,
      reference_state: 'stale',
      reference_age_days: 9,
      badge:
        'correctness 50.0% (1/2) · coverage 10.0% (n=20) · as of 2026-09-25 · ' +
        'single-family · reference stale (9.0 d)',
    }),
    deskRow('country_watch_ml', 'economic_stress', {
      correctness_share: null,
      coverage_share: 0,
      n_claims: 12,
      n_contains: 0,
      n_decided: 0,
      n_silent: 12,
      badge:
        'correctness unmeasured (0 decided) · coverage 0.0% (n=12) · as of 2026-09-25',
    }),
  ],
  roster: {
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
  },
  honesty_note: HONESTY_NOTE,
}

describe('EvalGraderRoster', () => {
  it('renders BOTH roster figures, each labelled with its denominator', () => {
    render(<EvalGraderRoster roster={LIVE} />)

    const mean = screen.getByTestId('grader-roster-headline-mean-of-desks')
    expect(mean).toHaveTextContent('mean of desks')
    expect(mean).toHaveTextContent('correctness 49.1%')
    expect(mean).toHaveTextContent('coverage 7.8%')
    expect(mean).toHaveTextContent('59 graded of 232 desks')

    const pooled = screen.getByTestId('grader-roster-headline-pooled-claims')
    expect(pooled).toHaveTextContent('pooled claims')
    expect(pooled).toHaveTextContent('correctness 50.5% (51/101)')
    expect(pooled).toHaveTextContent('coverage 8.3% (n=1209)')
    expect(pooled).toHaveTextContent('101 decided of 1209 claims')
  })

  it('shows the graded / unmeasured split, the window and the as-of', () => {
    render(<EvalGraderRoster roster={LIVE} />)
    expect(screen.getByTestId('grader-roster-desks')).toHaveTextContent(
      '59 desks graded · 173 unmeasured',
    )
    expect(screen.getByTestId('grader-roster-window')).toHaveTextContent(
      '7-night window',
    )
    // The population, named: compositions are graded against the same
    // references as the desks they compose over and are never pooled with them.
    expect(screen.getByTestId('grader-roster-grain')).toHaveTextContent('desk grain')
    expect(screen.getByTestId('grader-roster-asof')).toHaveTextContent('as of 2.0h ago')
  })

  it('reads an unmeasured desk as unmeasured — and its 0 coverage as a number', () => {
    render(<EvalGraderRoster roster={LIVE} />)
    const key = 'country_watch_ml:economic_stress'

    const correctness = screen.getByTestId(`grader-roster-correctness-${key}`)
    expect(correctness).toHaveTextContent('unmeasured (0 decided)')
    expect(correctness.textContent).not.toContain('0.0%')
    expect(correctness.textContent).not.toContain('0%')

    // The coverage IS shown, because the reference bearing on nothing is a
    // measurement. "we never measured this" and "we measured nothing in it"
    // are different facts and must not look alike.
    expect(screen.getByTestId(`grader-roster-coverage-${key}`)).toHaveTextContent(
      '0.0% (n=12)',
    )
  })

  it('sorts the thinnest coverage first', () => {
    render(<EvalGraderRoster roster={LIVE} />)
    const rows = within(screen.getByTestId('grader-roster-table')).getAllByTestId(
      /^grader-roster-row-/,
    )
    expect(rows.map((r) => r.getAttribute('data-testid'))).toEqual([
      'grader-roster-row-country_watch_ml:economic_stress',
      'grader-roster-row-country_g20_br:water_stress',
      'grader-roster-row-country_g20_tr:escalation',
    ])
  })

  it('keeps the server badge verbatim behind every row', () => {
    render(<EvalGraderRoster roster={LIVE} />)
    expect(
      screen.getByTestId('grader-roster-row-country_g20_br:water_stress'),
    ).toHaveAttribute(
      'title',
      'correctness 50.0% (1/2) · coverage 10.0% (n=20) · as of 2026-09-25 · ' +
        'single-family · reference stale (9.0 d)',
    )
  })

  it('names a stale reference and a single-family number on the row', () => {
    render(<EvalGraderRoster roster={LIVE} />)
    const key = 'country_g20_br:water_stress'
    expect(screen.getByTestId(`grader-roster-reference-${key}`)).toHaveTextContent(
      'stale (9.0 d)',
    )
    expect(screen.getByTestId(`grader-roster-family-${key}`)).toHaveTextContent(
      'single-family',
    )
    expect(
      screen.getByTestId('grader-roster-family-country_g20_tr:escalation'),
    ).toHaveTextContent('—')
  })

  it('renders the route honesty note verbatim', () => {
    render(<EvalGraderRoster roster={LIVE} />)
    expect(screen.getByTestId('grader-roster-honesty-note')).toHaveTextContent(
      HONESTY_NOTE,
    )
  })

  it('shows "no grader rows yet" — never zeros — when nothing is graded', () => {
    const empty: GraderRoster = {
      available: false,
      nights: 7,
      grain: 'desk',
      as_of: null,
      desks: [],
      roster: {
        desks_graded: 0,
        desks_unmeasured: 0,
        correctness_mean_of_desks: null,
        coverage_mean_of_desks: null,
        claims_pooled: {
          n_claims: 0,
          n_decided: 0,
          n_contains: 0,
          correctness_pooled: null,
          coverage_pooled: null,
        },
      },
      honesty_note: HONESTY_NOTE,
    }
    render(<EvalGraderRoster roster={empty} />)
    expect(screen.getByTestId('grader-roster-empty')).toHaveTextContent(
      'no grader rows yet',
    )
    expect(screen.queryByTestId('grader-roster-table')).toBeNull()
    expect(screen.queryByTestId('grader-roster-headline')).toBeNull()
    expect(screen.queryByTestId('grader-roster-asof')).toBeNull()
    expect(screen.queryByText('0.0%')).toBeNull()
  })

  it('reads an absent payload the same honest way', () => {
    render(<EvalGraderRoster roster={null} />)
    expect(screen.getByTestId('grader-roster-empty')).toBeInTheDocument()
    expect(screen.queryByTestId('grader-roster-table')).toBeNull()
  })
})
