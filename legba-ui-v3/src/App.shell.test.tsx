/**
 * THE U-6 FRAME against the REAL shell (a mounted App over a real Dockview):
 *
 *  - the frame: the top bar with the four reader pages and ONE separate
 *    operator entry, the rail, the STAGE (today's read on the Board), Consult
 *    UNDER it (not a fixed column), the panels to the right, the time band;
 *  - a page seeds at most two panel regions; Alt+N switches; the operator side
 *    has its own sections and its rail;
 *  - a page-sized open lands on the STAGE with a crumb back; a panel-sized one
 *    lands in its HOME region and replaces the previous opened view there —
 *    never a new column;
 *  - Consult moves (left, right, full centre), collapses to the ask bar, and
 *    the choice is remembered per page; Undo takes a move back;
 *  - the Record sheet follows a reader's record selection; a record Consult
 *    pulls up lands on the stage instead;
 *  - A BROWSER HOLDING THE PREVIOUS SHELLS' STATE (the U-2 `s1-` surface slots,
 *    `legba_ws`, `legba_layout_custom`, saved missions, a light theme) boots on
 *    the new default with no error and no hidden view, and says so in a line;
 *  - retired ids still resolve; a C-1b fixture case replays; `#case=` on boot.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import { App } from '@/App'
import { PreferencesProvider } from '@/components/density/PreferencesProvider'
import { __resetReadTelemetry } from '@/lib/readTelemetry'
import { act as actionOf, applyAction, drive, getShell } from '@/lib/applyAction'
import { CONSULT_PANEL_ID } from '@/lib/caseReplay'
import { parseCaseRecord } from '@/lib/caseModel'
import { casePlayer, resetCasePlayer } from '@/state/casePlayer'
import { useConsultSessions } from '@/state/consultSession'
import { resetScope, useScope } from '@/state/scope'
import { useSelection } from '@/state/selection'
import { useWorldState } from '@/v4/world/worldState'
import { MAX_REGIONS, SURFACE_STORE_KEY, findSurface, surfaceViews } from '@/lib/surfaces'
import { FRAME_STORE_KEY, resetFrameState } from '@/state/frame'
import { clearViewUndo } from '@/lib/panelActions'
import { C1B_FIXTURE_CASE, C1B_FIXTURE_SESSION } from '@/test/caseFixture'
import { dockIds, dockLayout, dockRegions, stageView } from '@/test/frameProbe'

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

const surface = () => screen.getByTestId('frame').getAttribute('data-surface')
const dockEl = () => screen.getByTestId('consult-dock')

let fetchBodies: Record<string, unknown> = {}

beforeEach(() => {
  __resetReadTelemetry()
  sessionStorage.clear()
  localStorage.clear()
  window.location.hash = ''
  document.documentElement.className = ''
  resetScope()
  resetCasePlayer()
  resetFrameState()
  clearViewUndo()
  useSelection.getState().clear()
  fetchBodies = {}
  vi.stubGlobal(
    'fetch',
    vi.fn().mockImplementation(async (url: string) => {
      const u = String(url)
      const hit = Object.entries(fetchBodies).find(([needle]) => u.includes(needle))
      const body = hit ? hit[1] : u.includes('/registry/') ? [] : { data: [], items: [], next_cursor: null }
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
  resetCasePlayer()
  useConsultSessions.getState().reset(CONSULT_PANEL_ID)
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

async function boot(firstDock = 'v4.map') {
  render(wrap(<App />))
  await waitFor(() => expect(getShell()).not.toBeNull())
  await waitFor(() => expect(dockIds()).toContain(firstDock))
}

describe('the frame', () => {
  it("lands on the Board: today's read on the stage, Consult under it, the map leading the panels", async () => {
    await boot()
    expect(surface()).toBe('board')
    for (const id of ['board', 'subject', 'ledger', 'watch']) {
      expect(screen.getByTestId(`surface-tab-${id}`)).toBeInTheDocument()
    }
    for (const id of ['engine', 'evaluation', 'gate', 'investigate']) {
      expect(screen.queryByTestId(`surface-tab-${id}`)).toBeNull()
    }
    expect(screen.getByTestId('to-operator-side')).toBeInTheDocument()
    expect(stageView()).toBe('v4.morning_read')
    // Consult is in the centre, under the stage, open — not a fixed column.
    expect(dockEl()).toHaveAttribute('data-placement', 'bottom')
    expect(dockEl()).toHaveAttribute('data-collapsed', 'false')
    expect(screen.getByTestId('chat-slot')).toContainElement(dockEl())
    expect(screen.getByTestId('chat-divider')).toBeInTheDocument()
    expect(screen.getByTestId('time-band')).toBeInTheDocument()
    expect(screen.getByTestId('frame-rail')).toBeInTheDocument()
    expect(dockRegions()[0][0]).toBe('v4.map')
    // No arrangement menu anywhere: no presets, no chooser, no save/restore.
    expect(screen.queryByTestId('layout-preset-select')).toBeNull()
    expect(screen.queryByTestId('workspace-chooser-trigger')).toBeNull()
    expect(screen.queryByTestId('layout-save')).toBeNull()
  })

  it("the Board's panels are its seed (at most three regions), and neither Consult nor the Record is one", async () => {
    await boot()
    for (const v of surfaceViews(findSurface('board')!)) expect(dockIds()).toContain(v)
    expect(dockRegions().length).toBeLessThanOrEqual(MAX_REGIONS)
    expect(dockIds()).not.toContain('system.consult')
    expect(dockIds()).not.toContain('system.inspector')
    expect(dockIds()).not.toContain('v4.morning_read')
  })

  it('one dark theme: <html> carries .dark and never .theme-light', async () => {
    localStorage.setItem('legba_theme', 'light')
    await boot()
    expect(document.documentElement.classList.contains('dark')).toBe(true)
    expect(document.documentElement.classList.contains('theme-light')).toBe(false)
    expect(screen.queryByTestId('theme-toggle')).toBeNull()
  })
})

describe('pages switch by tab, by key and by link', () => {
  it('a tab and Alt+N go through the one executor; each page lays its own stage and panels', async () => {
    await boot()
    fireEvent.click(screen.getByTestId('surface-tab-ledger'))
    await waitFor(() => expect(surface()).toBe('ledger'))
    expect(stageView()).toBe('view:forecasts')
    fireEvent.keyDown(window, { key: '4', altKey: true })
    await waitFor(() => expect(surface()).toBe('watch'))
    expect(stageView()).toBe('view:watch_alerts')
    fireEvent.keyDown(window, { key: '5', altKey: true })
    await waitFor(() => expect(surface()).toBe('engine'))
    expect(stageView()).toBe('system.status')
    // The operator side's chat starts as the ask bar; its rail is the object rail.
    expect(dockEl()).toHaveAttribute('data-collapsed', 'true')
    fireEvent.click(screen.getByTestId('rail-toggle'))
    await waitFor(() => expect(screen.getByTestId('object-rail')).toBeInTheDocument())
    expect(screen.getByTestId('surface-tab-gate')).toBeInTheDocument()
    expect(screen.queryByTestId('surface-tab-board')).toBeNull()
    expect(screen.queryByTestId('time-band')).toBeNull()
    expect(dockRegions().length).toBeLessThanOrEqual(MAX_REGIONS)
    fireEvent.click(screen.getByTestId('to-reader-side'))
    await waitFor(() => expect(surface()).toBe('board'))
  })

  it('switching never destroys: a page comes back as it was left, under the new version stamp', async () => {
    await boot()
    fireEvent.click(screen.getByTestId('surface-tab-ledger'))
    await waitFor(() => expect(surface()).toBe('ledger'))
    fireEvent.click(screen.getByTestId('surface-tab-board'))
    await waitFor(() => expect(surface()).toBe('board'))
    const store = JSON.parse(localStorage.getItem(SURFACE_STORE_KEY)!)
    expect(store.ledger.personal.version).toMatch(/^s2-/)
    expect(screen.queryByTestId('surface-notice')).toBeNull()
  })

  it('a retired workspace link (#ws=desk) lands on the nearest page', async () => {
    window.location.hash = '#ws=desk'
    await boot()
    expect(surface()).toBe('subject')
    expect(stageView()).toBe('subject.page')
  })

  it('Reset lays the default back down and says so', async () => {
    await boot()
    act(() => {
      drive(actionOf('open_panel', { kind: 'system.source_health', params: {} }))
    })
    await waitFor(() => expect(dockIds()).toContain('system.source_health'))
    fireEvent.click(screen.getByTestId('surface-reset'))
    await waitFor(() => expect(dockIds()).not.toContain('system.source_health'))
    expect(screen.getByTestId('surface-notice')).toHaveTextContent(/back to its default/)
  })
})

describe('an open lands on the stage or in its home region, and never adds a column', () => {
  it('a seeded view is activated where it lives; an operator view crosses to its section', async () => {
    await boot()
    const before = dockRegions().length
    act(() => {
      drive(actionOf('open_panel', { kind: 'system.wall', params: {} }))
    })
    expect(surface()).toBe('board')
    expect(dockRegions().length).toBe(before)
    act(() => {
      drive(actionOf('open_panel', { kind: 'system.audit', params: {} }))
    })
    await waitFor(() => expect(surface()).toBe('engine'))
    expect(dockIds()).toContain('system.ledgers')
    expect(dockIds()).not.toContain('system.audit')
    await waitFor(() => expect(screen.getByTestId('ledgers-tab-audit')).toHaveAttribute('aria-selected', 'true'), {
      timeout: 10000,
    })
  }, 30000)

  it("a panel-sized view REPLACES its region's previous opened view", async () => {
    await boot()
    act(() => {
      drive(actionOf('open_panel', { kind: 'system.source_health', params: {} }))
    })
    await waitFor(() => expect(dockIds()).toContain('system.source_health'))
    const count = dockRegions().length
    act(() => {
      drive(actionOf('open_panel', { kind: 'system.eval_scorecard', params: {} }))
    })
    await waitFor(() => expect(dockIds()).toContain('system.eval_scorecard'))
    expect(surface()).toBe('board')
    expect(dockIds()).not.toContain('system.source_health')
    expect(dockRegions().length).toBe(count)
    const transient = Object.entries(dockLayout().panels ?? {}).filter(([, p]) => p.params?.transient)
    expect(transient.map(([id]) => id)).toEqual(['system.eval_scorecard'])
  })

  it("a subject's read lands on THIS page's stage, on its tab, with a crumb back to today's read", async () => {
    await boot()
    act(() => {
      drive(actionOf('open_panel', { kind: 'target.findings', params: { subject: { kind: 'target', id: 'country_g20_br' } } }))
    })
    await waitFor(() => expect(stageView()).toBe('subject.page:country_g20_br'))
    expect(surface()).toBe('board')
    await waitFor(() => expect(screen.getByTestId('subject-tab-evidence')).toHaveAttribute('aria-selected', 'true'), {
      timeout: 10000,
    })
    act(() => {
      drive(actionOf('open_panel', { kind: 'target.claims', params: { subject: { kind: 'target', id: 'country_g20_br' } } }))
    })
    await waitFor(() => expect(screen.getByTestId('subject-tab-claims')).toHaveAttribute('aria-selected', 'true'), {
      timeout: 10000,
    })
    // A second subject: the trail grows, never a column.
    const regions = dockRegions().length
    act(() => {
      drive(actionOf('open_panel', { kind: 'subject.page', params: { subject: { kind: 'target', id: 'lane_hormuz' } } }))
    })
    await waitFor(() => expect(stageView()).toBe('subject.page:lane_hormuz'))
    expect(dockRegions().length).toBe(regions)
    expect(screen.getByTestId('stage-crumb-0')).toHaveTextContent("Today's read")
    // Back goes back; the first crumb is always the page's own read.
    fireEvent.click(screen.getByTestId('stage-back'))
    await waitFor(() => expect(stageView()).toBe('subject.page:country_g20_br'))
    fireEvent.click(screen.getByTestId('stage-crumb-0'))
    await waitFor(() => expect(stageView()).toBe('v4.morning_read'))
  }, 30000)

  it('a view on the stage can go to the panels, and a panel can come to the stage', async () => {
    await boot()
    act(() => {
      drive(actionOf('open_panel', { kind: 'subject.page', params: { subject: { kind: 'target', id: 'lane_hormuz' } } }))
    })
    await waitFor(() => expect(stageView()).toBe('subject.page:lane_hormuz'))
    fireEvent.click(screen.getByTestId('stage-to-panels'))
    await waitFor(() => expect(dockIds()).toContain('subject.page:lane_hormuz'))
    expect(stageView()).toBe('v4.morning_read')
    act(() => {
      window.dispatchEvent(new CustomEvent('legba:frame-to-stage', { detail: 'system.wall' }))
    })
    await waitFor(() => expect(stageView()).toBe('system.wall'))
    expect(dockIds()).not.toContain('system.wall')
  })
})

describe('Consult in the layout: moved, resized, collapsed — remembered per page, undoable', () => {
  it('moves to a right column and to the left, pops to the full centre, and comes back under the stage', async () => {
    await boot()
    fireEvent.click(screen.getByTestId('consult-move-right'))
    await waitFor(() => expect(dockEl()).toHaveAttribute('data-placement', 'right'))
    expect(screen.getByTestId('consult-side-right')).toContainElement(dockEl())
    expect(stageView()).toBe('v4.morning_read') // the page stays usable
    fireEvent.click(screen.getByTestId('consult-move-left'))
    await waitFor(() => expect(screen.getByTestId('consult-side-left')).toContainElement(dockEl()))
    fireEvent.click(screen.getByTestId('consult-full'))
    await waitFor(() => expect(dockEl()).toHaveAttribute('data-placement', 'full'))
    expect(screen.queryByTestId('stage')).toBeNull()
    expect(screen.getByTestId('stage-peek')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('stage-peek'))
    await waitFor(() => expect(dockEl()).toHaveAttribute('data-placement', 'bottom'))
    expect(stageView()).toBe('v4.morning_read')
    // The question box is there in every placement.
    await waitFor(() => expect(dockEl().querySelector('[data-testid="consult-question"]'), dockEl().innerHTML.slice(0, 600)).not.toBeNull(), { timeout: 4000 })
  })

  it('collapses to the ask bar and restores; the point line, the box and the drive switch stay', async () => {
    await boot()
    fireEvent.click(await screen.findByTestId('consult-collapse'))
    await screen.findByTestId('consult-question')
    await waitFor(() => expect(dockEl()).toHaveAttribute('data-collapsed', 'true'))
    expect(await screen.findByTestId('consult-question')).toBeInTheDocument()
    expect(screen.getByTestId('consult-point-line')).toBeInTheDocument()
    expect(screen.getByTestId('consult-drive-toggle')).toBeInTheDocument()
    expect(screen.queryByTestId('consult-history-toggle')).toBeNull()
    fireEvent.click(screen.getByTestId('consult-expand'))
    await waitFor(() => expect(dockEl()).toHaveAttribute('data-collapsed', 'false'))
    expect(screen.getByTestId('consult-history-toggle')).toBeInTheDocument()
  })

  it('the divider steps by keyboard; placement and size are remembered per page', async () => {
    await boot()
    const divider = screen.getByTestId('chat-divider')
    fireEvent.keyDown(divider, { key: 'ArrowUp' })
    const stored = () => JSON.parse(localStorage.getItem(FRAME_STORE_KEY)!)
    await waitFor(() => expect(stored().pages.board.share).toBeGreaterThan(0.5))
    fireEvent.keyDown(divider, { key: 'End' })
    await waitFor(() => expect(dockEl()).toHaveAttribute('data-collapsed', 'true'))
    // Another page keeps its own frame.
    fireEvent.click(screen.getByTestId('surface-tab-ledger'))
    await waitFor(() => expect(surface()).toBe('ledger'))
    expect(dockEl()).toHaveAttribute('data-collapsed', 'false')
    fireEvent.click(screen.getByTestId('consult-move-right'))
    await waitFor(() => expect(stored().pages.ledger.placement).toBe('right'))
    fireEvent.click(screen.getByTestId('surface-tab-board'))
    await waitFor(() => expect(surface()).toBe('board'))
    expect(dockEl()).toHaveAttribute('data-placement', 'bottom')
    expect(dockEl()).toHaveAttribute('data-collapsed', 'true')
  })

  it('Undo takes a move back', async () => {
    await boot()
    fireEvent.click(screen.getByTestId('consult-move-right'))
    await waitFor(() => expect(dockEl()).toHaveAttribute('data-placement', 'right'))
    fireEvent.click(screen.getByTestId('view-undo'))
    await waitFor(() => expect(dockEl()).toHaveAttribute('data-placement', 'bottom'))
    expect(screen.getByTestId('surface-notice')).toHaveTextContent(/Undone: move Consult/)
  })
})

describe('the Record', () => {
  it('the sheet opens on a reader\'s record selection, not on a subject; Ctrl+I toggles it; Escape closes it', async () => {
    await boot()
    expect(screen.queryByTestId('record-sheet')).toBeNull()
    act(() => useSelection.getState().select({ kind: 'target', id: 't1', origin: 'test' }))
    expect(screen.queryByTestId('record-sheet')).toBeNull()
    act(() => useSelection.getState().select({ kind: 'finding', id: 'f1', origin: 'test' }))
    await waitFor(() => expect(screen.getByTestId('record-sheet')).toBeInTheDocument())
    expect(dockIds()).not.toContain('system.inspector')
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('record-sheet')).toBeNull())
    fireEvent.keyDown(window, { key: 'i', ctrlKey: true })
    await waitFor(() => expect(screen.getByTestId('record-sheet')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('record-sheet-close'))
    await waitFor(() => expect(screen.queryByTestId('record-sheet')).toBeNull())
  })

  it('a link naming a record (#rec=) opens the sheet on that record when the shell loads (roll 87: it opened nothing)', async () => {
    window.location.hash = '#ws=board&rec=finding%3Af-link'
    // The sheet stays open past the assertion and reads the record's lineage;
    // answer it in the wire's shape (the catch-all list body has no `root`, and
    // RecordChain threw on it after the test had passed: an unhandled error).
    const root = { id: 'f-link', row_kind: 'finding', title: 'Linked finding', produced_at: '2026-11-15T00:00:00Z', depth: 0, body: { body: 'Plain.' } }
    fetchBodies['/lineage/finding/f-link'] = { root, nodes: [root], edges: [], truncated_at_depth: false }
    render(wrap(<App />))
    await waitFor(() => expect(screen.getByTestId('record-sheet').getAttribute('data-record')).toBe('finding:f-link'), { timeout: 5000 })
  })

  it('a record Consult pulls up lands on the STAGE, with a crumb back — not in the sheet', async () => {
    await boot()
    act(() => {
      applyAction(actionOf('focus', { ref: { kind: 'finding', id: 'f-9' }, label: 'Port strike enters day three' }), getShell(), {
        driver: 'case',
      })
    })
    await waitFor(() => expect(stageView()).toBe('record:finding:f-9'))
    expect(screen.getByTestId('stage-crumb-1')).toHaveTextContent('Port strike enters day three')
    expect(screen.queryByTestId('record-sheet')).toBeNull()
    expect(useSelection.getState().selection).toMatchObject({ kind: 'finding', id: 'f-9' })
    fireEvent.click(screen.getByTestId('stage-back'))
    await waitFor(() => expect(stageView()).toBe('v4.morning_read'))
  })
})

describe('A BROWSER HOLDING THE PREVIOUS SHELLS\' STATE lands on the new default', () => {
  const OLD = {
    grid: {
      root: {
        type: 'branch',
        data: [
          {
            type: 'leaf',
            data: { views: ['system.findings', 'system.optimizer'], activeView: 'system.findings', id: '1' },
            size: 800,
          },
        ],
        size: 600,
      },
      width: 800,
      height: 600,
      orientation: 'HORIZONTAL',
    },
    panels: {
      'system.findings': {
        id: 'system.findings',
        contentComponent: 'default',
        title: 'Live Feed',
        params: { registration: null, singletonKind: 'system.findings', mode: 'personal' },
      },
      'system.optimizer': {
        id: 'system.optimizer',
        contentComponent: 'default',
        title: 'Optimizer',
        params: { registration: null, singletonKind: 'system.optimizer', mode: 'personal' },
      },
    },
    activeGroup: '1',
  }

  it('legba_ws, legba_layout_custom, saved missions and a light theme: ignored, kept, nothing hidden, no error', async () => {
    const errors: unknown[] = []
    const onErr = (e: ErrorEvent) => errors.push(e.error ?? e.message)
    window.addEventListener('error', onErr)
    const slot = { schemaVersion: 2, layout: OLD, layoutVersion: 'deadbeef', seedKinds: ['system.findings'] }
    localStorage.setItem('legba_ws', JSON.stringify({ morning_read: { personal: slot }, desk: { personal: slot } }))
    localStorage.setItem('legba_ws_retired', JSON.stringify({ morning_read: { personal: slot } }))
    localStorage.setItem('legba_layout_custom', JSON.stringify({ personal: OLD }))
    localStorage.setItem('legba_user_missions', JSON.stringify({ personal: [{ id: 'u1', label: 'old' }] }))
    localStorage.setItem('legba_theme', 'light')
    await boot()
    try {
      expect(surface()).toBe('board')
      for (const v of surfaceViews(findSurface('board')!)) expect(dockIds()).toContain(v)
      expect(stageView()).toBe('v4.morning_read')
      expect(dockIds()).not.toContain('system.optimizer')
      expect(dockEl()).toBeInTheDocument()
      expect(JSON.parse(localStorage.getItem('legba_ws')!).morning_read.personal.layout.panels).toHaveProperty('system.optimizer')
      expect(localStorage.getItem('legba_layout_custom')).not.toBeNull()
      const shellErrors = errors.filter((e) => /fromJSON|layout|dockview|JSON|legba_|surface|panel id/i.test(String(e)))
      expect(shellErrors).toEqual([])
    } finally {
      window.removeEventListener('error', onErr)
    }
  })

  it("the U-2 shell's stored page (an s1- slot: three regions, the read in a region) is discarded, with a one-line notice", async () => {
    const U2_BOARD = {
      grid: {
        root: {
          type: 'branch',
          data: [
            { type: 'leaf', data: { views: ['v4.map'], activeView: 'v4.map', id: '1' }, size: 700 },
            { type: 'leaf', data: { views: ['v4.morning_read', 'system.wall', 'slot:coverage'], activeView: 'v4.morning_read', id: '2' }, size: 500 },
          ],
          size: 800,
        },
        width: 1200,
        height: 800,
        orientation: 'HORIZONTAL',
      },
      panels: {
        'v4.map': { id: 'v4.map', contentComponent: 'default', title: 'Map', params: { registration: null, singletonKind: 'v4.map', mode: 'personal' } },
        'v4.morning_read': { id: 'v4.morning_read', contentComponent: 'default', title: 'Morning Read', params: { registration: null, singletonKind: 'v4.morning_read', mode: 'personal' } },
        'system.wall': { id: 'system.wall', contentComponent: 'default', title: 'The Wall', params: { registration: null, singletonKind: 'system.wall', mode: 'personal' } },
        'slot:coverage': { id: 'slot:coverage', contentComponent: 'slot', title: 'Coverage', params: { slot: 'slot:coverage' } },
      },
      activeGroup: '2',
    }
    localStorage.setItem(SURFACE_STORE_KEY, JSON.stringify({ board: { personal: { version: 's1-6f1d2a7c', layout: U2_BOARD } } }))
    // …and a frame store from another version is dropped too.
    localStorage.setItem(FRAME_STORE_KEY, JSON.stringify({ v: 'f0', pages: { board: { placement: 'nowhere' } } }))
    await boot()
    for (const v of surfaceViews(findSurface('board')!)) expect(dockIds()).toContain(v)
    expect(dockIds()).not.toContain('v4.morning_read')
    expect(stageView()).toBe('v4.morning_read')
    expect(dockEl()).toHaveAttribute('data-placement', 'bottom')
    expect(screen.getByTestId('surface-notice')).toHaveTextContent('Board was reset to its new default')
    expect(JSON.parse(localStorage.getItem(SURFACE_STORE_KEY)!).board).toBeUndefined()
  })

  it('a stored arrangement that HIDES a seeded view the reader did not close is discarded, never restored', async () => {
    const { surfaceLayoutVersion } = await import('@/lib/surfaces')
    localStorage.setItem(
      SURFACE_STORE_KEY,
      JSON.stringify({ board: { personal: { version: surfaceLayoutVersion(findSurface('board')!), layout: OLD } } }),
    )
    await boot()
    for (const v of surfaceViews(findSurface('board')!)) expect(dockIds()).toContain(v)
    expect(screen.getByTestId('surface-notice')).toHaveTextContent('so no view stays hidden')
  })
})

describe('a C-1b fixture case replays on the new frame', () => {
  it('runs every step through the one executor to the recorded end state', async () => {
    await boot()
    const rec = parseCaseRecord(C1B_FIXTURE_CASE)!
    expect(rec).not.toBeNull()
    act(() => casePlayer().replay(rec, CONSULT_PANEL_ID))
    let guard = 0
    act(() => {
      while (casePlayer().stepReplay() && guard++ < 50) {
        /* step */
      }
    })
    const steps = casePlayer().player!.steps
    expect(steps.map((s) => `${s.seq}:${s.state}`)).toEqual(rec.actions.map((a) => `${a.seq}:executed`))
    expect(useScope.getState().scope).toMatchObject({ kind: 'target', id: 'country_g20_br' })
    expect(useSelection.getState().selection).toMatchObject({ kind: 'finding', id: 'f-77' })
    expect(useWorldState.getState().windowStartMs).toBe(Date.parse('2026-09-26T12:00:00Z'))
    expect(useWorldState.getState().layers.events).toBe(true)
    expect(useWorldState.getState().layers.signals).toBe(false)
    await waitFor(() => expect(dockIds()).toContain('analysis.cross_framing'))
    expect(dockRegions().length).toBeLessThanOrEqual(MAX_REGIONS)
    // Consult is still in the frame; nothing took it away.
    expect(dockEl()).toBeInTheDocument()
  })
})

