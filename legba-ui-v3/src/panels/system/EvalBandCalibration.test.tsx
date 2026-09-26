/**
 * Component test for the Eval Scorecard's band-calibration section.
 *
 * The fixtures are shaped from a real read of
 * `GET /api/v1/v3/eval/calibration` (2026-09-25): the same keys, the same
 * nesting, the same `claims_total` / `resolution_spec` / two-horizon /
 * `by_direction` / `by_dimension` / `no_brier` / `honesty_note` block the
 * registry actually serves. `LIVE_OPEN` is that payload verbatim, trimmed to
 * two dimensions — the live tracker has 48 claims logged and none resolved, so
 * every rate on it is `null`, which is exactly the state this section must NOT
 * render as 0%. `GRADED` keeps that shape and fills the horizons with resolved
 * outcomes, because no claim on the live instance has reached its 14d horizon
 * under the current stamp yet.
 *
 * What is asserted is the honesty contract, not the styling:
 *   - `available: false` reads "not measured yet" and shows NO as-of.
 *   - `available: true` with zero claims is a DIFFERENT state, and it does
 *     have an as-of (the tracker ran).
 *   - a null rate renders the honest empty label, never 0%.
 *   - the two excluded outcomes render, marked excluded.
 *   - `no_brier` renders as the statement it stands for, and the route's own
 *     `honesty_note` renders verbatim beside it.
 */

import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { EvalBandCalibration } from './EvalBandCalibration'
import type { BandCalibrationSection } from '@/lib/evalOps'

/** An unresolved horizon block, exactly as the live route serves one. */
function openBlock(n: number) {
  return {
    open: n,
    scored: 0,
    outcomes: {},
    resolved: 0,
    reverted: 0,
    confirmed: 0,
    reversal_rate: null,
    persistence_rate: null,
    excluded_insufficient: 0,
    excluded_unresolvable: 0,
  }
}

const HONESTY_NOTE =
  'Band-persistence and reversal rates are ordinal stability measures over later ' +
  'scorecard rows. Bands are categorical risk verdicts, not probabilities: no Brier ' +
  'score, Brier skill score, or forecast-skill claim exists (or can exist) for this ' +
  'harness.'

/** Verbatim from the live route, trimmed to two of its seven dimensions. */
const LIVE_OPEN: BandCalibrationSection = {
  available: true,
  produced_at: '2026-09-25T03:35:02.701788+00:00',
  claims_total: 48,
  resolution_spec: 'hard_band_at_horizon_v1',
  horizons: { '14d': openBlock(48), '28d': openBlock(48) },
  by_direction: {
    improvement: { claims: 21, '14d': openBlock(21), '28d': openBlock(21) },
    deterioration: { claims: 27, '14d': openBlock(27), '28d': openBlock(27) },
  },
  by_dimension: {
    escalation: { claims: 2, '14d': openBlock(2), '28d': openBlock(2) },
    internal_stability: { claims: 11, '14d': openBlock(11), '28d': openBlock(11) },
  },
  no_brier: true,
  honesty_note: HONESTY_NOTE,
  refs: ['5cc4a6fb-9142-45a9-a923-5e66468f46c4'],
}

/** The same shape with the horizons resolved — what the live payload becomes
 *  once its claims pass 14d/28d. 14d: 6 held + 2 worsened + 2 reverted scored,
 *  plus one of each abstain held OUT of the rate denominator. */
const GRADED: BandCalibrationSection = {
  ...LIVE_OPEN,
  claims_total: 20,
  horizons: {
    '14d': {
      open: 2,
      resolved: 12,
      outcomes: { held: 6, worsened: 2, reverted: 2, insufficient: 1, unresolvable: 1 },
      confirmed: 8,
      reverted: 2,
      scored: 10,
      excluded_insufficient: 1,
      excluded_unresolvable: 1,
      persistence_rate: 0.8,
      reversal_rate: 0.2,
    },
    '28d': {
      open: 8,
      resolved: 6,
      outcomes: { held: 3, reverted: 3 },
      confirmed: 3,
      reverted: 3,
      scored: 6,
      excluded_insufficient: 0,
      excluded_unresolvable: 0,
      persistence_rate: 0.5,
      reversal_rate: 0.5,
    },
  },
  by_direction: {
    deterioration: {
      claims: 12,
      '14d': {
        open: 0,
        resolved: 12,
        outcomes: { held: 9, reverted: 3 },
        confirmed: 9,
        reverted: 3,
        scored: 12,
        excluded_insufficient: 0,
        excluded_unresolvable: 0,
        persistence_rate: 0.75,
        reversal_rate: 0.25,
      },
      '28d': openBlock(12),
    },
    improvement: { claims: 8, '14d': openBlock(8), '28d': openBlock(8) },
  },
  by_dimension: {
    internal_stability: {
      claims: 11,
      '14d': {
        open: 1,
        resolved: 10,
        outcomes: { held: 5, reverted: 5 },
        confirmed: 5,
        reverted: 5,
        scored: 10,
        excluded_insufficient: 0,
        excluded_unresolvable: 0,
        persistence_rate: 0.5,
        reversal_rate: 0.5,
      },
      '28d': openBlock(11),
    },
    escalation: { claims: 2, '14d': openBlock(2), '28d': openBlock(2) },
  },
}

