/**
 * ONE "SINCE": in one page load every region that states "since you last
 * looked" states the same since-time, read from the same cursor — the Board
 * list, the rail's change counts, the map's change layer and the Wall's
 * movers. None says "first visit" while another prints a time, and no region
 * keeps a cursor of its own in the browser.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import BoardList from './BoardList'
import { sinceWords, useChangeCounts, useVisitCursor } from './boardData'
import WallMoversPanel from '@/panels/system/WallMovers'
import { VISIT_KEY, pageVisit } from '@/lib/morningReadBands'
import { resetScope } from '@/state/scope'
import type { PanelRegistration } from '@/types'

const NOW = new Date().toISOString()
const sect = <T,>(items: T[]) => ({ items, total: items.length, truncated: false })
const SINCE = { cursor: NOW, server_now: NOW, counts: {}, new_findings: sect([]), superseded: sect([]), band_changes: sect([]), situations: sect([]), alerts: sect([]), forecasts_due: sect([]) }

function reg(): PanelRegistration {
  return {
    id: 'singleton:system.wall_movers', panel_id: 'system_wall_movers', descriptor_id: '(singleton)', descriptor_version: '0',
    descriptor_family: 'target', analyst_id: null, title: 'Movers', mode: 'personal', layout_slot: 'system.wall_movers',
    data_query: {}, binding: {}, retired: false, created_at: '2026-10-08T00:00:00Z', retired_at: null,
  }
}

let urls: string[] = []

function RailProbe() {
  const counts = useChangeCounts()
  const { cursor } = useVisitCursor()
  return <p data-testid="rail-probe" data-cursor={cursor} data-read={counts ? '1' : '0'} />
}

function onePage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <BoardList />
      <RailProbe />
      <WallMoversPanel registration={reg()} scope={{}} mode="personal" />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  urls = []
  localStorage.clear()
  sessionStorage.clear()
  resetScope()
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    urls.push(String(url))
    const body = String(url).includes('/v3/since') ? SINCE : { data: [] }
    return { ok: true, status: 200, json: async () => body, text: async () => '' } as unknown as Response
  }))
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const cursorsAsked = () => [...new Set(urls.filter((u) => u.includes('/v3/since')).map((u) => new URL(u, 'http://x').searchParams.get('cursor')))]

describe('one page load, one since-time', () => {
  it('a returning reader: every region states the same time, from the same cursor', async () => {
    const lastVisit = Date.now() - 5 * 3_600_000
    localStorage.setItem(VISIT_KEY, JSON.stringify({ nonce: 'an-earlier-tab', startedAt: lastVisit, previousVisitAt: null }))
    onePage()
    await waitFor(() => expect(screen.getByTestId('rail-probe')).toHaveAttribute('data-read', '1'))
    const v = pageVisit()
    expect(v.firstVisit).toBe(false)
    expect(Date.parse(v.cursor)).toBe(lastVisit)
    // one cursor asked, by every region
    expect(cursorsAsked()).toEqual([v.cursor])
    expect(screen.getByTestId('rail-probe')).toHaveAttribute('data-cursor', v.cursor)
    expect(screen.getByTestId('board-list-since')).toHaveAttribute('data-since', v.cursor)
    // the same words, on the Board and on the movers
    const words = sinceWords(v)
    expect(words).toMatch(/^since you last looked, /)
    expect(screen.getByTestId('board-list-since').textContent).toContain(words)
    expect(document.body.textContent).toContain(`what changed ${words}`)
    expect(document.body.textContent).not.toMatch(/first visit/)
  })

  it('a first visit: every region says the same fallback time; none prints "first visit" beside a time', async () => {
    onePage()
    await waitFor(() => expect(screen.getByTestId('rail-probe')).toHaveAttribute('data-read', '1'))
    const v = pageVisit()
    expect(v.firstVisit).toBe(true)
    expect(cursorsAsked()).toEqual([v.cursor])
    expect(screen.getByTestId('board-list-since').textContent).toContain('since yesterday 06:00Z')
    expect(document.body.textContent).toContain('what changed since yesterday 06:00Z')
    expect(document.body.textContent).not.toMatch(/first visit — last 24h/)
  })

  it('a second mount in the same page load keeps the same since (the visit is not re-resolved per region)', async () => {
    const first = onePage()
    await waitFor(() => expect(screen.getByTestId('rail-probe')).toHaveAttribute('data-read', '1'))
    const before = pageVisit().cursor
    first.unmount()
    onePage()
    await waitFor(() => expect(screen.getByTestId('rail-probe')).toHaveAttribute('data-read', '1'))
    expect(pageVisit().cursor).toBe(before)
    expect(cursorsAsked()).toEqual([before])
  })

  it('the Wall keeps no cursor of its own in the browser', async () => {
    onePage()
    await waitFor(() => expect(screen.getByTestId('rail-probe')).toHaveAttribute('data-read', '1'))
    expect(localStorage.getItem('legba_wall_cursor')).toBeNull()
  })
})