describe('#case=<session>:<turn> on boot', () => {
  it("opens the addressed turn's case as a replay in Consult", async () => {
    fetchBodies['/consult/sessions/sess-fixture'] = C1B_FIXTURE_SESSION
    window.location.hash = '#case=sess-fixture:a1'
    await boot()
    await waitFor(() => expect(casePlayer().player?.mode).toBe('replay'))
    expect(casePlayer().panelId).toBe(CONSULT_PANEL_ID)
    expect(useConsultSessions.getState().panel(CONSULT_PANEL_ID).sessionId).toBe('sess-fixture')
  })

  it('a turn that presented no case says so on the bar and replays nothing', async () => {
    fetchBodies['/consult/sessions/sess-fixture'] = C1B_FIXTURE_SESSION
    window.location.hash = '#case=sess-fixture:u1'
    await boot()
    await waitFor(() =>
      expect(screen.getByTestId('surface-notice')).toHaveTextContent('case link — turn u1 presented no case'),
    )
    expect(casePlayer().player).toBeNull()
  })
})

describe('the region budget', () => {
  it('the panel side refuses a split beyond three regions (the overlay guard)', async () => {
    const { dropWouldExceedRegions } = await import('@/lib/surfaceDock')
    const groups = (n: number) => ({ groups: Array.from({ length: n }, () => ({ api: { location: { type: 'grid' } } })) }) as never
    expect(dropWouldExceedRegions(groups(3), 'content', 'right')).toBe(true)
    expect(dropWouldExceedRegions(groups(2), 'content', 'right')).toBe(false)
    expect(dropWouldExceedRegions(groups(3), 'content', 'center')).toBe(false)
    expect(dropWouldExceedRegions(groups(3), 'tab', 'right')).toBe(false)
  })
})