describe('EvalBandCalibration — the two absences are different states', () => {
  it('available:false reads "not measured yet" and shows no as-of', () => {
    render(<EvalBandCalibration section={{ ...LIVE_OPEN, available: false }} />)
    expect(screen.getByTestId('band-calibration-empty')).toBeInTheDocument()
    expect(screen.getByTestId('band-calibration-unavailable')).toHaveTextContent(
      /not measured yet/,
    )
    // No as-of, no claim count, no rate: nothing has been measured.
    expect(screen.queryByTestId('band-calibration-asof')).not.toBeInTheDocument()
    expect(screen.queryByTestId('band-calibration-claims-total')).not.toBeInTheDocument()
    expect(screen.queryByTestId('band-calibration-no-claims')).not.toBeInTheDocument()
  })

  it('a null section (the read itself failed) reads the same, never a zeroed table', () => {
    render(<EvalBandCalibration section={null} />)
    expect(screen.getByTestId('band-calibration-unavailable')).toBeInTheDocument()
    expect(screen.queryByTestId('band-calibration-outcomes-14d')).not.toBeInTheDocument()
  })

  it('available with zero claims is a DIFFERENT state, and it does carry an as-of', () => {
    render(<EvalBandCalibration section={{ ...LIVE_OPEN, claims_total: 0 }} />)
    const el = screen.getByTestId('band-calibration-no-claims')
    expect(el).toHaveTextContent(/no band transitions graded yet/)
    // "the tracker ran <when>" — the distinction from "not measured yet".
    expect(el).toHaveTextContent(/the tracker ran .*ago/)
    expect(screen.queryByTestId('band-calibration-unavailable')).not.toBeInTheDocument()
  })
})

describe('EvalBandCalibration — the live payload, whose rates are all null', () => {
  it('renders the spec, the claim total and the as-of as the header', () => {
    render(<EvalBandCalibration section={LIVE_OPEN} />)
    expect(screen.getByTestId('band-calibration-resolution-spec')).toHaveTextContent(
      'hard_band_at_horizon_v1',
    )
    expect(screen.getByTestId('band-calibration-claims-total')).toHaveTextContent(
      '48 claims logged',
    )
    expect(screen.getByTestId('band-calibration-asof')).toHaveTextContent(/as of .*ago/)
  })

  it('never coerces a null rate to 0% — 48 logged, none scored', () => {
    render(<EvalBandCalibration section={LIVE_OPEN} />)
    for (const hz of ['14d', '28d']) {
      expect(screen.getByTestId(`band-calibration-persistence-${hz}`)).toHaveTextContent(
        '— (no scored claims yet)',
      )
      expect(screen.getByTestId(`band-calibration-reversal-${hz}`)).toHaveTextContent(
        '— (no scored claims yet)',
      )
      expect(screen.getByTestId(`band-calibration-persistence-${hz}`)).not.toHaveTextContent(
        '0%',
      )
      // An empty outcome map is an absence of outcomes, not six zeroes.
      expect(
        screen.getByTestId(`band-calibration-outcomes-empty-${hz}`),
      ).toHaveTextContent(/not the same as every outcome being zero/)
      expect(screen.queryByTestId(`band-calibration-outcomes-${hz}`)).not.toBeInTheDocument()
    }
  })

  it('renders both slice tables with their claim counts, rates still absent', () => {
    render(<EvalBandCalibration section={LIVE_OPEN} />)
    expect(
      screen.getByTestId('band-calibration-by-direction-deterioration'),
    ).toHaveTextContent('27')
    expect(screen.getByTestId('band-calibration-by-direction-improvement')).toHaveTextContent(
      '21',
    )
    expect(
      screen.getByTestId('band-calibration-by-dimension-internal_stability-14d'),
    ).toHaveTextContent('— (no scored claims yet) (n=0)')
  })
})

