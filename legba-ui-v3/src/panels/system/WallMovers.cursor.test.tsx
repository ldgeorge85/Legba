/**
 * X1/D1 — the Wall's "since last visit" cursor is stamped by a READ, never by
 * the page's own load.
 *
 * Review D (2026-10-08, `D03-wall-tab.png` → `D03b-wall-movers.png`): the
 * Overview said "band changes 31"; four seconds later the Movers tab said
 * "nothing changed since <the moment the page loaded>". Every `/v3/since`
 * response stamped the stored cursor (a background tile included), and every
 * mount re-read the anchor from storage, so the second surface diffed from
 * the first one's stamp.
 *
 * Pinned here:
 *   * a movers list fetched in a tile that is NOT visible stamps nothing;
 *   * the same list becoming visible is the read that stamps it;
 *   * a second Wall surface mounted later in the same page load asks the SAME
 *     question (one anchor) and shows the same movers — not "nothing changed".
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

import WallMoversPanel from './WallMovers'
import { DockviewPanelApiProvider, type TilePanelApi } from '@/components/DockviewPanelApiContext'
import { WALL_CURSOR_KEY, __resetWallVisit, type SinceResponse } from '@/lib/wallModel'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 'singleton:system.wall_movers',
    panel_id: 'system_wall_movers',
    descriptor_id: '(singleton)',
    descriptor_version: '00000000',
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Movers Since Last Visit',
    mode: 'personal',
    layout_slot: 'system.wall_movers',
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-10-08T00:00:00Z',
    retired_at: null,
  }
}

const SERVER_NOW = '2026-10-08T17:58:32Z'

function since(): SinceResponse {
  return {
    cursor: '2026-10-07T17:58:00Z',
    server_now: SERVER_NOW,
    counts: {},
    new_findings: { items: [], total: 0, truncated: false },
    superseded: { items: [], total: 0, truncated: false },
    band_changes: {
      items: [
        {
          target_id: 'country_g20_br',
          dimension: 'stability',
          from_band: 'Moderate',
          to_band: 'Elevated',
          direction: 'deterioration',
          severity: 'high',
          from_scorecard_row_id: 'a',
          to_scorecard_row_id: 'b',
          changed_at: '2026-10-08T10:00:00Z',
        },
      ],
      total: 1,
      truncated: false,
    },
    situations: { items: [], total: 0, truncated: false },
    alerts: { items: [], total: 0, truncated: false },
  }
}

/** A Dockview tile api whose visibility the test drives. */
function fakeTileApi(initiallyVisible: boolean) {
  let listener: ((e: { isVisible: boolean }) => void) | null = null
  const api = {
    isVisible: initiallyVisible,
    onDidVisibilityChange: (cb: (e: { isVisible: boolean }) => void) => {
      listener = cb
      return { dispose: () => (listener = null) }
    },
  }
  return {
    api: api as unknown as TilePanelApi,
    show() {
      api.isVisible = true
      listener?.({ isVisible: true })
    },
  }
}

function inTile(ui: ReactElement, api: TilePanelApi | null, qc: QueryClient) {
  return (
    <QueryClientProvider client={qc}>
      <DockviewPanelApiProvider value={api}>{ui}</DockviewPanelApiProvider>
    </QueryClientProvider>
  )
}

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  localStorage.clear()
  __resetWallVisit()
  fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => since() })
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('the Wall movers cursor (X1/D1)', () => {
  it('a background tile fetching the movers does NOT stamp the cursor; showing it does', async () => {
    const tile = fakeTileApi(false)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(inTile(<WallMoversPanel registration={reg()} scope={{}} mode="personal" />, tile.api, qc))
    await waitFor(() => expect(screen.getByTestId('wall-band-change')).toBeInTheDocument())
    // Fetched and rendered — but nobody could see it. Not a read.
    expect(localStorage.getItem(WALL_CURSOR_KEY)).toBeNull()

    act(() => tile.show())
    await waitFor(() => expect(localStorage.getItem(WALL_CURSOR_KEY)).toBe(SERVER_NOW))
  })

  it('a second Wall surface in the same page load diffs from the SAME anchor, not from the first one’s read', async () => {
    const qc1 = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const first = render(
      inTile(<WallMoversPanel registration={reg()} scope={{}} mode="personal" />, null, qc1),
    )
    await waitFor(() => expect(screen.getByTestId('wall-band-change')).toBeInTheDocument())
    // Outside a tile the list counts as shown: this IS a read.
    await waitFor(() => expect(localStorage.getItem(WALL_CURSOR_KEY)).toBe(SERVER_NOW))
    const firstUrl = String(fetchMock.mock.calls[0][0])
    first.unmount()

    // The Movers tab mounts next (a fresh query cache, so it must re-ask).
    const qc2 = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(inTile(<WallMoversPanel registration={reg()} scope={{}} mode="personal" />, null, qc2))
    await waitFor(() => expect(screen.getByTestId('wall-band-change')).toBeInTheDocument())
    const secondUrl = String(fetchMock.mock.calls[fetchMock.mock.calls.length - 1][0])
    expect(secondUrl).toBe(firstUrl)
    expect(secondUrl).not.toContain(encodeURIComponent(SERVER_NOW))
    expect(screen.queryByTestId('wall-movers-empty')).toBeNull()
  })

  it('the NEXT page load diffs from the last read', async () => {
    // A read an hour ago (relative to the real clock, so the 90-day and
    // future clamps in resolveWallCursor never move it).
    const lastRead = new Date(Date.now() - 3_600_000).toISOString()
    localStorage.setItem(WALL_CURSOR_KEY, lastRead)
    __resetWallVisit()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(inTile(<WallMoversPanel registration={reg()} scope={{}} mode="personal" />, null, qc))
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    expect(String(fetchMock.mock.calls[0][0])).toContain(encodeURIComponent(lastRead))
  })
})
