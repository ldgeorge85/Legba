/**
 * ONE CLOCK (UI-D1 D6; UI-B1/shell). `useWorldState`'s window is the only
 * writer of "when". The validity Timeline FOLLOWS it; its zoom, pan and fit
 * are a VIEW LENS that never writes the shared window; a visible "re-centre"
 * control drops the lens; a new shared window drops it too.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import type { PanelRegistration } from '@/types'
import type { TimelineResponse } from '@/lib/timelineWindows'
import { useWorldState } from '@/v4/world/worldState'
import { resetCasePlayer, useCasePlayer } from '@/state/casePlayer'
import TimelinePanel from './Timeline'

function reg(): PanelRegistration {
  return {
    id: 'p', panel_id: 'system_timeline', descriptor_id: 'timeline', descriptor_version: 'v'.repeat(64),
    descriptor_family: 'target', analyst_id: null, title: 'Timeline', mode: 'personal',
    layout_slot: 'x', data_query: {}, binding: {}, retired: false,
    created_at: '2026-06-03T00:00:00Z', retired_at: null,
  }
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

class MeasuringResizeObserver {
  constructor(private cb: ResizeObserverCallback) {}
  observe() {
    this.cb([{ contentRect: { width: 800 } } as unknown as ResizeObserverEntry], this as unknown as ResizeObserver)
  }
  unobserve() {}
  disconnect() {}
}

const HOUR = 3_600_000
const DAY = 24 * HOUR
const NOW = Date.now()
const iso = (msAgo: number) => new Date(NOW - msAgo).toISOString()

const TIMELINE: TimelineResponse = {
  days: 30,
  server_now: new Date(NOW).toISOString(),
  target_id: null,
  items: [
    { id: 's1', kind: 'situation', label: 'Border clashes', start: iso(20 * DAY), end: null, status: 'active' },
    { id: 'f1', kind: 'finding', label: 'Verified finding', start: iso(3 * DAY), end: iso(6 * HOUR), severity: 'high' },
  ],
  counts: { situation: 1, finding: 1 },
  truncated: {},
}

const realRO = globalThis.ResizeObserver
let urls: string[] = []

beforeEach(() => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  ;(globalThis as any).ResizeObserver = MeasuringResizeObserver
  resetCasePlayer()
  useWorldState.setState({ windowStartMs: NOW - 2 * DAY, windowEndMs: NOW - DAY, spanMs: 7 * DAY })
  urls = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      urls.push(String(url))
      return { ok: true, json: async () => (String(url).includes('/v3/timeline') ? TIMELINE : {}) }
    }),
  )
})

afterEach(() => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  ;(globalThis as any).ResizeObserver = realRO
  vi.unstubAllGlobals()
  resetCasePlayer()
})

const domain = () => screen.getByTestId('timeline-svg').getAttribute('data-domain')
const shared = () => {
  const w = useWorldState.getState()
  return `${Math.round(w.windowStartMs)}:${Math.round(w.windowEndMs)}`
}

async function mount() {
  render(wrap(<TimelinePanel registration={reg()} scope={{}} mode="personal" />))
  await waitFor(() => expect(screen.getByTestId('timeline-svg')).toBeInTheDocument())
}

describe('the Timeline follows the one clock', () => {
  it('its axis IS the shared window — not a fit of its own data', async () => {
    await mount()
    expect(domain()).toBe(shared())
    expect(screen.getByTestId('timeline-recentre')).toHaveAttribute('data-lens', 'off')
  })

  it('follows when anyone writes the shared window (a mission, the scrubber, a case)', async () => {
    await mount()
    act(() => useWorldState.getState().setWindow(NOW - 10 * DAY, NOW - 9 * DAY))
    await waitFor(() => expect(domain()).toBe(shared()))
  })

  it('centres on a case clock drive\'s `at`, still over the shared window\'s span', async () => {
    await mount()
    const at = NOW - 5 * DAY
    act(() => useCasePlayer.setState({ clockDrive: { atMs: at } }))
    const half = DAY / 2
    await waitFor(() => expect(domain()).toBe(`${Math.round(at - half)}:${Math.round(at + half)}`))
  })

  it('fetches deep enough to cover the shared window, whatever the depth select says', async () => {
    act(() => useWorldState.getState().setWindow(NOW - 60 * DAY, NOW - 50 * DAY))
    await mount()
    await waitFor(() => expect(urls.some((u) => u.includes('days=90'))).toBe(true))
  })
})

describe('zoom, pan and fit are a LENS — they never write the shared window', () => {
  it('zoom moves the axis only; re-centre returns it to the shared window', async () => {
    await mount()
    const before = { ...useWorldState.getState() }
    fireEvent.click(screen.getByTestId('timeline-zoom-in'))
    expect(domain()).not.toBe(shared())
    expect(screen.getByTestId('timeline-recentre')).toHaveAttribute('data-lens', 'on')
    fireEvent.click(screen.getByTestId('timeline-fit'))
    fireEvent.click(screen.getByTestId('timeline-zoom-out'))
    const after = useWorldState.getState()
    expect([after.windowStartMs, after.windowEndMs, after.spanMs]).toEqual([
      before.windowStartMs,
      before.windowEndMs,
      before.spanMs,
    ])
    fireEvent.click(screen.getByTestId('timeline-recentre'))
    expect(domain()).toBe(shared())
    expect(screen.getByTestId('timeline-recentre')).toHaveAttribute('data-lens', 'off')
  })

  it('a drag-pan is a lens too', async () => {
    await mount()
    const plot = screen.getByTestId('timeline-plot')
    const before = shared()
    fireEvent.mouseDown(plot, { clientX: 400 })
    fireEvent.mouseMove(plot, { clientX: 200 })
    fireEvent.mouseUp(plot)
    expect(domain()).not.toBe(before)
    expect(shared()).toBe(before)
  })

  it('a new shared window drops the lens', async () => {
    await mount()
    fireEvent.click(screen.getByTestId('timeline-zoom-in'))
    expect(screen.getByTestId('timeline-recentre')).toHaveAttribute('data-lens', 'on')
    act(() => useWorldState.getState().setWindow(NOW - 4 * DAY, NOW - 3 * DAY))
    await waitFor(() => expect(domain()).toBe(shared()))
    expect(screen.getByTestId('timeline-recentre')).toHaveAttribute('data-lens', 'off')
  })
})
