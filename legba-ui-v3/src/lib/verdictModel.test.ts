/**
 * Unit tests for the verdict vocabulary — focused on the P0-4 structural
 * verify-exemption classification (`unverified — structural`).
 *
 * Locks: the analyst-id registry mirror, the server `verify_exempt` stamp
 * override, the `structural` flag on `buildVerdict`, the honest default
 * (unknown analyst → NOT structural), and that a real verify block always
 * wins over the structural label (confidence axis leaves `unassessed`).
 */
import { describe, it, expect } from 'vitest'
import {
  STRUCTURAL_EXEMPT_NOTE,
  STRUCTURAL_VERIFY_EXEMPT_ANALYSTS,
  buildVerdict,
  isStructuralExempt,
  isStructuralVerified,
} from './verdictModel'

describe('isStructuralExempt', () => {
  it('classifies every registry analyst as structural-exempt', () => {
    for (const id of STRUCTURAL_VERIFY_EXEMPT_ANALYSTS) {
      expect(isStructuralExempt(id)).toBe(true)
    }
  })

  it('names the load-bearing structural analysts explicitly', () => {
    // The live verify-exempt finding emitters (confirmed against the DB:
    // zero faithfulness critiques over 14 days) — a registry regression on
    // any of these would silently unbadge live rows.
    for (const id of [
      'graph_mining',
      'thematic_proposal',
      'indicator_tracker',
      'calibration_tracking',
      'unit_correctness_scorer',
      'composition_lineage_sweep',
      'situation_clustering',
      'collection_gap',
    ]) {
      expect(isStructuralExempt(id)).toBe(true)
    }
  })

  it('never classifies a verify-covered or unknown analyst as structural', () => {
    expect(isStructuralExempt('country_assessor')).toBe(false)
    expect(isStructuralExempt('escalation')).toBe(false)
    expect(isStructuralExempt('world_assessor')).toBe(false)
    expect(isStructuralExempt('country_composition')).toBe(false)
    expect(isStructuralExempt('some_future_analyst')).toBe(false)
    expect(isStructuralExempt(null)).toBe(false)
    expect(isStructuralExempt(undefined)).toBe(false)
    expect(isStructuralExempt('')).toBe(false)
  })

  it('honors the authoritative server stamp regardless of analyst id', () => {
    // A REST row stamped by the reads API is structural even if the client
    // mirror lags behind the server registry.
    expect(isStructuralExempt('brand_new_mining_analyst', 'structural')).toBe(true)
    // A non-structural stamp value never classifies.
    expect(isStructuralExempt('country_assessor', 'something_else')).toBe(false)
    expect(isStructuralExempt('country_assessor', null)).toBe(false)
  })
})

describe('buildVerdict structural flag', () => {
  it('flags a structural analyst finding (unassessed + structural)', () => {
    const v = buildVerdict({ confidence: 1.0, analystId: 'graph_mining' })
    expect(v.structural).toBe(true)
    expect(v.confidence).toBe('unassessed')
  })

  it('flags via the server verify_exempt stamp alone', () => {
    const v = buildVerdict({ confidence: 1.0, verifyExempt: 'structural' })
    expect(v.structural).toBe(true)
    expect(v.confidence).toBe('unassessed')
  })

  it('does not flag a verified LLM read, and verify state still derives', () => {
    const v = buildVerdict({
      confidence: 0.8,
      analystId: 'country_assessor',
      verification: { faithfulness_score: 0.9, judge_status: 'llm' },
      citationCount: 3,
    })
    expect(v.structural).toBe(false)
    expect(v.confidence).toBe('high')
  })

  it('an unverified non-structural finding stays a bare unverified', () => {
    const v = buildVerdict({ confidence: 0.7, analystId: 'country_assessor' })
    expect(v.structural).toBe(false)
    expect(v.confidence).toBe('unassessed')
  })

  it('a real verify block wins the confidence axis even when structural', () => {
    // Defensive: should a structural analyst ever gain a verify pass, the
    // measured level renders (the badge only shows `unverified — structural`
    // in the unassessed state).
    const v = buildVerdict({
      confidence: 1.0,
      analystId: 'graph_mining',
      verification: { faithfulness_score: 0.7, judge_status: 'deterministic' },
    })
    expect(v.structural).toBe(true)
    expect(v.confidence).toBe('moderate')
  })
})

describe('STRUCTURAL_EXEMPT_NOTE', () => {
  it('is the one-line honest explanation surfaces render', () => {
    expect(STRUCTURAL_EXEMPT_NOTE).toBe(
      'deterministic structural read — not routed through the faithfulness verify pass',
    )
  })
})

describe('C2b structural-verified badge', () => {
  it('a passing structural critique is still structural AND verified', () => {
    expect(isStructuralExempt('geo_convergence_scan', 'structural-verified')).toBe(true)
    expect(isStructuralVerified('structural-verified')).toBe(true)
    const v = buildVerdict({ confidence: 1.0, verifyExempt: 'structural-verified' })
    expect(v.structural).toBe(true)
    expect(v.structuralVerified).toBe(true)
  })

  it('an unverified structural row is structural but NOT verified', () => {
    expect(isStructuralVerified('structural')).toBe(false)
    const v = buildVerdict({ confidence: 1.0, verifyExempt: 'structural' })
    expect(v.structural).toBe(true)
    expect(v.structuralVerified).toBe(false)
  })

  it('a non-structural row is never verified', () => {
    expect(isStructuralVerified(null)).toBe(false)
    expect(buildVerdict({ confidence: 0.7, analystId: 'country_assessor' }).structuralVerified).toBe(
      false,
    )
  })
})
