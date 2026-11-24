/**
 * Select primitive — open from the keyboard, pick by click and by keys,
 * Escape, the `data-value` stamp, the portal host.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from './select'
import { PortalHost } from './portal'

afterEach(cleanup)

function Harness() {
  const [v, setV] = useState('')
  return (
    <PortalHost>
      <Select value={v} onValueChange={setV}>
        <SelectTrigger data-testid="trigger" aria-label="fruit">
          <SelectValue placeholder="pick a fruit…" />
        </SelectTrigger>
        <SelectContent data-testid="content">
          <SelectItem value="apple" data-testid="opt-apple">Apple</SelectItem>
          <SelectItem value="banana" data-testid="opt-banana">Banana</SelectItem>
          <SelectItem value="cherry" data-testid="opt-cherry">Cherry</SelectItem>
        </SelectContent>
      </Select>
      <span data-testid="value">{v}</span>
    </PortalHost>
  )
}

describe('Select primitive', () => {
  it('shows the placeholder with no value and stamps an empty data-value', () => {
    render(<Harness />)
    const trigger = screen.getByTestId('trigger')
    expect(trigger).toHaveTextContent('pick a fruit…')
    expect(trigger).toHaveAttribute('data-value', '')
    expect(trigger).toHaveAttribute('role', 'combobox')
    expect(screen.queryByTestId('content')).toBeNull()
  })

  it('Enter opens the listbox; a click on an option picks it and closes', async () => {
    render(<Harness />)
    const trigger = screen.getByTestId('trigger')
    fireEvent.keyDown(trigger, { key: 'Enter' })
    expect(await screen.findByRole('listbox')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('opt-banana'))
    await waitFor(() => expect(screen.queryByRole('listbox')).toBeNull())
    expect(screen.getByTestId('value')).toHaveTextContent('banana')
    expect(trigger).toHaveAttribute('data-value', 'banana')
    expect(trigger).toHaveTextContent('Banana')
  })

  it('keyboard only: open, ArrowDown, Enter picks', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    screen.getByTestId('trigger').focus()
    await user.keyboard('{Enter}')
    await screen.findByRole('listbox')
    await waitFor(() => expect(screen.getByTestId('opt-apple')).toHaveFocus())
    await user.keyboard('{ArrowDown}')
    expect(screen.getByTestId('opt-banana')).toHaveFocus()
    await user.keyboard('{Enter}')
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('banana'))
  })

  it('Escape closes without picking and returns focus to the trigger', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    screen.getByTestId('trigger').focus()
    await user.keyboard('{Enter}')
    await screen.findByRole('listbox')
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('listbox')).toBeNull())
    expect(screen.getByTestId('value')).toHaveTextContent('')
    expect(screen.getByTestId('trigger')).toHaveFocus()
  })

  it('mounts its listbox into the explicit portal host', async () => {
    render(<Harness />)
    fireEvent.keyDown(screen.getByTestId('trigger'), { key: 'Enter' })
    const listbox = await screen.findByRole('listbox')
    expect(screen.getByTestId('ui-portal-host').contains(listbox)).toBe(true)
  })
})
