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
 *  3. **The panel-open bridges.** A panel asking for another panel fires a DOM
 *     event, and an event with no listener is lost — so the listener that
 *     MATERIALISES the tile has to live where the dock api does. Covered at the
 *     foot of this file.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import { App, CROSS_FRAMING_KIND, OPEN_CROSS_FRAMING_EVENT } from '@/App'
import { PANEL_REGISTRY, type RegistryEntry } from '@/panel-registry/registry'
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
 * The PANEL-OPEN BRIDGES — the third thing that lives in `App.tsx` rather than
 * in any panel.
 *
 * A panel that wants another panel opened on a subject parks the subject on
 * `window` and fires a DOM event. The event alone cannot work: a `CustomEvent`
 * with no listener is simply lost, which is exactly the "first click does
 * nothing" bug the optimizer-diff bridge was added to fix. The listener has to
 * sit in the shell, because the shell is the only thing holding the dock api,
 * and only the shell can materialise a tile that is not mounted yet.
 */
describe('the cross-framing bridge (wave P lane B)', () => {
  it('materialises the tile on the event, so the FIRST click opens it', async () => {
    // Lane B registers `analysis.cross_framing`; this shell lane is pinned
    // before that merge, so the kind may not be in the registry here. Stand a
    // minimal entry in when it is missing — what is under test is the SHELL's
    // half (event → addPanel), which is identical either way, and the entry is
    // removed again so no other test sees it.
    const registry = PANEL_REGISTRY as Record<string, RegistryEntry>
    const injected = !(CROSS_FRAMING_KIND in registry)
    if (injected) {
      registry[CROSS_FRAMING_KIND] = {
        definition: {
          kind: CROSS_FRAMING_KIND as never,
          panelId: 'analysis_cross_framing',
          category: 'system',
          scopeKey: null,
          defaultTitle: 'Cross-framing',
          requiresBinding: false,
          modes: ['personal'],
          hidden: true,
          tier: 'live',
        },
        Component: () => <div data-testid="cross-framing-stub" />,
      }
    }
    try {
      render(wrap(<App />))
      await waitFor(() => expect(screen.getByTestId('workspace-bar')).toBeInTheDocument())
      expect(screen.queryByText('Cross-framing')).toBeNull()

      fireEvent(window, new CustomEvent(OPEN_CROSS_FRAMING_EVENT))

      // The tab carries the kind's title; the panel itself drains the subject
      // lane B parked on `window` when it mounts.
      await waitFor(() => expect(screen.getAllByText('Cross-framing').length).toBeGreaterThan(0))
    } finally {
      if (injected) delete registry[CROSS_FRAMING_KIND]
    }
  })
})