describe('EvalBandCalibration — a graded section', () => {
  it('orders 14d before 28d and renders each horizon rate with its denominator', () => {
    render(<EvalBandCalibration section={GRADED} />)
    const ids = screen
      .getAllByTestId(/^band-calibration-horizon-/)
      .map((el) => el.getAttribute('data-testid'))
    expect(ids).toEqual(['band-calibration-horizon-14d', 'band-calibration-horizon-28d'])
    expect(screen.getByTestId('band-calibration-persistence-14d')).toHaveTextContent('80%')
    expect(screen.getByTestId('band-calibration-reversal-14d')).toHaveTextContent('20%')
    expect(screen.getByTestId('band-calibration-scored-14d')).toHaveTextContent(
      'n scored=10 · 12 resolved of 14 (2 open)',
    )
    expect(screen.getByTestId('band-calibration-persistence-28d')).toHaveTextContent('50%')
  })

  it('renders the outcome table per horizon, with shares over RESOLVED', () => {
    render(<EvalBandCalibration section={GRADED} />)
    const table = screen.getByTestId('band-calibration-outcomes-14d')
    expect(table).toHaveTextContent('share of resolved')
    // 6 held of 12 resolved.
    expect(screen.getByTestId('band-calibration-outcome-14d-held')).toHaveTextContent('50%')
    expect(screen.getByTestId('band-calibration-outcome-14d-worsened')).toHaveTextContent('17%')
  })

  it('keeps the two abstains visible and marks them out of the rate denominator', () => {
    render(<EvalBandCalibration section={GRADED} />)
    for (const o of ['insufficient', 'unresolvable']) {
      const row = screen.getByTestId(`band-calibration-outcome-14d-${o}`)
      expect(row).toHaveTextContent('excluded from both rates')
      expect(row.className).toContain('text-slate-500')
    }
    // The confirming outcomes are not muted.
    expect(screen.getByTestId('band-calibration-outcome-14d-held').className).toContain(
      'text-slate-300',
    )
  })

  it('splits by direction and by dimension, most claims first, each rate with its n', () => {
    render(<EvalBandCalibration section={GRADED} />)
    const dirs = Array.from(
      screen.getByTestId('band-calibration-by-direction').querySelectorAll('tbody tr'),
    ).map((el) => el.getAttribute('data-testid'))
    expect(dirs).toEqual([
      'band-calibration-by-direction-deterioration',
      'band-calibration-by-direction-improvement',
    ])
    expect(
      screen.getByTestId('band-calibration-by-direction-deterioration-14d'),
    ).toHaveTextContent('75% (n=12)')
    expect(
      screen.getByTestId('band-calibration-by-dimension-internal_stability-14d'),
    ).toHaveTextContent('50% (n=10)')
  })

  it('renders an empty slice map as an absence, never a table of zeroes', () => {
    render(<EvalBandCalibration section={{ ...GRADED, by_dimension: {} }} />)
    expect(screen.getByTestId('band-calibration-by-dimension-empty')).toHaveTextContent(
      'no per-dimension split on this finding',
    )
    expect(screen.queryByTestId('band-calibration-by-dimension')).not.toBeInTheDocument()
  })
})

describe('EvalBandCalibration — the no-Brier contract', () => {
  it('spells out what no_brier stands for AND renders the route note verbatim', () => {
    render(<EvalBandCalibration section={GRADED} />)
    expect(screen.getByTestId('band-calibration-no-brier')).toHaveTextContent(
      /No Brier score, Brier skill score or forecast-skill claim exists/,
    )
    expect(screen.getByTestId('band-calibration-honesty-note')).toHaveTextContent(
      HONESTY_NOTE,
    )
  })

  it('a missing honesty_note falls back to the statement — never a blank line', () => {
    render(<EvalBandCalibration section={{ ...GRADED, honesty_note: null }} />)
    expect(screen.getByTestId('band-calibration-honesty-note')).toHaveTextContent(
      /bands are ordinal risk categories, not probabilities/,
    )
  })

  it('never uses the word "Brier" as a label for a rate this section reports', () => {
    render(<EvalBandCalibration section={GRADED} />)
    // Every rate cell carries a percentage or the honest dash; none of them
    // is titled with a skill/probability word.
    for (const hz of ['14d', '28d']) {
      for (const kind of ['persistence', 'reversal']) {
        expect(
          screen.getByTestId(`band-calibration-${kind}-${hz}`).textContent,
        ).not.toMatch(/brier|probabilit|skill/i)
      }
    }
  })

  it('an absent resolution_spec renders the dash, never an invented spec id', () => {
    render(<EvalBandCalibration section={{ ...GRADED, resolution_spec: null }} />)
    expect(screen.getByTestId('band-calibration-resolution-spec')).toHaveTextContent('—')
  })
})
