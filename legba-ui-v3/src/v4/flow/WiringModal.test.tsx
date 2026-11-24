/**
 * The Flow wiring modal on the Dialog primitive (UI-B1/tokens). The behaviour
 * the hand-rolled overlay had is kept — Escape closes, the backdrop closes,
 * the ✕ and Cancel close — and the primitive adds the focus trap.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import WiringModal from './WiringModal'
import { PortalHost } from '@/ui/portal'

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <PortalHost>{ui}</PortalHost>
    </QueryClientProvider>
  )
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: true, status: 200, json: async () => [] })),
  )
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('WiringModal — the Dialog primitive', () => {
  it('renders nothing while closed', () => {
    render(wrap(<WiringModal open={false} onClose={vi.fn()} />))
    expect(screen.queryByTestId('wiring-modal')).toBeNull()
  })

  it('opens as a modal dialog named by its title, inside the portal host', async () => {
    render(wrap(<WiringModal open onClose={vi.fn()} />))
    const dlg = await screen.findByTestId('wiring-modal')
    expect(dlg).toHaveAttribute('role', 'dialog')
    expect(dlg).toHaveAccessibleName('Wire it up')
    expect(screen.getByTestId('ui-portal-host').contains(dlg)).toBe(true)
  })

  it('Escape closes', async () => {
    const onClose = vi.fn()
    render(wrap(<WiringModal open onClose={onClose} />))
    await screen.findByTestId('wiring-modal')
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
  })

  it('the ✕ and Cancel close', async () => {
    const onClose = vi.fn()
    render(wrap(<WiringModal open onClose={onClose} />))
    fireEvent.click(await screen.findByTestId('wiring-modal-close'))
    fireEvent.click(screen.getByTestId('wiring-modal-cancel'))
    expect(onClose).toHaveBeenCalledTimes(2)
  })

  it('Tab stays inside the modal', async () => {
    const user = userEvent.setup()
    render(wrap(<WiringModal open onClose={vi.fn()} />))
    const dlg = await screen.findByTestId('wiring-modal')
    await waitFor(() => expect(dlg.contains(document.activeElement)).toBe(true))
    for (let i = 0; i < 10; i++) {
      await user.tab()
      expect(dlg.contains(document.activeElement)).toBe(true)
    }
  })

  it('holds no raw palette class (every colour is a token)', async () => {
    render(wrap(<WiringModal open onClose={vi.fn()} />))
    const dlg = await screen.findByTestId('wiring-modal')
    expect(dlg.outerHTML).not.toMatch(/\b(slate|gray|zinc|neutral|stone)-(50|[1-9]00|950)\b/)
  })
})
