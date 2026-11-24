/**
 * Dialog primitive — open/close, the focus trap, the keyboard, the portal host.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { Dialog, DialogContent, DialogTitle, DialogTrigger, DialogClose } from './dialog'
import { PortalHost } from './portal'

afterEach(cleanup)

function Harness({ onOpenChange }: { onOpenChange?: (o: boolean) => void }) {
  const [open, setOpen] = useState(false)
  return (
    <PortalHost>
      <button type="button" data-testid="outside">outside</button>
      <Dialog
        open={open}
        onOpenChange={(o) => {
          setOpen(o)
          onOpenChange?.(o)
        }}
      >
        <DialogTrigger data-testid="opener">open</DialogTrigger>
        <DialogContent data-testid="dlg" overlayTestId="dlg-overlay" aria-describedby={undefined}>
          <DialogTitle>Title</DialogTitle>
          <input data-testid="first" />
          <button type="button" data-testid="middle">middle</button>
          <DialogClose data-testid="last">close</DialogClose>
        </DialogContent>
      </Dialog>
    </PortalHost>
  )
}

describe('Dialog primitive', () => {
  it('is closed until the trigger opens it, and is a labelled modal dialog', async () => {
    render(<Harness />)
    expect(screen.queryByTestId('dlg')).toBeNull()
    fireEvent.click(screen.getByTestId('opener'))
    const dlg = await screen.findByTestId('dlg')
    expect(dlg).toHaveAttribute('role', 'dialog')
    expect(dlg).toHaveAccessibleName('Title')
  })

  it('moves focus inside on open and returns it to the opener on close', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await user.click(screen.getByTestId('opener'))
    await waitFor(() => expect(screen.getByTestId('first')).toHaveFocus())
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByTestId('dlg')).toBeNull())
    expect(screen.getByTestId('opener')).toHaveFocus()
  })

  it('TRAPS focus: Tab from the last control wraps to the first, Shift-Tab back', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await user.click(screen.getByTestId('opener'))
    await waitFor(() => expect(screen.getByTestId('first')).toHaveFocus())
    await user.tab()
    expect(screen.getByTestId('middle')).toHaveFocus()
    await user.tab()
    expect(screen.getByTestId('last')).toHaveFocus()
    await user.tab()
    expect(screen.getByTestId('first')).toHaveFocus()
    await user.tab({ shift: true })
    expect(screen.getByTestId('last')).toHaveFocus()
    // never reaches the control outside the dialog
    expect(screen.getByTestId('outside')).not.toHaveFocus()
  })

  it('Escape closes and reports the change', async () => {
    const onOpenChange = vi.fn()
    render(<Harness onOpenChange={onOpenChange} />)
    fireEvent.click(screen.getByTestId('opener'))
    await screen.findByTestId('dlg')
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('dlg')).toBeNull())
    expect(onOpenChange).toHaveBeenLastCalledWith(false)
  })

  it('a press on the overlay closes', async () => {
    render(<Harness />)
    fireEvent.click(screen.getByTestId('opener'))
    await screen.findByTestId('dlg')
    fireEvent.pointerDown(screen.getByTestId('dlg-overlay'))
    await waitFor(() => expect(screen.queryByTestId('dlg')).toBeNull())
  })

  it('mounts into the explicit portal host, not straight onto <body>', async () => {
    render(<Harness />)
    fireEvent.click(screen.getByTestId('opener'))
    const dlg = await screen.findByTestId('dlg')
    const host = screen.getByTestId('ui-portal-host')
    expect(host.contains(dlg)).toBe(true)
    expect(host.contains(screen.getByTestId('dlg-overlay'))).toBe(true)
  })

  it('falls back to <body> outside a host (a lone panel under test)', async () => {
    render(
      <Dialog open>
        <DialogContent data-testid="lone" aria-describedby={undefined}>
          <DialogTitle>t</DialogTitle>
        </DialogContent>
      </Dialog>,
    )
    const lone = await screen.findByTestId('lone')
    expect(document.body.contains(lone)).toBe(true)
  })
})
