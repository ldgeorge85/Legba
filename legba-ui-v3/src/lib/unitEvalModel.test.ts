/**
 * Unit test for the per-unit eval scoreboard data layer (P2-T6).
 *
 * Locks the `findUnitScore` lookup: a bounded unit's row is matched by its
 * analyst id; a non-unit id (or an empty board) resolves to null so the badge
 * renders nothing rather than a fabricated score.
 */
import { describe, it, expect } from 'vitest'
import { findUnitScore, type EvalScores } from './unitEvalModel'

const BOARD: EvalScores = {
  scored_at: '2026-06-30T22:47:32Z',
  units: [
    {
      unit: 'energy_security',
      faithfulness: 0.09,
      correctness_vs_reference: null,
      n_labeled: 0,
      n_findings: 2,
      status: 'no gold labels',
      badge: 'verified | faithfulness 0.09 | unmeasured (0 labels)',
    },
    {
      unit: 'leadership_transition',
      faithfulness: 0.23,
      correctness_vs_reference: 0.78,
      n_labeled: 12,
      n_findings: 1,
      status: 'scored',
      badge: 'verified | faithfulness 0.23 | correctness 0.78 (n=12)',
    },
  ],
}

describe('findUnitScore', () => {
  it('matches a bounded unit by its analyst id', () => {
    const row = findUnitScore(BOARD, 'leadership_transition')
    expect(row?.badge).toBe('verified | faithfulness 0.23 | correctness 0.78 (n=12)')
    expect(row?.correctness_vs_reference).toBe(0.78)
  })

  it('returns null for a non-unit analyst id (no fabricated badge)', () => {
    expect(findUnitScore(BOARD, 'country_assessor')).toBeNull()
  })

  it('returns null for a null id or an empty/absent board', () => {
    expect(findUnitScore(BOARD, null)).toBeNull()
    expect(findUnitScore(BOARD, undefined)).toBeNull()
    expect(findUnitScore(null, 'energy_security')).toBeNull()
    expect(findUnitScore({ scored_at: null, units: [] }, 'energy_security')).toBeNull()
  })

  it('preserves the honest unmeasured badge for an unlabeled unit', () => {
    const row = findUnitScore(BOARD, 'energy_security')
    expect(row?.correctness_vs_reference).toBeNull()
    expect(row?.badge).toContain('unmeasured (0 labels)')
    expect(row?.badge).not.toContain('correctness ')
  })
})
