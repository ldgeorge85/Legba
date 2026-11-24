/**
 * C-1b/ui — the case executor against the REAL shell.
 *
 * The runner's unit tests prove each action calls the right port method; only a
 * mounted `App` proves the port is the shell's own openers: a preset `arrange`
 * really replaces the dock AND brings the presenting Consult tile back, a
 * bound `open_panel` really mints the per-subject tile, and Restore really puts
 * the wall back on the stance and arrangement the case started from.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import { App } from '@/App'
import { PreferencesProvider } from '@/components/density/PreferencesProvider'
import { __resetReadTelemetry } from '@/lib/readTelemetry'
import { applyAction, getCaseShell, takeSnapshot, restoreSnapshot } from '@/lib/caseRunner'
import { parseCaseAction, type CaseAction } from '@/lib/caseModel'
import { resetScope } from '@/state/scope'
import { useSelection } from '@/state/selection'
import { resetFrameState } from '@/state/frame'

vi.mock('@/panels/v4/MapPanel', () => ({
  default: () => <div data-testid="stub-world-map" />,
}))

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <PreferencesProvider>{ui}</PreferencesProvider>
    </QueryClientProvider>
  )
}

function action(name: string, args: Record<string, unknown>): CaseAction {
  return parseCaseAction({
    seq: 1,
    action: name,
    args,
    at: '2026-10-03T12:00:00Z',
    cites: [{ kind: 'target', id: 't1' }],
    rests_on: [{ round: 1, tool: 'list_targets' }],
  })!
}

/** What is on screen: the panel side's ids (via the shell's own serializer) and the stage's view (U-6). */
function dockPanels(): string[] {
  const layout = getCaseShell()!.serializeLayout() as { panels?: Record<string, unknown> } | null
  const stage = document.querySelector('[data-testid="stage"]')?.getAttribute('data-stage-view')
  return [...Object.keys(layout?.panels ?? {}), ...(stage ? [stage] : [])].sort()
}

function wsParam(): string | null {
  return new URLSearchParams(window.location.hash.replace(/^#/, '')).get('ws')
}

beforeEach(() => {
  resetFrameState()
  __resetReadTelemetry()
  sessionStorage.clear()
  localStorage.clear()
  window.location.hash = ''
  resetScope()
  useSelection.getState().clear()
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (url: string) => {
      const body = String(url).includes('/registry/') ? [] : { data: [], items: [], next_cursor: null }
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

async function mountShell() {
  render(wrap(<App />))
  await waitFor(() => expect(getCaseShell()).not.toBeNull())
  await waitFor(() => expect(dockPanels().length).toBeGreaterThan(0))
}

describe('the case shell port, mounted', () => {
  it('arrange(preset) lands a retired preset on its page; Consult stays in the frame', async () => {
    await mountShell()
    act(() => {
      expect(applyAction(action('arrange', { layout: 'workspace', layout_type: 'preset' }))).toEqual({
        state: 'executed',
      })
    })
    // U-2: the retired "Feed & Provenance" preset (id `workspace`) opens the Investigate section.
    await waitFor(() => expect(wsParam()).toBe('investigate'))
    await waitFor(() => expect(dockPanels()).toContain('system.findings'))
    expect(dockPanels()).not.toContain('system.consult')
    expect(screen.getByTestId('consult-dock')).toBeInTheDocument()
  })

  it('open_panel on a per-subject kind mints the bound tile — a retired id mints the subject page', async () => {
    await mountShell()
    act(() => {
      applyAction(
        action('open_panel', { kind: 'target.overview', params: { subject: { kind: 'target', id: 't1' } } }),
      )
    })
    // UI-B3: `target.overview` retired onto `subject.page#runtime`.
    await waitFor(() => expect(dockPanels()).toContain('subject.page:t1'))
    expect(dockPanels()).not.toContain('target.overview:t1')
  })

  it('Restore returns the stance and the arrangement the case started from', async () => {
    await mountShell()
    const before = dockPanels()
    expect(wsParam()).toBe(null)
    const snap = takeSnapshot()
    expect(snap.stance).toBe('board')
    act(() => {
      applyAction(action('arrange', { layout: 'investigate', layout_type: 'stance' }))
    })
    await waitFor(() => expect(wsParam()).toBe('investigate'))
    expect(dockPanels()).not.toEqual(before)
    act(() => restoreSnapshot(snap))
    await waitFor(() => expect(wsParam()).toBe('board'))
    expect(dockPanels()).toEqual(before)
  })
})
