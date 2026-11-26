/**
 * THE PANEL CONTRACT, END TO END (U-6; brief §12: "every panel declares what
 * Consult can do to it, so Consult can see what is on screen and act on it
 * through the one command executor").
 *
 * A DUMMY VIEW registered here — one call, no edit to the frame — is opened,
 * bound, driven by an action it declares, reported in the on-screen context,
 * and undone. The reader views' declared actions reach their views (the map
 * through the stores it follows, with no map code touched), what a view does
 * not accept is refused by name, and a Consult turn asked in the frame carries
 * the on-screen report.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useState, type ReactElement } from 'react'

import { App } from '@/App'
import { PreferencesProvider } from '@/components/density/PreferencesProvider'
import { __resetReadTelemetry } from '@/lib/readTelemetry'
import { getShell } from '@/lib/applyAction'
import { CONSULT_PANEL_ID } from '@/lib/caseReplay'
import { applyPanelAction, clearViewUndo, undoView, usePanelActionHandler } from '@/lib/panelActions'
import { describeOnScreen, MAX_BYTES } from '@/lib/onScreen'
import { registerPanel, type PanelContract } from '@/panel-registry/contracts'
import { catalogEntries } from '@/components/CatalogPopover'
import { resetCasePlayer } from '@/state/casePlayer'
import { useConsultSessions } from '@/state/consultSession'
import { resetScope } from '@/state/scope'
import { useSelection } from '@/state/selection'
import { resetFrameState } from '@/state/frame'
import { useWorldState } from '@/v4/world/worldState'
import { useGlobeLayers, resetGlobeLayers } from '@/v4/globe/layerState'
import { dockIds, stageView } from '@/test/frameProbe'

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

const DUMMY: PanelContract = {
  id: 'test.dummy',
  name: 'Dummy board',
  description: 'A view registered by a test: bound to a subject, it answers a ping.',
  group: 'Other',
  side: 'reader',
  binds: ['subject'],
  size: 'column',
  home: 'panel',
  actions: [
    {
      id: 'ping',
      label: 'Ping',
      args: [{ name: 'note', type: 'string', required: true, describe: 'what to show' }],
    },
  ],
}

function Dummy({ binding, instanceId }: { binding: Record<string, string> | null; instanceId: string }) {
  const [note, setNote] = useState('(no ping yet)')
  usePanelActionHandler(instanceId, 'ping', (args) => {
    setNote(String(args.note))
  })
  return (
    <div data-testid="dummy-view">
      <span data-testid="dummy-binding">{binding?.target_id ?? 'unbound'}</span>
      <span data-testid="dummy-note">{note}</span>
    </div>
  )
}

let unregister: (() => void) | null = null
let posts: { url: string; body: Record<string, unknown> }[] = []

beforeEach(() => {
  __resetReadTelemetry()
  localStorage.clear()
  sessionStorage.clear()
  window.location.hash = ''
  resetScope()
  resetCasePlayer()
  resetFrameState()
  clearViewUndo()
  resetGlobeLayers()
  useSelection.getState().clear()
  posts = []
  unregister = registerPanel(DUMMY, Dummy)
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
      const u = String(url)
      if (init?.method === 'POST') {
        try {
          posts.push({ url: u, body: JSON.parse(String(init.body)) })
        } catch {
          posts.push({ url: u, body: {} })
        }
      }
      const body = u.includes('/registry/') ? [] : { data: [], items: [], next_cursor: null }
      return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) } as unknown as Response
    }),
  )
})

afterEach(() => {
  cleanup()
  unregister?.()
  resetCasePlayer()
  useConsultSessions.getState().reset(CONSULT_PANEL_ID)
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

async function boot() {
  render(wrap(<App />))
  await waitFor(() => expect(getShell()).not.toBeNull())
  await waitFor(() => expect(dockIds()).toContain('v4.map'))
}

describe('a view registered in one call', () => {
  it('is listed in the catalog with its description, with no edit to the frame', () => {
    const e = catalogEntries('personal', 'reader').find((x) => x.kind === 'test.dummy')
    expect(e).toMatchObject({ title: 'Dummy board', description: DUMMY.description, isKind: false })
  })

  it('is opened, bound, driven by its own action, reported on screen — and undone', async () => {
    await boot()
    // BOUND: the contract's `bind` lands it in its home (the panel side).
    let out = applyPanelAction('test.dummy', 'bind', { subject: { kind: 'target', id: 't1' } })
    expect(out).toEqual({ state: 'executed' })
    await waitFor(() => expect(dockIds()).toContain('test.dummy:t1'))
    await waitFor(() => expect(screen.getByTestId('dummy-binding')).toHaveTextContent('t1'))
    // DRIVEN: its declared action, with typed arguments, through the router.
    act(() => {
      out = applyPanelAction('test.dummy:t1', 'ping', { note: 'hello from Consult' })
    })
    expect(out).toEqual({ state: 'executed' })
    expect(screen.getByTestId('dummy-note')).toHaveTextContent('hello from Consult')
    // A bad argument and an undeclared action are refused by name.
    expect(applyPanelAction('test.dummy:t1', 'ping', {})).toEqual({ state: 'refused', reason: 'invalid_arg: missing note' })
    expect(applyPanelAction('test.dummy:t1', 'layer', { layer: 'events', on: true })).toEqual({
      state: 'refused',
      reason: 'action_not_accepted: layer on test.dummy',
    })
    // REPORTED: the on-screen context names it, where it sits, small.
    const on = describeOnScreen()!
    expect(on.page).toBe('board')
    expect(on.views.find((v) => v.id === 'test.dummy:t1')).toMatchObject({ at: expect.stringMatching(/^p\d$/) })
    expect(on.views[0]).toEqual({ id: 'view:board_changed', at: 'stage', front: true })
    expect(JSON.stringify(on).length).toBeLessThanOrEqual(MAX_BYTES)
    // ON THE STAGE: the same view, sent to the stage through its contract.
    act(() => {
      applyPanelAction('test.dummy:t1', 'open', { place: 'stage' })
    })
    await waitFor(() => expect(stageView()).toBe('test.dummy:t1'))
    // UNDO: two steps back, the stage has the read and the dummy is in its panel.
    act(() => {
      undoView()
    })
    await waitFor(() => expect(stageView()).toBe('view:board_changed'))
    act(() => {
      undoView()
      undoView()
    })
    await waitFor(() => expect(dockIds()).not.toContain('test.dummy:t1'))
  }, 30000)
})

describe("the reader views' declared actions reach them", () => {
  it('the map: a layer on/off and a fly-to, through the stores it follows', async () => {
    await boot()
    expect(useGlobeLayers.getState().on.events).toBe(false)
    expect(applyPanelAction('v4.map', 'set_layer', { layer: 'events', on: true })).toEqual({ state: 'executed' })
    expect(useGlobeLayers.getState().on.events).toBe(true)
    // the globe's row is bound to the world layers a Consult `map` step drives
    expect(useWorldState.getState().layers.events).toBe(true)
    // an id the map does not have is refused by name
    expect(applyPanelAction('v4.map', 'set_layer', { layer: 'entities', on: true })).toMatchObject({ state: 'refused' })
    expect(applyPanelAction('v4.map', 'fly_to', { ref: { kind: 'finding', id: 'f-3' } })).toEqual({ state: 'executed' })
    expect(useSelection.getState().selection).toMatchObject({ kind: 'finding', id: 'f-3' })
    expect(applyPanelAction('v4.map', 'set_window', { span: '7d' })).toEqual({ state: 'executed' })
    expect(useWorldState.getState().spanMs).toBe(7 * 24 * 3_600_000)
    // and the whole lot undoes, newest first
    act(() => {
      undoView()
      undoView()
      undoView()
    })
    expect(useGlobeLayers.getState().on.events).toBe(false)
    expect(useWorldState.getState().spanMs).toBe(24 * 3_600_000)
  })

  it('the subject page: bound on the stage, then its tab set', async () => {
    await boot()
    act(() => {
      applyPanelAction('subject.page', 'bind', { subject: { kind: 'target', id: 'country_g20_br' } })
    })
    await waitFor(() => expect(stageView()).toBe('subject.page:country_g20_br'))
    act(() => {
      applyPanelAction('subject.page:country_g20_br', 'set_tab', { tab: 'claims' })
    })
    await waitFor(() => expect(screen.getByTestId('subject-tab-claims')).toHaveAttribute('aria-selected', 'true'), {
      timeout: 10000,
    })
  }, 30000)

  it('a view in the panels closes and re-opens through its contract', async () => {
    await boot()
    expect(applyPanelAction('v4.map', 'close')).toEqual({ state: 'executed' })
    await waitFor(() => expect(dockIds()).not.toContain('v4.map'))
    expect(applyPanelAction('v4.map', 'open')).toEqual({ state: 'executed' })
    await waitFor(() => expect(dockIds()).toContain('v4.map'))
  })
})

describe('Consult is given the on-screen context', () => {
  it('a turn asked in the frame carries `on_screen` (ids and places only)', async () => {
    await boot()
    const box = (await screen.findByTestId('consult-question', {}, { timeout: 15000 })) as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'what moved?' } })
    fireEvent.click(screen.getByTestId('consult-submit'))
    await waitFor(() => expect(posts.some((p) => p.url.includes('/consult'))).toBe(true))
    const turn = posts.find((p) => p.url.includes('/consult'))!.body
    expect(turn.on_screen).toMatchObject({ v: 1, page: 'board', side: 'reader', chat: { placement: 'bottom', collapsed: false } })
    const views = (turn.on_screen as { views: { id: string }[] }).views.map((v) => v.id)
    expect(views).toEqual(expect.arrayContaining(['view:board_changed', 'v4.map']))
    // the map is on the page: its rows that are on ride along (U-7)
    expect((turn.on_screen as { map?: { layers: string[] } }).map?.layers).toEqual(expect.arrayContaining(['change', 'blanks']))
    expect(JSON.stringify(turn.on_screen).length).toBeLessThanOrEqual(MAX_BYTES)
  }, 30000)
})
