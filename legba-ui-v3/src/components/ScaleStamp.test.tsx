import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ScaleStamp } from '@/components/ScaleStamp'
import { UNSTAMPED_LABEL } from '@/lib/scaleStamp'

describe('ScaleStamp', () => {
  it('renders both halves when the row carries both — the full state', () => {
    render(
      <ScaleStamp
        row={{
          scale_version: 'intensity/2026-08',
          method_version: 'situation_clustering/2026-09.1',
        }}
      />,
    )
    const chip = screen.getByTestId('scale-stamp')
    expect(chip).toHaveTextContent('intensity/2026-08 · situation_clustering/2026-09.1')
    expect(chip).toHaveAttribute('data-stamp-state', 'full')
  })

  it('renders the revision alone when the row has no scale — the method-only state', () => {
    render(<ScaleStamp row={{ method_version: 'scorecard_banding/2026-09.1' }} />)
    const chip = screen.getByTestId('scale-stamp')
    expect(chip).toHaveTextContent('scorecard_banding/2026-09.1')
    expect(chip).toHaveAttribute('data-stamp-state', 'method-only')
    // No fabricated scale half, and no separator implying one is missing.
    expect(chip.textContent).not.toContain('·')
  })

  it('renders an unstamped row as the dated absence — never a dash, zero or guess', () => {
    render(<ScaleStamp row={{ intensity_score: 59 }} />)
    const chip = screen.getByTestId('scale-stamp')
    expect(chip).toHaveTextContent(UNSTAMPED_LABEL)
    expect(chip).toHaveAttribute('data-stamp-state', 'unstamped')
    expect(chip.textContent).not.toBe('—')
    expect(chip.textContent).not.toContain('2026-09.1')
  })

  it('teaches what a scale change costs a comparison, in the tooltip', () => {
    render(
      <ScaleStamp
        row={{ scale_version: 'intensity/2026-08', method_version: 'x/2026-09.1' }}
      />,
    )
    const title = screen.getByTestId('scale-stamp').getAttribute('title') ?? ''
    expect(title).toContain('comparable within')
    expect(title).toContain('across a scale change they cannot')
  })

  it('says, on an unstamped row, that the current scale is not assumed', () => {
    render(<ScaleStamp row={{}} />)
    const title = screen.getByTestId('scale-stamp').getAttribute('title') ?? ''
    expect(title).toContain('not assumed')
  })

  it('takes a pre-read stamp, for a call site whose halves are separate fields', () => {
    render(
      <ScaleStamp
        stamp={{ scale: null, method: 'desk_baseline/2026-09.1', state: 'method-only' }}
      />,
    )
    expect(screen.getByTestId('scale-stamp')).toHaveTextContent('desk_baseline/2026-09.1')
  })

  it('can be hidden on an unstamped row, but only when asked explicitly', () => {
    const { container } = render(<ScaleStamp row={{}} showUnstamped={false} />)
    expect(container.querySelector('[data-testid="scale-stamp"]')).toBeNull()
  })

  it('carries the call site’s own testid so a list of chips stays distinguishable', () => {
    render(<ScaleStamp row={{}} testId="situation-scale-stamp-abc" />)
    expect(screen.getByTestId('situation-scale-stamp-abc')).toBeInTheDocument()
  })
})
