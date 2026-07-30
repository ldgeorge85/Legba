/**
 * Component tests for VerdictBadge — locks the C2b `structural —
 * grounding-verified` chip branch (the server stamps `verify_exempt:
 * "structural-verified"` for a structural finding whose asserted quantities
 * passed the deterministic `structural_claims` verify profile; the badge
 * previously only ever rendered the honest-but-unverified `unverified —
 * structural` default for ANY structural row, verified or not).
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { VerdictBadge } from './VerdictBadge'

describe('VerdictBadge structural confidence variants', () => {
  it('renders the plain unverified — structural chip for an ungraded structural row', () => {
    render(<VerdictBadge input={{ confidence: 0.7, verifyExempt: 'structural' }} />)
    expect(screen.getByTestId('verdict-confidence')).toHaveTextContent('unverified — structural')
  })

  it('renders the distinct structural — recomputation-verified chip when the structural_claims profile passed', () => {
    render(<VerdictBadge input={{ confidence: 0.7, verifyExempt: 'structural-verified' }} />)
    const chip = screen.getByTestId('verdict-confidence')
    expect(chip).toHaveTextContent('structural — recomputation-verified')
    expect(chip).not.toHaveTextContent('unverified — structural')
  })

  it('a non-structural unverified row stays the bare "unverified" (no structural suffix)', () => {
    render(<VerdictBadge input={{ confidence: 0.7, analystId: 'country_assessor' }} />)
    const chip = screen.getByTestId('verdict-confidence')
    expect(chip).toHaveTextContent('unverified')
    expect(chip).not.toHaveTextContent('structural')
  })

  it('a real faithfulness verify block still wins the confidence axis', () => {
    render(
      <VerdictBadge
        input={{
          confidence: 0.9,
          analystId: 'country_assessor',
          verification: { faithfulness_score: 0.9, judge_status: 'llm' },
          citationCount: 2,
        }}
      />,
    )
    expect(screen.getByTestId('verdict-confidence')).toHaveTextContent('high')
  })
})

describe('VerdictBadge interactiveParent passthrough', () => {
  it('without interactiveParent, a chip click reaches an ambient row onClick', () => {
    const onRowClick = vi.fn()
    render(
      <button onClick={onRowClick}>
        <VerdictBadge input={{ confidence: 0.7, analystId: 'country_assessor' }} />
      </button>,
    )
    fireEvent.click(screen.getByTestId('verdict-confidence'))
    expect(onRowClick).toHaveBeenCalledTimes(1)
  })

  it('interactiveParent stops both chips from bubbling into an ambient row onClick', () => {
    const onRowClick = vi.fn()
    render(
      <button onClick={onRowClick}>
        <VerdictBadge input={{ confidence: 0.7, analystId: 'country_assessor' }} interactiveParent />
      </button>,
    )
    fireEvent.click(screen.getByTestId('verdict-likelihood'))
    fireEvent.click(screen.getByTestId('verdict-confidence'))
    expect(onRowClick).not.toHaveBeenCalled()
  })
})
