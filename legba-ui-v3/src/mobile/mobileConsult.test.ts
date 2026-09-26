/**
 * mobileConsult — the pin must actually reach the model.
 *
 * The operator's complaint was that the Consult lost the thing you were
 * looking at. These tests hold the two guarantees that fix it: the pinned
 * report is IN the payload the server will read, and it can never push the
 * request past the server's own length ceiling.
 */
import { describe, it, expect } from 'vitest'

import {
  buildConsultBody,
  buildPinnedContext,
  buildStructuredPins,
  composeQuestion,
  CONTEXT_BUDGET,
  QUESTION_MAX,
  type PinnedReport,
} from './mobileConsult'

const REPORT: PinnedReport = {
  id: '7c8170f9-3464-4f65-b5b8-979dd9d82b6d',
  title: 'World read · 3 Sep — Saudi Arabia remains under high energy-security pressure',
  tier: 'World',
  target: 'World',
  bluf: 'Saudi Arabia remains under high energy-security pressure as recent tanker attacks in the Strait of Hormuz continue to disrupt its oil export chain.',
  producedAt: '2026-09-03T12:00:00Z',
}

describe('the pinned context', () => {
  it('names the report by id, tier, target and lead', () => {
    const ctx = buildPinnedContext(REPORT, null)
    expect(ctx).toContain(REPORT.id)
    expect(ctx).toContain('tier: World')
    expect(ctx).toContain('Saudi Arabia remains under high energy')
    expect(ctx.startsWith('[Pinned report]')).toBe(true)
  })

  it('adds the focused entity when one is selected', () => {
    const ctx = buildPinnedContext(REPORT, { id: 'country_g20_sa', name: 'Saudi Arabia' })
    expect(ctx).toContain('focused on: Saudi Arabia (target id=country_g20_sa)')
  })

  it('is empty with no report — the sheet still works unpinned', () => {
    expect(buildPinnedContext(null, null)).toBe('')
  })

  it('keeps the identity lines and truncates the LEAD when over budget', () => {
    const long = { ...REPORT, bluf: 'x'.repeat(10_000) }
    const ctx = buildPinnedContext(long, null)
    expect(ctx.length).toBeLessThanOrEqual(CONTEXT_BUDGET)
    // The id survives; it is the part that makes the answer about this report.
    expect(ctx).toContain(long.id)
    expect(ctx).toContain('…')
  })
})

describe('composing the question', () => {
  it('prefixes the context above the operator text', () => {
    const out = composeQuestion('[Pinned report]\nid: abc', 'What changed?')
    expect(out).toBe('[Pinned report]\nid: abc\n\nWhat changed?')
  })

  it('never exceeds the server ceiling', () => {
    const out = composeQuestion('c'.repeat(5000), 'q'.repeat(5000))
    expect(out.length).toBeLessThanOrEqual(QUESTION_MAX)
  })

  it('shrinks the CONTEXT, never the operator words', () => {
    const question = 'q'.repeat(7000)
    const out = composeQuestion('c'.repeat(3000), question)
    expect(out.length).toBeLessThanOrEqual(QUESTION_MAX)
    expect(out.endsWith(question)).toBe(true)
  })

  it('passes an unpinned question straight through', () => {
    expect(composeQuestion('', '  What changed?  ')).toBe('What changed?')
  })
})

describe('the request body', () => {
  const body = buildConsultBody({
    question: 'What is driving this?',
    report: REPORT,
    entity: { id: 'country_g20_sa', name: 'Saudi Arabia' },
    transcript: [{ role: 'user', content: 'earlier' }],
    sessionId: 'sess-1',
    model: 'opus',
    maxRounds: 10,
    requestId: 'req-1',
  })

  it('carries the pin in the question text — the channel the server reads today', () => {
    expect(String(body.question)).toContain(REPORT.id)
    expect(String(body.question)).toContain('Saudi Arabia')
    expect(String(body.question)).toContain('What is driving this?')
  })

  it('sends the exact field set the registry declares', () => {
    expect(Object.keys(body).sort()).toEqual(
      [
        'max_tool_rounds',
        'messages',
        'mode',
        'model',
        'pinned_context',
        'question',
        'request_id',
        'scope_predicate',
        'session_id',
      ].sort(),
    )
    expect(body.mode).toBe('chat')
    expect(body.session_id).toBe('sess-1')
    expect(body.request_id).toBe('req-1')
    expect(body.messages).toEqual([{ role: 'user', content: 'earlier' }])
  })

  it('leaves scope_predicate null rather than guessing a predicate dialect', () => {
    expect(body.scope_predicate).toBeNull()
  })

  it('also sends the structured pin in the shape the server is growing', () => {
    const pins = buildStructuredPins(REPORT, { id: 'country_g20_sa', name: 'Saudi Arabia' })
    expect(pins.map((p) => p.kind)).toEqual(['report', 'target'])
    expect(pins[0].id).toBe(REPORT.id)
    expect(pins[0].title).toBe(REPORT.title)
    // Both channels must carry the same substance.
    expect(pins[0].text).toContain(REPORT.id)
    expect(pins[1].text).toContain('Saudi Arabia')
    expect(body.pinned_context).toEqual(pins)
  })
})
