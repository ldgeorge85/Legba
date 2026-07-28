/**
 * Tests for claimVerdicts (P1-8) — the per-citation-chip verify verdict,
 * derived ONLY from what the verification block actually records.
 */
import { describe, it, expect } from 'vitest'
import { claimReasonLabel, claimVerdictForMarker, markerOrdinal } from './claimVerdicts'

describe('markerOrdinal', () => {
  it('extracts the ordinal from both marker forms', () => {
    expect(markerOrdinal('[8]')).toBe(8)
    expect(markerOrdinal('[[ref:3]]')).toBe(3)
  })
  it('null for a digitless (legacy uuid) marker', () => {
    expect(markerOrdinal('[abc-def]')).toBe(null)
  })
})

describe('claimVerdictForMarker', () => {
  const verifiedLlm = {
    faithfulness_score: 0.9091,
    checkable_claims: 11,
    supported_claims: 10,
    judge_status: 'llm',
    unsupported_spans: [
      { text: 'An uncited assertion.', reason: 'judge_unsupported', markers: [] },
      { text: 'Hedged claim over ref 5.', reason: 'hedge_laundering', markers: [5] },
      { text: 'Contradicted claim over ref 2.', reason: 'judge_contradicted', markers: [2, 5] },
    ],
  }

  it('no verification block → explicit not-recorded (never fabricated)', () => {
    const v = claimVerdictForMarker(null, '[3]')
    expect(v.kind).toBe('not-recorded')
    expect(v.label).toBe('claim-level verdict not recorded')
    expect(v.spans).toEqual([])
  })

  it('deterministic floor only → not-checked with an honest label', () => {
    const v = claimVerdictForMarker(
      { judge_status: 'deterministic', unsupported_spans: [] },
      '[3]',
    )
    expect(v.kind).toBe('not-checked')
    expect(v.label).toMatch(/LLM judge did not run/)
  })

  it('LLM judge ran + chip not named by any flag → not-flagged with pooled context', () => {
    const v = claimVerdictForMarker(verifiedLlm, '[[ref:9]]')
    expect(v.kind).toBe('not-flagged')
    expect(v.checkable).toBe(11)
    expect(v.supported).toBe(10)
  })

  it('a flagged span naming the ordinal surfaces its reason + claim text', () => {
    const v = claimVerdictForMarker(verifiedLlm, '[[ref:5]]')
    // [5] is named by hedge_laundering AND judge_contradicted → worst wins.
    expect(v.kind).toBe('contradicted')
    expect(v.label).toBe('contradicted by the verify judge')
    expect(v.spans.map((s) => s.reason)).toEqual(['judge_contradicted', 'hedge_laundering'])
    expect(v.spans[0].text).toBe('Contradicted claim over ref 2.')
  })

  it('a non-judge flag alone → flagged with its reason label', () => {
    const v = claimVerdictForMarker(
      {
        judge_status: 'llm',
        unsupported_spans: [{ text: 'Hedged.', reason: 'hedge_laundering', markers: [7] }],
      },
      '[7]',
    )
    expect(v.kind).toBe('flagged')
    expect(v.label).toBe(claimReasonLabel('hedge_laundering'))
  })

  it('judge_unsupported naming the ordinal → unsupported', () => {
    const v = claimVerdictForMarker(
      {
        judge_status: 'llm',
        unsupported_spans: [{ text: 'Shaky.', reason: 'judge_unsupported', markers: [4] }],
      },
      '[4]',
    )
    expect(v.kind).toBe('unsupported')
    expect(v.label).toBe('unsupported by the verify judge')
  })

  it('markers [] spans (uncited claims) never attach to a chip', () => {
    const v = claimVerdictForMarker(
      {
        judge_status: 'llm',
        unsupported_spans: [{ text: 'Uncited.', reason: 'judge_unsupported', markers: [] }],
      },
      '[1]',
    )
    expect(v.kind).toBe('not-flagged')
  })

  it('tolerates string markers in a span (legacy tolerance)', () => {
    const v = claimVerdictForMarker(
      {
        judge_status: 'llm',
        unsupported_spans: [{ text: 'Legacy.', reason: 'judge_unsupported', markers: ['6'] }],
      },
      '[6]',
    )
    expect(v.kind).toBe('unsupported')
  })

  it('a digitless marker cannot match any span → falls through honestly', () => {
    const v = claimVerdictForMarker(verifiedLlm, '[not-a-number]')
    expect(v.kind).toBe('not-flagged')
  })
})
