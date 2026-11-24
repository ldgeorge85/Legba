/**
 * Popover primitive — toggle, focus in and back, Escape / outside press, the portal host.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Popover, PopoverContent, PopoverTrigger } from './popover'
import { PortalHost } from './portal'

afterEach(cleanup)

function Harness() {
  return (
    <PortalHost>
      <button type="button" data-testid="elsewhere">elsewhere</button>
      <Popover>
        <PopoverTrigger data-testid="trigger">catalog</PopoverTrigger>
        <PopoverContent data-testid="content">
          <button type="button" data-testid="item-a">a</button>
          <button type="button" data-testid="item-b">b</button>
        </PopoverContent>
      </Popover>
    </PortalHost>
  )
}

describe('Popover primitive', () => {
  it('a click on the trigger toggles it open and closed', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    expect(screen.queryByTestId('content')).toBeNull()
    expect(screen.getByTestId('trigger')).toHaveAttribute('aria-expanded', 'false')
    await user.click(screen.getByTestId('trigger'))
    expect(await screen.findByTestId('content')).toBeInTheDocument()
    expect(screen.getByTestId('trigger')).toHaveAttribute('aria-expanded', 'true')
    await user.click(screen.getByTestId('trigger'))
    await waitFor(() => expect(screen.queryByTestId('content')).toBeNull())
  })

  it('moves focus into the content, and Escape returns it to the trigger', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await user.click(screen.getByTestId('trigger'))
    const content = await screen.findByTestId('content')
    await waitFor(() => expect(content.contains(document.activeElement)).toBe(true))
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByTestId('content')).toBeNull())
    expect(screen.getByTestId('trigger')).toHaveFocus()
  })

  it('opens from the keyboard (Enter on the focused trigger)', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    screen.getByTestId('trigger').focus()
    await user.keyboard('{Enter}')
    expect(await screen.findByTestId('content')).toBeInTheDocument()
  })

  it('a press outside closes it', async () => {
    render(<Harness />)
    fireEvent.click(screen.getByTestId('trigger'))
    await screen.findByTestId('content')
    fireEvent.pointerDown(screen.getByTestId('elsewhere'))
    await waitFor(() => expect(screen.queryByTestId('content')).toBeNull())
  })

  it('mounts into the explicit portal host', async () => {
    render(<Harness />)
    fireEvent.click(screen.getByTestId('trigger'))
    const content = await screen.findByTestId('content')
    expect(screen.getByTestId('ui-portal-host').contains(content)).toBe(true)
  })
})
