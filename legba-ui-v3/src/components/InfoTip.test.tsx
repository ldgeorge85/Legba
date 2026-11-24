/**
 * Component tests for InfoTip — the shared verdict-literacy explainer (U-5).
 * Locks the accessibility contract: the trigger is focusable, links to the
 * description via `aria-describedby`, and the description text renders. Since
 * UI-B1/tokens the visible box is the Tooltip primitive, so its reveal on
 * focus / tap IS testable in jsdom — the cases near the end.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { InfoTip } from './InfoTip'
import { PortalHost } from '@/ui/portal'

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

  // UI-B1/tokens — the floating box is the Tooltip primitive now. The
  // description above stays in the DOM for screen readers; the VISIBLE box
  // opens on focus, on a click or tap, and portals out of the tile.
  it('keyboard focus opens the floating explainer; Escape closes it', async () => {
    render(
      <PortalHost>
        <InfoTip text="Explains the chip." testId="float-tip">
          <span>chip</span>
        </InfoTip>
      </PortalHost>,
    )
    expect(screen.queryByTestId('float-tip-popover-floating')).toBeNull()
    act(() => screen.getByTestId('float-tip').focus())
    const floating = await screen.findByTestId('float-tip-popover-floating')
    expect(floating).toHaveTextContent('Explains the chip.')
    expect(screen.getByTestId('ui-portal-host').contains(floating)).toBe(true)
    // the trigger keeps naming the always-present description
    expect(screen.getByTestId('float-tip')).toHaveAttribute(
      'aria-describedby',
      screen.getByTestId('float-tip-popover').id,
    )
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('float-tip-popover-floating')).toBeNull())
  })

  it('a tap opens the floating explainer (the touch contract)', async () => {
    render(
      <InfoTip text="Tap to read." testId="tap-tip">
        <span>chip</span>
      </InfoTip>,
    )
    const trigger = screen.getByTestId('tap-tip')
    fireEvent.pointerDown(trigger, { pointerType: 'touch' })
    fireEvent.pointerUp(trigger, { pointerType: 'touch' })
    fireEvent.click(trigger)
    expect(await screen.findByTestId('tap-tip-popover-floating')).toHaveTextContent('Tap to read.')
  })

  it('interactiveParent: a tap opens the explainer WITHOUT firing the parent', async () => {
    const onParentClick = vi.fn()
    render(
      <button onClick={onParentClick}>
        <InfoTip text="Guarded." testId="guarded-tap" interactiveParent>
          <span>chip</span>
        </InfoTip>
      </button>,
    )
    fireEvent.click(screen.getByTestId('guarded-tap'))
    expect(await screen.findByTestId('guarded-tap-popover-floating')).toHaveTextContent('Guarded.')
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
