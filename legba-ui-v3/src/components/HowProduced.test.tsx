/**
 * The reader's trust words and the "How this was produced" readout: at most
 * two plain words up front; every figure behind a collapsed disclosure,
 * labelled in plain words with what it is taken over; the engine's own badges
 * only for an admin.
 */
import { describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { IDENTITY_QUERY_KEY, type IdentityState } from '@/auth/session'
import { engineWordsIn } from '@/lib/readerVocabulary'
import { findingTrust } from '@/lib/trustWords'
import { producerRows, referenceRow } from '@/components/HowProduced'
import CitedAssessment from '@/components/inspector/CitedAssessment'
import type { Citation } from '@/lib/citationsModel'

vi.mock('@/lib/unitEvalModel', async () => {
  const actual = await vi.importActual<typeof import('@/lib/unitEvalModel')>('@/lib/unitEvalModel')
  return {
    ...actual,
    fetchEvalScores: async () => ({
      scored_at: '2026-11-24T00:00:00Z',
      units: [
        {
          unit: 'escalation',
          faithfulness: 0.98,
          correctness_vs_reference: null,
          n_labeled: 0,
          n_findings: 34,
          status: 'verified',
          correctness_operator: 1,
          n_operator_scored: 1,
          operator_sufficient: false,
          badge: 'verified | faithfulness 0.98 | unmeasured (0 labels) | operator 1.00 (n=1, indicative)',
        },
      ],
    }),
  }
})

vi.mock('@/lib/unitCorrectnessModel', async () => {
  const actual = await vi.importActual<typeof import('@/lib/unitCorrectnessModel')>('@/lib/unitCorrectnessModel')
  const row = {
    analyst_id: 'escalation',
    n_claims: 11,
    n_decided: 3,
    n_contains: 0,
    n_contradicts: 1,
    badge: 'correctness 0.0% (0/3) · coverage 27.3% (n=11)',
    reference: { state: 'current' },
  }
  return {
    ...actual,
    fetchUnitCorrectness: async () => ({ target_id: 't', reference: { state: 'current' }, data: [row], next_cursor: null }),
  }
})

function withRole(role: string | null, ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  if (role) {
    const state: IdentityState = {
      state: 'signed_in',
      identity: { kind: 'user', actor: 'user:x', role: role as never, user: null, session_rejected: false },
    }
    qc.setQueryData(IDENTITY_QUERY_KEY, state)
  }
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

const CITES = [
  { marker: '[1]', refId: 's1', refKind: 'signal', sourceId: 'source.a' },
  { marker: '[2]', refId: 's2', refKind: 'signal', sourceId: 'source.b' },
  { marker: '[3]', refId: 's3', refKind: 'signal', sourceId: 'source.b' },
] as unknown as Citation[]

describe('the trust words', () => {
  it('at most two plain words, each with its n', () => {
    expect(findingTrust({ verification: { faithfulness_score: 0.98 }, citations: CITES }).map((w) => w.text)).toEqual([
      'Checked',
      '2 sources',
    ])
    expect(
      findingTrust({ verification: { unsupported_spans: [{ text: 'a' }, { text: 'b' }] }, citations: [CITES[0]] }).map((w) => w.text),
    ).toEqual(['2 claims flagged', 'Single source'])
  })

  it('a read that may not carry the check says nothing about it, rather than "Not checked"', () => {
    expect(findingTrust({ citations: CITES }, false, { checkMayBeElsewhere: true }).map((w) => w.text)).toEqual(['2 sources'])
    expect(findingTrust({ citations: CITES }).map((w) => w.text)).toEqual(['Not checked', '2 sources'])
  })
})

describe('How this was produced', () => {
  it("names what each figure is over, so the per-claim check and the outside reference never read as one contradictory pair", () => {
    const rows = [
      ...producerRows(
        { unit: 'escalation', faithfulness: 0.98, correctness_vs_reference: null, n_labeled: 0, n_findings: 34, status: null, n_operator_scored: 1, correctness_operator: 1, operator_sufficient: false, badge: '' },
        'escalation',
      ),
      referenceRow({ n_claims: 11, n_decided: 3, n_contains: 0, n_contradicts: 1 } as never, null)!,
    ]
    const text = rows.map((r) => `${r.label}: ${r.value}`).join('\n')
    expect(text).toContain('Earlier reads on the same question: Escalation: 98% supported by their sources, over 34 reads')
    expect(text).toContain('Graded by a person: 100% judged right, over 1 read (too few to call a rate)')
    expect(text).toContain('Checked against an outside reference: 0 of 3 claims it could decide confirmed; 1 contradicted; 8 of 11 not covered by it')
    for (const r of rows) expect(engineWordsIn(`${r.label} ${r.value}`), r.label).toEqual([])
  })

  it('a reader sees the trust words and a collapsed readout in plain words, and no engine badge', async () => {
    render(
      withRole(
        'reader',
        <CitedAssessment
          text="A claim [1] and another [2]."
          citations={CITES}
          verification={{ faithfulness_score: 0.98, judge_status: 'llm' }}
          confidence={0.84}
          analystId="escalation"
          targetId="country_x"
        />,
      ),
    )
    expect(screen.getByTestId('cited-trust').textContent).toBe('Checked2 sources')
    const how = screen.getByTestId('how-produced')
    expect(how.tagName).toBe('DETAILS')
    expect((how as HTMLDetailsElement).open).toBe(false)
    expect(await within(how).findByTestId('produced-reference')).toBeInTheDocument()
    expect(within(how).getByTestId('produced-likelihood').textContent).toContain('very likely (84%)')
    expect(within(how).getByTestId('produced-held').textContent).toContain('98% of what it says is supported by its 3 citations')
    expect(screen.queryByTestId('how-produced-engine')).toBeNull()
    expect(screen.queryByTestId('verdict-badge')).toBeNull()
    expect(engineWordsIn(how.textContent ?? '')).toEqual([])
  })

  it('an admin also gets the engine readout under the plain rows', async () => {
    render(
      withRole(
        'admin',
        <CitedAssessment text="A claim [1]." citations={CITES} verification={{ faithfulness_score: 0.98 }} analystId="escalation" targetId="country_x" />,
      ),
    )
    expect(screen.getByTestId('how-produced-engine')).toBeInTheDocument()
    expect(await screen.findByTestId('unit-eval-badge')).toBeInTheDocument()
  })
})
