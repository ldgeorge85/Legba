/**
 * Unit tests for the gap-strip state derivation (7b-ii).
 *
 * Pins the four states and the two honesty rules that would otherwise be easy
 * to get quietly wrong:
 *
 *   * `reason === 'no-finding'` must NOT read as `insufficient` — that reason
 *     is the scorecard's own window seeing nothing, not a grading verdict on
 *     the finding the `/findings` read actually has.
 *   * a real exclusion reason (`verify-failed`, `below-floor`,
 *     `low-faithfulness`, `no-severity-tag`) DOES read as `insufficient`, even
 *     when the read is otherwise fresh — insufficiency outranks staleness.
 *   * `stale` is measured against the UNIT'S OWN cadence with grace, never a
 *     shared constant across units.
 */
import { describe, it, expect } from 'vitest'
import {
  GAP_STRIP_UNITS,
  deriveGapCell,
  deriveGapStrip,
  type GapLatestFinding,
  type GapUnit,
} from './gapStripModel'
import type { DimensionBand } from './evalOps'

const UNIT: GapUnit = { id: 'escalation', label: 'Escalation', short: 'ESC', cadenceHours: 12 }
const NOW = new Date('2026-09-24T12:00:00Z')

function finding(over: Partial<GapLatestFinding> = {}): GapLatestFinding {
  return { id: 'f1', producedAt: '2026-09-24T06:00:00Z', severity: 'high', ...over }
}

function band(over: Partial<DimensionBand> = {}): DimensionBand {
  return {
    band: 'high',
    basis: ['f1'],
    severity_tag: 'high',
    effective_confidence: 0.8,
    confidence: 0.8,
    critic_score: 0.91,
    damped: false,
    reason: '',
    produced_at: '2026-09-24T06:00:00Z',
    eval: { faithfulness: 0.9, correctness_vs_reference: null, n_labeled: 3, faithfulness_flagged: false },
    ...over,
  }
}

describe('deriveGapCell', () => {
  it('reads `none` when no finding exists at all, even with a dimension row', () => {
    const cell = deriveGapCell({ unit: UNIT, latestFinding: null, dimension: null, now: NOW })
    expect(cell.state).toBe('none')
    expect(cell.findingId).toBeNull()
    expect(cell.producedAt).toBeNull()
  })

  it('reads `current` for a read inside the unit\'s cadence with a real band', () => {
    const cell = deriveGapCell({
      unit: UNIT,
      latestFinding: finding({ producedAt: '2026-09-24T06:00:00Z' }), // 6h old, cadence 12h
      dimension: band(),
      now: NOW,
    })
    expect(cell.state).toBe('current')
    expect(cell.findingId).toBe('f1')
    expect(cell.judgeScore).toBe(0.91)
  })

  it('reads `stale` for a real-banded read older than cadence × grace', () => {
    const cell = deriveGapCell({
      unit: UNIT,
      // 19h old vs a 12h cadence × 1.5 grace = 18h threshold.
      latestFinding: finding({ producedAt: '2026-09-23T17:00:00Z' }),
      dimension: band({ produced_at: '2026-09-23T17:00:00Z' }),
      now: NOW,
    })
    expect(cell.state).toBe('stale')
  })

  it('stays `current` just inside the grace window', () => {
    const cell = deriveGapCell({
      unit: UNIT,
      // 17h old — under the 18h (12h × 1.5) threshold.
      latestFinding: finding({ producedAt: '2026-09-23T19:00:00Z' }),
      dimension: band({ produced_at: '2026-09-23T19:00:00Z' }),
      now: NOW,
    })
    expect(cell.state).toBe('current')
  })

  it('reads `insufficient` when a fresh read was excluded for a real reason', () => {
    const cell = deriveGapCell({
      unit: UNIT,
      latestFinding: finding({ producedAt: '2026-09-24T11:00:00Z' }), // 1h old — well within cadence
      dimension: band({
        band: 'insufficient-evidence',
        basis: [],
        reason: 'low-faithfulness',
        produced_at: null,
        critic_score: 0.2,
      }),
      now: NOW,
    })
    expect(cell.state).toBe('insufficient')
    expect(cell.reason).toBe('low-faithfulness')
    // The click target is still the real read the scorecard excluded.
    expect(cell.findingId).toBe('f1')
    expect(cell.judgeScore).toBe(0.2)
  })

  it('does NOT read `insufficient` for reason=no-finding — that is a window fact, not a grade', () => {
    const cell = deriveGapCell({
      unit: UNIT,
      // A finding the /findings read still has, older than the scorecard's window.
      latestFinding: finding({ producedAt: '2026-09-01T00:00:00Z' }),
      dimension: band({ band: 'insufficient-evidence', basis: [], reason: 'no-finding', produced_at: null }),
      now: NOW,
    })
    expect(cell.state).toBe('stale')
  })

  it('falls back to a cadence-only read when no scorecard dimension exists', () => {
    const cell = deriveGapCell({
      unit: UNIT,
      latestFinding: finding({ producedAt: '2026-09-24T06:00:00Z' }),
      dimension: undefined,
      now: NOW,
    })
    expect(cell.state).toBe('current')
    expect(cell.judgeScore).toBeNull()
  })
})

describe('deriveGapStrip', () => {
  it('produces exactly one cell per configured unit, in order', () => {
    const latestByUnit = new Map<string, GapLatestFinding>([
      ['leadership_transition', finding({ id: 'lt1' })],
    ])
    const cells = deriveGapStrip({ latestByUnit, dimensions: {}, now: NOW })
    expect(cells).toHaveLength(GAP_STRIP_UNITS.length)
    expect(cells.map((c) => c.unitId)).toEqual(GAP_STRIP_UNITS.map((u) => u.id))
    expect(cells[0].state).toBe('current')
    expect(cells[1].state).toBe('none')
  })

  it('carries all nine bounded units', () => {
    expect(GAP_STRIP_UNITS.map((u) => u.id)).toEqual([
      'leadership_transition',
      'energy_security',
      'escalation',
      'narrative_coordination',
      'internal_stability',
      'military_posture',
      'economic_coercion',
      'proliferation_watch',
      'disruption_status',
    ])
  })
})
