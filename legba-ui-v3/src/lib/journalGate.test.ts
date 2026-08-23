/**
 * Tests for the journal-gate pure model.
 *
 * The two load-bearing behaviours: a proposal's diff renders as WHAT ACCEPTING
 * APPLIES (in the apply worker's own op vocabulary, including recognising a
 * diff no apply path would route), and the §7.5(b) protected-section mirror
 * agrees with the server's list so a would-be auto-reject is visible before the
 * click rather than as a 409 after it.
 */

import { describe, it, expect } from 'vitest'
import { ApiError } from '@/lib/api'
import type { JournalProposal } from '@/lib/api'
import {
  PROTECTED_PROMPT_PHRASES,
  decisionErrorText,
  decisionOutcomeText,
  isActionable,
  proposalEffect,
  proposalKindLabel,
  protectedSectionViolations,
  selfRevisionEvidenceSummary,
} from './journalGate'

/** A prompt that carries every protected clause — the "safe revision" fixture. */
const COMPLIANT_PROMPT = [
  'You are the journal. Poetry without evidence is noise.',
  'Cite every factual claim as [[ref:<uuid>]].',
  'Say plainly that the forecast pilot has no skill until it earns one.',
  'Never re-assert state that has been retired.',
  'You never write a fact.',
].join('\n')

describe('protectedSectionViolations (§7.5b client mirror)', () => {
  it('reports no drops for a prompt carrying every protected clause', () => {
    expect(protectedSectionViolations(COMPLIANT_PROMPT)).toEqual([])
  })

  it('is case-insensitive, like the server check', () => {
    expect(protectedSectionViolations(COMPLIANT_PROMPT.toUpperCase())).toEqual([])
  })

  it('names exactly the clauses a revision dropped', () => {
    const weakened = COMPLIANT_PROMPT.replace('You never write a fact.', '')
      .replace('Never re-assert state that has been retired.', '')
    expect(protectedSectionViolations(weakened).sort()).toEqual(
      ['never re-assert', 'never write a fact'].sort(),
    )
  })

  it('treats an empty prompt as dropping everything (never as vacuously clean)', () => {
    expect(protectedSectionViolations('')).toEqual([...PROTECTED_PROMPT_PHRASES])
  })
})

describe('proposalEffect — correction', () => {
  it('describes supersede_fact as retiring the stale row, not writing a new one', () => {
    const e = proposalEffect('correction', {
      op: 'supersede_fact',
      subject: 'Wagner Group',
      predicate: 'commander',
      value: 'Pavel Prigozhin',
    })
    expect(e.op).toBe('supersede_fact')
    expect(e.unrecognized).toBe(false)
    expect(e.summary).toContain('Closes the open fact')
    expect(e.summary).toContain('never writes a replacement fact')
    expect(e.fields.map((f) => f.key)).toEqual(['subject', 'predicate', 'corrected value'])
  })

  it('flags an incomplete supersede_fact as unroutable before the click', () => {
    const e = proposalEffect('correction', { op: 'supersede_fact', subject: 'x' })
    expect(e.unrecognized).toBe(true)
    expect(e.summary).toContain('INCOMPLETE')
  })

  it('recognises the routed correction sub-kinds', () => {
    for (const op of ['merge_entities', 'correct_situation']) {
      const e = proposalEffect('correction', { op, a: 1 })
      expect(e.unrecognized).toBe(false)
      expect(e.summary).toContain('existing')
    }
  })
})

describe('proposalEffect — change', () => {
  it('describes update_descriptor as a deep-merge into the current head', () => {
    const e = proposalEffect('change', {
      op: 'update_descriptor',
      family: 'target',
      descriptor_id: 'target.geo.iran',
      patch: { scope: { tags: ['news'] } },
    })
    expect(e.unrecognized).toBe(false)
    expect(e.summary).toContain('target/target.geo.iran')
    expect(e.body?.text).toContain('"tags"')
  })

  it('describes update_stack the same way, keyed on the component', () => {
    const e = proposalEffect('change', {
      op: 'update_stack',
      stack_id: 'llm.core',
      patch: { temperature: 1 },
    })
    expect(e.unrecognized).toBe(false)
    expect(e.fields[0]).toEqual({ key: 'stack component', value: 'llm.core' })
  })

  it('flags a patch that is not an object as unroutable', () => {
    const e = proposalEffect('change', {
      op: 'update_stack',
      stack_id: 'llm.core',
      patch: 'not-an-object',
    })
    expect(e.unrecognized).toBe(true)
  })
})

describe('proposalEffect — self_revision', () => {
  it('says the prompt would go LIVE and surfaces the full text verbatim', () => {
    const e = proposalEffect('self_revision', {
      target_analyst_id: 'journal_assessor',
      new_prompt_text: COMPLIANT_PROMPT,
    })
    expect(e.unrecognized).toBe(false)
    expect(e.summary).toContain('LIVE system prompt')
    expect(e.body?.text).toBe(COMPLIANT_PROMPT)
  })

  it('flags a self_revision missing its prompt text', () => {
    const e = proposalEffect('self_revision', { target_analyst_id: 'journal_assessor' })
    expect(e.unrecognized).toBe(true)
    expect(e.body).toBeUndefined()
  })
})

