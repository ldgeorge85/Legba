import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ConsultTurnStamp } from '@/components/ConsultTurnStamp'
import { parseVerification, parseUsageReceipt } from '@/lib/consultTurnStamp'
import { turnsFromServer } from '@/state/consultSession'
import type { ConsultSessionDetail } from '@/lib/api'

const RECEIPT = {
  schema: 'consult_usage/1',
  plane: 'core',
  route: 'llm.primary.openai_compat',
  model: 'gpt-oss-120b',
  calls: 4,
  input_tokens: 41230,
  output_tokens: 1880,
  elapsed_s: 37.4,
  cost_usd: null,
  cost_reason: 'route_unpriced',
  reason: null,
}

describe('ConsultTurnStamp — the turn header cell', () => {
  it('a judged turn shows its score, the weak-judge mark, and its usage', () => {
    render(
      <ConsultTurnStamp
        usage={RECEIPT}
        verification={{
          state: 'judged',
          score: 0.8333,
          weak_judge: true,
          supported_claims: 5,
          checkable_claims: 6,
          judge_model: 'llama-3.1-8b',
          judge_llm_ref: 'llm.verify.slm_8b',
          judged_at: '2026-10-08T21:00:00Z',
        }}
      />,
    )
    const v = screen.getByTestId('consult-turn-verification')
    expect(v).toHaveTextContent('judged: 0.83 (weak judge)')
    expect(v).toHaveAttribute('data-state', 'judged')
    expect(v.getAttribute('title')).toContain('5 of 6 cited claims supported')
    expect(v.getAttribute('title')).toContain('2026-10-08T21:00:00Z')
    const u = screen.getByTestId('consult-turn-usage')
    expect(u).toHaveTextContent('gpt-oss-120b · 41,230 in / 1,880 out tok · 37 s · cost: not priced')
    expect(u).not.toHaveTextContent('$0.00')
  })

  it('a not-judged turn says so, with why — never blank', () => {
    render(
      <ConsultTurnStamp
        usage={{ ...RECEIPT, plane: 'opus', model: 'claude-opus', cost_usd: 0.4212, cost_reason: null }}
        verification={{ state: 'not_judged', score: null, reason: 'judge_timeout' }}
      />,
    )
    const v = screen.getByTestId('consult-turn-verification')
    expect(v).toHaveTextContent('not judged · judge timed out')
    expect(v).toHaveAttribute('data-state', 'not_judged')
    expect(screen.getByTestId('consult-turn-usage')).toHaveTextContent('$0.42')
  })

  it('a judge that could not run names why — never a bare "judge unavailable"', () => {
    const { unmount } = render(
      <ConsultTurnStamp
        usage={RECEIPT}
        verification={{
          state: 'not_judged',
          score: null,
          reason: 'judge_unavailable',
          judge_unavailable_reason: 'route_down',
        }}
      />,
    )
    const v = screen.getByTestId('consult-turn-verification')
    expect(v).toHaveTextContent('not judged · judge unavailable (route down)')
    expect(v.getAttribute('title')).toContain('cause: route down')
    unmount()
    render(
      <ConsultTurnStamp
        usage={RECEIPT}
        verification={{
          state: 'not_judged',
          score: null,
          reason: 'judged_text_changed',
          judge_unavailable_reason: 'not_settled',
        }}
      />,
    )
    expect(screen.getByTestId('consult-turn-verification')).toHaveTextContent(
      'not judged · answer changed after judging (answer not settled)',
    )
  })

  it('a reason that already says why is not repeated, but the cause is in the title', () => {
    render(
      <ConsultTurnStamp
        usage={RECEIPT}
        verification={{
          state: 'not_judged',
          score: null,
          reason: 'judge_input_over_budget',
          judge_unavailable_reason: 'budget_exhausted',
        }}
      />,
    )
    const v = screen.getByTestId('consult-turn-verification')
    expect(v.textContent).toBe('not judged · too much to judge')
    expect(v.getAttribute('title')).toContain('cause: budget exhausted')
  })

  it('an unassessable turn never shows 0.5 — or any number', () => {
    render(
      <ConsultTurnStamp
        usage={RECEIPT}
        verification={{ state: 'unassessable', score: 0.5, reason: 'no_cited_claims' }}
      />,
    )
    const v = screen.getByTestId('consult-turn-verification')
    expect(v).toHaveTextContent('unassessable · no cited claims')
    expect(v.textContent).not.toMatch(/\d/)
    expect(v).toHaveAttribute('data-state', 'unassessable')
  })

  it('a turn recorded before judging, and one with no stamp, still carry a label', () => {
    const { unmount } = render(<ConsultTurnStamp usage={null} verification={null} />)
    expect(screen.getByTestId('consult-turn-verification')).toHaveTextContent(
      'not judged · recorded before judging',
    )
    expect(screen.getByTestId('consult-turn-usage')).toHaveTextContent('usage: not recorded')
    unmount()
    render(<ConsultTurnStamp usage={undefined} verification={undefined} />)
    expect(screen.getByTestId('consult-turn-verification')).toHaveTextContent(/^not judged$/)
  })

  it('a usage field the run did not report reads not measured, never 0', () => {
    render(
      <ConsultTurnStamp
        usage={{ ...RECEIPT, input_tokens: null, output_tokens: null, elapsed_s: null, cost_reason: 'pricing_not_reported' }}
        verification={{ state: 'not_judged', reason: 'judge_disabled' }}
      />,
    )
    const u = screen.getByTestId('consult-turn-usage')
    expect(u).toHaveTextContent('tokens: not measured')
    expect(u).toHaveTextContent('time: not measured')
    expect(u).toHaveTextContent('cost: not measured')
    expect(u.textContent).not.toMatch(/\b0 in\b/)
  })

  it('a price-table estimate reads as an estimate', () => {
    render(
      <ConsultTurnStamp
        usage={{ ...RECEIPT, cost_usd: 4.2, cost_reason: 'table_estimate' }}
        verification={{ state: 'not_judged', reason: 'judge_disabled' }}
      />,
    )
    const u = screen.getByTestId('consult-turn-usage')
    expect(u).toHaveTextContent('~$4.20')
  })
})

