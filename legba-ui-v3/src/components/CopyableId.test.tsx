/**
 * Component tests for CopyableId — truncated raw-id + copy affordance (U-5).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { CopyableId } from './CopyableId'

const LONG_ID = 'a1b2c3d4-e5f6-7890-abcd-ef1234567890'

describe('CopyableId', () => {
  beforeEach(() => {
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } })
  })

  it('renders a truncated id, not the full id, as visible text', () => {
    render(<CopyableId id={LONG_ID} />)
    expect(screen.getByTestId('copyable-id')).toHaveTextContent('a1b2c3d4…7890')
    expect(screen.getByTestId('copyable-id')).not.toHaveTextContent(LONG_ID)
  })

  it('carries the full id in title for hover reference', () => {
    render(<CopyableId id={LONG_ID} />)
    expect(screen.getByTestId('copyable-id').getAttribute('title')).toContain(LONG_ID)
  })

  it('copies the FULL id to the clipboard on click, and shows copied feedback', async () => {
    render(<CopyableId id={LONG_ID} />)
    fireEvent.click(screen.getByTestId('copyable-id'))
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(LONG_ID)
    await waitFor(() => expect(screen.getByTestId('copyable-id')).toHaveAttribute('data-copied', 'true'))
  })

  it('is a real focusable button (keyboard-accessible)', () => {
    render(<CopyableId id={LONG_ID} />)
    expect(screen.getByTestId('copyable-id').tagName).toBe('BUTTON')
  })
})
