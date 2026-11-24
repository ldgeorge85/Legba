/**
 * X4 READPATH (W11-F53) — the registry's pending|absent state, read never inferred.
 */
import { describe, expect, it } from 'vitest'
import {
  UNVERIFIED_LABEL,
  asStateBlock,
  isVerifyExemptState,
  unverifiedExplain,
  unverifiedStateOf,
} from './unverifiedState'

const PENDING = {
  score_state: 'pending',
  since: '2026-10-09T04:41:26.595762+00:00',
  judge_window_s: 900,
  reason: 'awaiting_verify',
}
const ABSENT = { ...PENDING, score_state: 'absent', reason: 'no_verdict' }

describe('unverifiedStateOf', () => {
  it('reads the judgment weight: the state block IS `verification`', () => {
    expect(unverifiedStateOf({ verification: PENDING })).toMatchObject({ state: 'pending', basis: 'registry' })
    expect(unverifiedStateOf({ verification: ABSENT })).toMatchObject({ state: 'absent', basis: 'registry' })
  })

  it('reads the full weight: `verification` null, `verification_state` beside it', () => {
    expect(unverifiedStateOf({ verification: null, verification_state: PENDING })?.state).toBe('pending')
    expect(unverifiedStateOf({ verification: null, verification_state: ABSENT })?.state).toBe('absent')
  })

  it('a REAL verify block is not unverified — the verdict badge speaks for it', () => {
    expect(unverifiedStateOf({ verification: { faithfulness_score: 0.25, judge_status: 'llm' } })).toBeNull()
    // `score_state` is also a real block's leaf (Q-1): `unassessable` is a verdict, not this state.
    expect(unverifiedStateOf({ verification: { faithfulness_score: null, score_state: 'unassessable' } })).toBeNull()
    // A block carrying a number is never read as a state block, whatever it says.
    expect(asStateBlock({ score_state: 'pending', faithfulness_score: 0 })).toBeNull()
  })

  it('a live-tail row arrived ahead of its verify pass → pending', () => {
    expect(unverifiedStateOf({ verification: null, live: true })).toMatchObject({ state: 'pending', basis: 'live_tail' })
  })

  it('no block and no state (an older registry) → absent, never pending, never plain', () => {
    const info = unverifiedStateOf({ verification: null })
    expect(info).toMatchObject({ state: 'absent', basis: 'unstated' })
    expect(unverifiedExplain(info!)).toMatch(/did not say/)
  })

  it('the two labels are the brief’s words, verbatim', () => {
    expect(UNVERIFIED_LABEL).toEqual({
      pending: 'unverified · judge pending',
      absent: 'unverified · not judged',
    })
  })

  it('names verify-exempt and carries the clock in the explanation', () => {
    const exempt = unverifiedStateOf({ verification: { ...ABSENT, reason: 'verify_exempt' } })
    expect(isVerifyExemptState(exempt)).toBe(true)
    expect(isVerifyExemptState(unverifiedStateOf({ verification: ABSENT }))).toBe(false)
    const pending = unverifiedStateOf({ verification: PENDING })!
    expect(unverifiedExplain(pending)).toContain('within 15 minutes')
    expect(unverifiedExplain(pending)).toContain('2026-10-09 04:41:26Z')
    expect(unverifiedExplain(unverifiedStateOf({ verification: ABSENT })!)).toMatch(/Nothing will grade/)
  })
})
