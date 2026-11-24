/**
 * C2 — the unit eval badge names its scope. Its faithfulness figure is the
 * UNIT's, across its reads; printed bare beside a record it read as the
 * record's own score and contradicted the record's verdict.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'

vi.mock('@/lib/unitEvalModel', async () => {
  const actual = await vi.importActual<typeof import('@/lib/unitEvalModel')>('@/lib/unitEvalModel')
  return {
    ...actual,
    fetchEvalScores: async () => ({
      scored_at: '2026-01-01T00:00:00Z',
      units: [
        {
          unit: 'unit_a',
          faithfulness: 0.5,
          correctness_vs_reference: null,
          n_labeled: 0,
          n_findings: 12,
          status: null,
          badge: 'verified | faithfulness 0.50 | unmeasured (0 labels)',
        },
      ],
    }),
  }
})

import { UnitEvalBadge } from './UnitEvalBadge'

afterEach(() => vi.restoreAllMocks())

describe('UnitEvalBadge', () => {
  it('names the figure as the unit’s, over its reads — never a bare faithfulness beside a record', async () => {
    render(<UnitEvalBadge analystId="unit_a" />)
    const badge = await screen.findByTestId('unit-eval-badge')
    expect(screen.getByTestId('unit-eval-scope')).toHaveTextContent('unit, over 12 reads:')
    expect(badge.textContent).toMatch(/^unit, over 12 reads:.*faithfulness 0\.50/)
  })
})
