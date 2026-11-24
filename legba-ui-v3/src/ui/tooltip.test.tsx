/**
 * Tooltip primitive — keyboard focus opens, Escape closes, a click or TAP opens
 * and stays open (the touch contract Radix leaves out), the portal host.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Tooltip, TooltipContent, TooltipTrigger } from './tooltip'
import { PortalHost } from './portal'

afterEach(cleanup)

function Harness() {
  return (
    <PortalHost>
      <button type="button" data-testid="before">before</button>
      <Tooltip>
        <TooltipTrigger asChild>
          <span tabIndex={0} data-testid="trigger">
            chip
          </span>
        </TooltipTrigger>
        <TooltipContent data-testid="tip">Plain-language explanation.</TooltipContent>
      </Tooltip>
    </PortalHost>
  )
}

describe('Tooltip primitive', () => {
  it('is closed until asked', () => {
    render(<Harness />)
    expect(screen.queryByTestId('tip')).toBeNull()
  })

  it('keyboard focus opens it; Escape closes it', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    screen.getByTestId('before').focus()
    await user.tab()
    expect(screen.getByTestId('trigger')).toHaveFocus()
    expect(await screen.findByTestId('tip')).toHaveTextContent('Plain-language explanation.')
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByTestId('tip')).toBeNull())
  })

  it('moving focus away closes it', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    screen.getByTestId('before').focus()
    await user.tab()
    await screen.findByTestId('tip')
    await user.tab({ shift: true })
    await waitFor(() => expect(screen.queryByTestId('tip')).toBeNull())
  })

  it('a click opens it and keeps it open (Radix alone would close on click)', async () => {
    render(<Harness />)
    fireEvent.click(screen.getByTestId('trigger'))
    expect(await screen.findByTestId('tip')).toBeInTheDocument()
    // a second click keeps it open rather than toggling it shut
    fireEvent.click(screen.getByTestId('trigger'))
    expect(screen.getByTestId('tip')).toBeInTheDocument()
  })

  it('a TAP opens it — the phone has no hover', async () => {
    render(<Harness />)
    const trigger = screen.getByTestId('trigger')
    fireEvent.pointerDown(trigger, { pointerType: 'touch' })
    fireEvent.pointerUp(trigger, { pointerType: 'touch' })
    fireEvent.click(trigger)
    expect(await screen.findByTestId('tip')).toBeInTheDocument()
  })

  it('mounts into the explicit portal host', async () => {
    render(<Harness />)
    fireEvent.click(screen.getByTestId('trigger'))
    const tip = await screen.findByTestId('tip')
    expect(screen.getByTestId('ui-portal-host').contains(tip)).toBe(true)
  })
})
