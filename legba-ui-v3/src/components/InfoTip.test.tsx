/**
 * Component tests for InfoTip — the shared verdict-literacy explainer (U-5).
 * Locks the accessibility contract: the trigger is focusable, links to the
 * popover via `aria-describedby`, and the popover text renders (CSS-only
 * hover/focus reveal isn't itself testable in jsdom, but the DOM wiring that
 * makes it keyboard/touch-safe is).
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { InfoTip } from './InfoTip'

describe('InfoTip', () => {
  it('renders the trigger content and the explainer text', () => {
    render(
      <InfoTip text="Plain-language explanation." testId="my-tip">
        <span>L likely</span>
      </InfoTip>,
    )
    expect(screen.getByText('L likely')).toBeInTheDocument()
    expect(screen.getByTestId('my-tip-popover')).toHaveTextContent('Plain-language explanation.')
  })

  it('is keyboard-focusable and describes itself via aria-describedby', () => {
    render(
      <InfoTip text="Explains the chip." testId="kb-tip">
        <span>chip</span>
      </InfoTip>,
    )
    const trigger = screen.getByTestId('kb-tip')
    expect(trigger).toHaveAttribute('tabindex', '0')
    const describedBy = trigger.getAttribute('aria-describedby')
    expect(describedBy).toBeTruthy()
    expect(screen.getByTestId('kb-tip-popover')).toHaveAttribute('id', describedBy)
  })

  it('falls back to the generic popover testid when none is given', () => {
    render(
      <InfoTip text="Generic.">
        <span>x</span>
      </InfoTip>,
    )
    expect(screen.getByTestId('info-tip-popover')).toHaveTextContent('Generic.')
  })

  it('without interactiveParent, a click on the trigger reaches an ambient parent onClick', () => {
    const onParentClick = vi.fn()
    render(
      <button onClick={onParentClick}>
        <InfoTip text="Explains." testId="plain-tip">
          <span>chip</span>
        </InfoTip>
      </button>,
    )
    fireEvent.click(screen.getByTestId('plain-tip'))
    expect(onParentClick).toHaveBeenCalledTimes(1)
  })

  it('interactiveParent stops a trigger click from reaching an ambient parent onClick', () => {
    const onParentClick = vi.fn()
    render(
      <button onClick={onParentClick}>
        <InfoTip text="Explains." testId="guarded-tip" interactiveParent>
          <span>chip</span>
        </InfoTip>
      </button>,
    )
    fireEvent.click(screen.getByTestId('guarded-tip'))
    expect(onParentClick).not.toHaveBeenCalled()
  })

  it('interactiveParent stops Enter/Space on the trigger from reaching an ambient parent onKeyDown', () => {
    const onParentKeyDown = vi.fn()
    render(
      <div onKeyDown={onParentKeyDown}>
        <InfoTip text="Explains." testId="guarded-key-tip" interactiveParent>
          <span>chip</span>
        </InfoTip>
      </div>,
    )
    fireEvent.keyDown(screen.getByTestId('guarded-key-tip'), { key: 'Enter' })
    expect(onParentKeyDown).not.toHaveBeenCalled()
  })
})
