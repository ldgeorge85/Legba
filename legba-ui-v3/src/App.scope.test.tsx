/**
 * The shell's half of the SCOPE ≠ FOCUS contract (WORKSTATION_V2_FLOW_DESIGN §3).
 *
 * Two things live in `App.tsx` rather than in any panel, and both are asserted
 * against a really-mounted shell because that is the only place they exist:
 *
 *  1. **The desk bridge.** A desk click is a scoping act wherever it is made —
 *     the sidebar, a choropleth, a registry row, ⌘K. Wiring it per-panel would
 *     make the scope depend on which panels happened to be mounted; one
 *     subscription in the shell cannot drift.
 *  2. **The trail keys.** `Alt+←/→` walk FOCUS, `Alt+Shift+←/→` walk SCOPE. The
 *     selection store shipped `back()` only, capped at 12, with a single
 *     consumer inside the Inspector; v2 had back AND forward over 50, and a
 *     drill trail that nothing can navigate is a breadcrumb, not history.
 *  3. **A panel opening another panel.** The DOM-event bridges are retired
 *     (UI-B1/shell): a panel drives an `open_panel` through the ONE executor,
 *     whose shell port lives where the dock api does, so the FIRST click
 *     materialises the tile. Covered at the foot of this file.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import { App } from '@/App'

const CROSS_FRAMING_KIND = 'analysis.cross_framing' as const
import { requestCrossFraming } from '@/lib/crossFramingLink'
import { pendingPanelParams, resetPanelParams } from '@/state/panelParams'
import { PANEL_REGISTRY } from '@/panel-registry/registry'
import { PreferencesProvider } from '@/components/density/PreferencesProvider'
import { __resetReadTelemetry } from '@/lib/readTelemetry'
import { resetScope, useScope } from '@/state/scope'
import { selectRow, useSelection } from '@/state/selection'

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <PreferencesProvider>{ui}</PreferencesProvider>
    </QueryClientProvider>
  )
}

const alt = (key: string, shiftKey = false) =>
  fireEvent.keyDown(window, { key, altKey: true, shiftKey })

beforeEach(() => {
  __resetReadTelemetry()
  sessionStorage.clear()
  localStorage.clear()
  window.location.hash = ''
  resetScope()
  useSelection.getState().clear()
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (url: string) => {
      const body = String(url).includes('/registry/')
        ? []
        : { data: [], items: [], next_cursor: null }
      return {
        ok: true,
        status: 200,
        json: async () => body,
        text: async () => JSON.stringify(body),
      } as unknown as Response
    }),
  )
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('the desk bridge — installed by the shell, not by a panel', () => {
  it('mirrors a target selection into the scope', async () => {
    render(wrap(<App />))
    await waitFor(() => expect(useSelection.getState()).toBeTruthy())
    selectRow('target', 'country_g20_br', 'Brazil', { origin: 'desks' })
    await waitFor(() => expect(useScope.getState().scope?.id).toBe('country_g20_br'))
    expect(useScope.getState().scope?.kind).toBe('target')
  })

  it('leaves the scope alone for every other kind — reading never re-scopes', async () => {
    render(wrap(<App />))
    selectRow('target', 'country_g20_br', 'Brazil')
    await waitFor(() => expect(useScope.getState().scope?.id).toBe('country_g20_br'))
    selectRow('finding', 'f1', 'a finding')
    selectRow('signal', 's1', 'a signal')
    expect(useScope.getState().scope?.id).toBe('country_g20_br')
    expect(useSelection.getState().selection?.id).toBe('s1')
  })
})

describe('Alt+← / Alt+→ walk the FOCUS trail', () => {
  it('steps back and forward through the drill', async () => {
    render(wrap(<App />))
    selectRow('finding', 'a', 'A')
    selectRow('finding', 'b', 'B')
    selectRow('finding', 'c', 'C')

    alt('ArrowLeft')
    expect(useSelection.getState().selection?.id).toBe('b')
    alt('ArrowLeft')
    expect(useSelection.getState().selection?.id).toBe('a')
    alt('ArrowRight')
    expect(useSelection.getState().selection?.id).toBe('b')
    alt('ArrowRight')
    expect(useSelection.getState().selection?.id).toBe('c')
  })

  it('is inert at the ends rather than clearing the selection', async () => {
    render(wrap(<App />))
    selectRow('finding', 'only', 'Only')
    alt('ArrowLeft')
    alt('ArrowLeft')
    alt('ArrowRight')
    expect(useSelection.getState().selection?.id).toBe('only')
  })
})

describe('Alt+Shift+← / → walk the SCOPE trail', () => {
  it('steps back and forward through what the wall was about', async () => {
    render(wrap(<App />))
    selectRow('target', 'a', 'A')
    await waitFor(() => expect(useScope.getState().scope?.id).toBe('a'))
    selectRow('target', 'b', 'B')
    await waitFor(() => expect(useScope.getState().scope?.id).toBe('b'))

    alt('ArrowLeft', true)
    expect(useScope.getState().scope?.id).toBe('a')
    alt('ArrowRight', true)
    expect(useScope.getState().scope?.id).toBe('b')
  })

  it('does not disturb the focus trail', async () => {
    render(wrap(<App />))
    selectRow('target', 'a', 'A')
    selectRow('finding', 'f1', 'F1')
    const focusBefore = useSelection.getState().selection?.id
    alt('ArrowLeft', true)
    expect(useSelection.getState().selection?.id).toBe(focusBefore)
  })
})

/**
 * The cross-framing drill, opened by a claim chip (wave P lane B; UI-B1/shell).
 *
 * The `legba:open-cross-framing` event and its `window` park are retired. A
 * claim chip drives an `open_panel` carrying the claim through the ONE
 * executor; the shell port materialises the tile on the FIRST click, and the
 * claim waits for the tile in `state/panelParams`.
 */
describe('the cross-framing drill opens through the one executor', () => {
  it('materialises the tile on the FIRST request, with the claim in hand', async () => {
    resetPanelParams()
    render(wrap(<App />))
    await waitFor(() => expect(screen.getByTestId('surface-bar')).toBeInTheDocument())
    expect(screen.queryByText('Cross-framing')).toBeNull()

    requestCrossFraming({ targetId: 'country_ruritania', claimId: 'c9', origin: 'test' })

    await waitFor(() => expect(screen.getAllByText('Cross-framing').length).toBeGreaterThan(0))
    expect(PANEL_REGISTRY[CROSS_FRAMING_KIND]).toBeDefined()
    // The request carried the claim; the mounted tile consumed it.
    await waitFor(() => expect(pendingPanelParams(CROSS_FRAMING_KIND)).toBeNull(), { timeout: 5000 })
  })
})