describe('the stamps parse and lift from a reloaded session', () => {
  it('parses: {} is no receipt, null is pre-judging, junk is absent', () => {
    expect(parseUsageReceipt({})).toBeNull()
    expect(parseUsageReceipt(undefined)).toBeNull()
    expect(parseVerification(null)).toBeNull()
    expect(parseVerification({ state: 'bogus', score: 0.9 })).toBeUndefined()
    expect(parseVerification({ state: 'judged', score: 0.9 })?.score).toBe(0.9)
  })

  it('turnsFromServer lifts usage + verification onto assistant turns only', () => {
    const detail: ConsultSessionDetail = {
      id: 's',
      mode: 'chat',
      title: 't',
      turns: [
        { id: 'u', role: 'user', content: 'q', steps: [], tool_calls: [], cited_refs: [], usage: {}, verification: null },
        {
          id: 'a', role: 'assistant', content: 'a', steps: [], tool_calls: [], cited_refs: [],
          usage: RECEIPT, verification: { state: 'judged', score: 0.7 },
        },
        { id: 'b', role: 'assistant', content: 'old', steps: [], tool_calls: [], cited_refs: [], usage: {}, verification: null },
      ],
    }
    const [user, judged, old] = turnsFromServer(detail)
    expect(user.verification).toBeUndefined()
    expect(judged.usageReceipt?.model).toBe('gpt-oss-120b')
    expect(judged.verification?.state).toBe('judged')
    expect(old.usageReceipt).toBeNull()
    expect(old.verification).toBeNull()
  })
})