describe('proposalEffect — nothing routes it', () => {
  it('flags an op the kind does not dispatch', () => {
    const e = proposalEffect('correction', { op: 'update_stack', stack_id: 'x' })
    expect(e.unrecognized).toBe(true)
    expect(e.summary).toContain('would fail and archive')
  })

  it('flags an unknown proposal_kind', () => {
    const e = proposalEffect('teleport', { op: 'whatever' })
    expect(e.unrecognized).toBe(true)
  })

  it('flags a diff with no op at all', () => {
    const e = proposalEffect('change', {})
    expect(e.unrecognized).toBe(true)
    expect(e.summary).toContain('declares no op')
  })
})

describe('decisionErrorText', () => {
  it('names a 409 as the protected-section auto-reject and says nothing was applied', () => {
    const text = decisionErrorText(
      new ApiError(409, { detail: 'self_revision auto-rejected (protected section): x' }),
    )
    expect(text).toContain('Auto-rejected')
    expect(text).toContain('NOTHING was applied')
  })

  it('names a 422 as an apply failure and says nothing was applied', () => {
    const text = decisionErrorText(new ApiError(422, { detail: 'apply failed: bad diff' }))
    expect(text).toContain('Apply failed')
    expect(text).toContain('NOTHING was applied')
    expect(text).toContain('bad diff')
  })

  it('explains a 404 without euphemism', () => {
    expect(decisionErrorText(new ApiError(404, { detail: 'proposal not found' }))).toContain(
      'not found',
    )
  })

  it('falls through to the status + server detail for anything else', () => {
    expect(decisionErrorText(new ApiError(503, 'upstream down'))).toBe(
      'HTTP 503 — upstream down',
    )
  })

  it('handles a non-ApiError without crashing', () => {
    expect(decisionErrorText(new Error('network'))).toBe('network')
  })
})

describe('decisionOutcomeText', () => {
  it('says a replayed decision re-applied nothing', () => {
    expect(
      decisionOutcomeText({
        status: 'accepted',
        replayed: true,
        applied: null,
        decision_reason: null,
      }),
    ).toContain('nothing was re-applied')
  })

  it('names the op a fresh accept applied', () => {
    expect(
      decisionOutcomeText({
        status: 'accepted',
        replayed: false,
        applied: { op: 'supersede_fact' },
        decision_reason: null,
      }),
    ).toContain('supersede_fact')
  })

  it('echoes the reason on a reject', () => {
    expect(
      decisionOutcomeText({
        status: 'rejected',
        replayed: false,
        applied: null,
        decision_reason: 'not supported by the cited signal',
      }),
    ).toContain('not supported by the cited signal')
  })
})

describe('selfRevisionEvidenceSummary (§7.5a counterweight)', () => {
  it('says plainly that there is no record when the evidence is unavailable', () => {
    expect(selfRevisionEvidenceSummary(null)).toContain('has not earned a track record')
    expect(
      selfRevisionEvidenceSummary({
        available: false,
        forecast_unproven: true,
        calibration_thin: true,
        brier_skill_score: null,
        journal_critic_mean: null,
        journal_critic_n: 0,
      }),
    ).toContain('has not earned a track record')
  })

  it('leads with UNPROVEN forecast skill and flags thin calibration', () => {
    const s = selfRevisionEvidenceSummary({
      available: true,
      forecast_unproven: true,
      calibration_thin: true,
      brier_skill_score: null,
      journal_critic_mean: 0.71,
      journal_critic_n: 12,
    })
    expect(s).toContain('forecast skill UNPROVEN')
    expect(s).toContain('THIN')
    expect(s).toContain('n=12')
  })

  it('reports positive skill with the score when it has been earned', () => {
    const s = selfRevisionEvidenceSummary({
      available: true,
      forecast_unproven: false,
      calibration_thin: false,
      brier_skill_score: 0.12,
      journal_critic_mean: null,
      journal_critic_n: 0,
    })
    expect(s).toContain('positive')
    expect(s).toContain('0.120')
    expect(s).toContain('no critic scores')
  })
})

describe('isActionable / proposalKindLabel', () => {
  const base = { status: 'pending' } as JournalProposal
  it('treats only pending as actionable', () => {
    expect(isActionable(base)).toBe(true)
    for (const status of ['accepted', 'rejected', 'archived']) {
      expect(isActionable({ ...base, status } as JournalProposal)).toBe(false)
    }
  })

  it('humanizes self_revision and passes unknown kinds through', () => {
    expect(proposalKindLabel('self_revision')).toBe('self-revision')
    expect(proposalKindLabel('teleport')).toBe('teleport')
  })
})
