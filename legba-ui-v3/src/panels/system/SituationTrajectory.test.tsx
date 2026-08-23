/**
 * Component test for the `system.situations` panel.
 *
 * Mocks the two routes at the HTTP boundary:
 *   GET /api/v1/situations                              → the register's frames
 *   GET /api/v1/v3/situations/{id}/trajectory           → the ledger
 *
 * The tests concentrate on the honesty contract the route encodes in its wire
 * shape — an unmeasured read, an empty ledger, a never-recorded state and an
 * unknown situation must each render as themselves — and on the panel being
 * indifferent to how many frames the register holds, since a later FRAME-2
 * train changes exactly that.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import SituationTrajectoryPanel from './SituationTrajectory'
import { mockErrorResponse } from '@/test/apiMocks'
import { useSelection } from '@/state/selection'
import type { SituationFrame, SituationTrajectory } from '@/lib/api'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 's1',
    panel_id: 'system_situations',
    descriptor_id: '(singleton)',
    descriptor_version: '0'.repeat(64),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Situation Trajectory',
    mode: 'personal',
    layout_slot: 'main',
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-08-01T00:00:00Z',
    retired_at: null,
  }
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

function frame(id: string, name: string): SituationFrame {
  return {
    id,
    name,
    status: 'open',
    category: 'conflict',
    last_event_at: '2026-08-18T00:00:00Z',
    event_count: 4,
    intensity_score: 0.7,
    target_id: 'target.geo.iran',
    produced_at: '2026-08-18T00:00:00Z',
  }
}

const LEDGER: SituationTrajectory = {
  situation_id: 'sit-a',
  name: 'Hormuz transit',
  state: 'escalating',
  measured: true,
  events: [
    {
      id: 'ev-1',
      delta: 'escalates',
      occurred_at: '2026-08-18T00:00:00Z',
      state_from: 'watching',
      state_to: 'escalating',
      why: 'two verified strikes inside the window',
      derived_from: ['22222222-2222-2222-2222-222222222222'],
      source_output_id: '33333333-3333-3333-3333-333333333333',
      created_at: '2026-08-18T02:00:00Z',
    },
  ],
}

let frames: SituationFrame[] = [frame('sit-a', 'Hormuz transit'), frame('sit-b', 'Border build-up')]
let trajectoryResponse: SituationTrajectory | Response = LEDGER

function mockFetch() {
  const fetchMock = vi.fn(async (url: string) => {
    const u = String(url)
    if (u.includes('/situations/') && u.includes('/trajectory')) {
      if (trajectoryResponse instanceof Object && 'ok' in trajectoryResponse) {
        return trajectoryResponse as Response
      }
      return { ok: true, json: async () => trajectoryResponse } as unknown as Response
    }
    if (u.includes('/situations')) {
      return {
        ok: true,
        json: async () => ({ data: frames, next_cursor: null }),
      } as unknown as Response
    }
    return { ok: true, json: async () => ({}) } as unknown as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  useSelection.getState().clear()
  frames = [frame('sit-a', 'Hormuz transit'), frame('sit-b', 'Border build-up')]
  trajectoryResponse = LEDGER
})

function renderPanel() {
  return render(
    wrap(<SituationTrajectoryPanel registration={reg()} scope={{}} mode="personal" />),
  )
}

describe('SituationTrajectoryPanel — the register', () => {
  it('lists the register frames and auto-opens the first one', async () => {
    mockFetch()
    renderPanel()
    await waitFor(() => {
      expect(screen.getByTestId('trajectory-frame-sit-a')).toBeInTheDocument()
    })
    expect(screen.getByTestId('trajectory-frame-sit-b')).toBeInTheDocument()
    expect(await screen.findByTestId('trajectory-events')).toBeInTheDocument()
  })

  it('renders whatever frame count the register returns (tolerant of FRAME-2 changes)', async () => {
    frames = Array.from({ length: 7 }, (_, i) => frame(`sit-${i}`, `Frame ${i}`))
    mockFetch()
    renderPanel()
    await waitFor(() => {
      expect(screen.getByTestId('trajectory-frame-sit-6')).toBeInTheDocument()
    })
    expect(screen.getByTestId('trajectory-frames').textContent).toContain('Frame 0')
  })

  it('states an empty register plainly', async () => {
    frames = []
    mockFetch()
    renderPanel()
    expect(await screen.findByTestId('trajectory-frames-empty')).toBeInTheDocument()
  })

  it('clicking a frame publishes it to the shared selection store', async () => {
    mockFetch()
    renderPanel()
    await screen.findByTestId('trajectory-frame-sit-b')
    fireEvent.click(screen.getByTestId('trajectory-frame-sit-b'))
    await waitFor(() => {
      expect(useSelection.getState().selection).toMatchObject({
        kind: 'situation',
        id: 'sit-b',
      })
    })
  })

  it('follows a situation selected by another panel', async () => {
    mockFetch()
    useSelection.getState().select({ kind: 'situation', id: 'sit-b', label: 'Border build-up' })
    renderPanel()
    await waitFor(() => {
      expect(
        screen.getByTestId('trajectory-frame-sit-b').className,
      ).toContain('border-line-strong')
    })
  })
})

describe('SituationTrajectoryPanel — the ledger', () => {
  it('renders a delta row dated by its EVIDENCE, with both drill targets', async () => {
    mockFetch()
    renderPanel()
    const row = await screen.findByTestId('trajectory-event-ev-1')
    expect(row.textContent).toContain('escalates')
    expect(row.textContent).toContain('watching → escalating')
    expect(row.textContent).toContain('two verified strikes')
    expect(screen.getByTestId('trajectory-source-ev-1')).toBeInTheDocument()
    // The delta legend is derived from the rows present, not a fixed list.
    expect(screen.getByTestId('trajectory-legend-escalates')).toHaveTextContent('×1')
  })

  it('drills into the graded situation_update finding that asserted the delta', async () => {
    mockFetch()
    renderPanel()
    fireEvent.click(await screen.findByTestId('trajectory-source-ev-1'))
    await waitFor(() => {
      expect(useSelection.getState().selection).toMatchObject({
        kind: 'finding',
        id: '33333333-3333-3333-3333-333333333333',
      })
    })
  })

  it('shows the recorded state when there is one', async () => {
    mockFetch()
    renderPanel()
    expect(await screen.findByTestId('trajectory-state')).toHaveTextContent('escalating')
  })
})

describe('SituationTrajectoryPanel — the three honest zero-states', () => {
  it('an UNMEASURED read says "could not look", not "nothing happened"', async () => {
    trajectoryResponse = { ...LEDGER, measured: false, events: [], state: null }
    mockFetch()
    renderPanel()
    expect(await screen.findByTestId('trajectory-unmeasured')).toHaveTextContent(
      /could not look/i,
    )
  })

  it('an EMPTY ledger on a known frame says it was never assessed', async () => {
    trajectoryResponse = { ...LEDGER, measured: true, events: [], state: null }
    mockFetch()
    renderPanel()
    const empty = await screen.findByTestId('trajectory-empty')
    expect(empty).toHaveTextContent(/never been assessed/i)
    expect(empty.textContent).not.toMatch(/could not look/i)
  })

  it('a NULL state renders as never-recorded rather than a fabricated default', async () => {
    trajectoryResponse = { ...LEDGER, state: null }
    mockFetch()
    renderPanel()
    expect(await screen.findByTestId('trajectory-state-null')).toHaveTextContent(
      /never recorded/i,
    )
    expect(screen.queryByTestId('trajectory-state')).not.toBeInTheDocument()
  })

  it('an UNKNOWN situation (404) is distinguished from an empty ledger', async () => {
    trajectoryResponse = mockErrorResponse(404, { detail: 'unknown situation' })
    mockFetch()
    renderPanel()
    expect(await screen.findByTestId('trajectory-not-found')).toHaveTextContent(
      /Unknown situation/i,
    )
  })
})
