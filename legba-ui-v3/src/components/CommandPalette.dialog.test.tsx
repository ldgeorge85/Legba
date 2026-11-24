/**
 * The command palette on the Dialog primitive (UI-B1/tokens): the modal half of
 * its key handling now belongs to the dialog — the search box takes focus on
 * open, Escape and the backdrop close, Tab is trapped inside — while the list
 * keys (↑/↓/Enter, ⌘/Ctrl-Enter) stay the palette's. Same test ids as before.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useState, type ReactElement } from 'react'
import { CommandPalette, type CommandPaletteProps } from './CommandPalette'
import { PortalHost } from '@/ui/portal'

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <PortalHost>{ui}</PortalHost>
    </QueryClientProvider>
  )
}

type Handlers = Omit<CommandPaletteProps, 'open' | 'mode' | 'onClose'>

// Every family compiles to drive actions (UI-B1/shell); the test records them.
function handlers(): Handlers & { onDrive: ReturnType<typeof vi.fn> } {
  return { onDrive: vi.fn() }
}

function Harness({ h, onClose }: { h: Handlers; onClose?: () => void }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" data-testid="opener" onClick={() => setOpen(true)}>
        ⌘K
      </button>
      <CommandPalette
        open={open}
        mode="personal"
        onClose={() => {
          setOpen(false)
          onClose?.()
        }}
        {...h}
      />
    </>
  )
}

beforeEach(() => {
  localStorage.clear()
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: true, status: 200, json: async () => [] })),
  )
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('CommandPalette — the Dialog primitive', () => {
  it('renders nothing while closed', () => {
    render(wrap(<Harness h={handlers()} />))
    expect(screen.queryByTestId('command-palette')).toBeNull()
  })

  it('opens as a labelled modal dialog with focus in the search box', async () => {
    const user = userEvent.setup()
    render(wrap(<Harness h={handlers()} />))
    await user.click(screen.getByTestId('opener'))
    const dlg = await screen.findByTestId('command-palette')
    expect(dlg).toHaveAttribute('role', 'dialog')
    expect(dlg).toHaveAccessibleName('Command palette')
    await waitFor(() => expect(screen.getByTestId('command-palette-input')).toHaveFocus())
    expect(screen.getByTestId('ui-portal-host').contains(dlg)).toBe(true)
  })

  it('Escape closes (the dialog owns it now) and focus returns to the opener', async () => {
    const user = userEvent.setup()
    const onClose = vi.fn()
    render(wrap(<Harness h={handlers()} onClose={onClose} />))
    await user.click(screen.getByTestId('opener'))
    await waitFor(() => expect(screen.getByTestId('command-palette-input')).toHaveFocus())
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByTestId('command-palette')).toBeNull())
    expect(onClose).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(screen.getByTestId('opener')).toHaveFocus())
  })

  it('a press on the backdrop closes', async () => {
    const onClose = vi.fn()
    render(wrap(<Harness h={handlers()} onClose={onClose} />))
    fireEvent.click(screen.getByTestId('opener'))
    await screen.findByTestId('command-palette')
    fireEvent.pointerDown(screen.getByTestId('command-palette-backdrop'))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('Tab is trapped inside the palette', async () => {
    const user = userEvent.setup()
    render(wrap(<Harness h={handlers()} />))
    await user.click(screen.getByTestId('opener'))
    const dlg = await screen.findByTestId('command-palette')
    await waitFor(() => expect(screen.getByTestId('command-palette-input')).toHaveFocus())
    for (let i = 0; i < 6; i++) {
      await user.tab()
      expect(dlg.contains(document.activeElement)).toBe(true)
    }
    expect(screen.getByTestId('opener')).not.toHaveFocus()
  })

  it('the list keys stay the palette\'s: # narrows, ↓ moves, Enter opens the active entry', async () => {
    const user = userEvent.setup()
    const h = handlers()
    render(wrap(<Harness h={h} />))
    await user.click(screen.getByTestId('opener'))
    const input = await screen.findByTestId('command-palette-input')
    await waitFor(() => expect(input).toHaveFocus())
    await user.keyboard('#')
    const items = screen
      .getByTestId('command-palette-list')
      .querySelectorAll('[data-entry-kind="surface"]')
    expect(items.length).toBeGreaterThanOrEqual(2)
    await user.keyboard('{ArrowDown}{Enter}')
    // `#` narrows to the surfaces: each compiles to `arrange` (U-2).
    expect(h.onDrive).toHaveBeenCalledTimes(1)
    expect(h.onDrive.mock.calls[0][0][0].action).toBe('arrange')
    await waitFor(() => expect(screen.queryByTestId('command-palette')).toBeNull())
  })
})
