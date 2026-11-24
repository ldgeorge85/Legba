/**
 * The Events lanes follow THE ONE CLOCK (UI-D1 D6; closes the W7 gap "the
 * Events-lanes mode of `system.timeline` does not follow the shared window
 * yet; it never writes it either") under the SAME lens rules as the validity
 * Timeline (`panels/system/Timeline.clock.test.tsx`): the axis IS the shared
 * window; zoom, pan and fit are a view lens that never writes it; re-centre
 * drops the lens; a new shared window drops it too. And the lanes plot either
 * clock — valid time or record time — keeping absence as absence.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import { useWorldState } from '@/v4/world/worldState'
import { resetCasePlayer, useCasePlayer } from '@/state/casePlayer'
import EventLanes from './TimelinePanel'

const HOUR = 3_600_000
const DAY = 24 * HOUR
const NOW = Date.now()
const iso = (msAgo: number) => new Date(NOW - msAgo).toISOString()

const SIGNALS = {
  data: [
    // published 3 days back, fetched 2 days back.
    { id: 's1', title: 'a', created_at: iso(2 * DAY), data: { published_at: iso(3 * DAY) } },
    // no publisher date.
    { id: 's2', title: 'b', created_at: iso(36 * HOUR), data: {} },
  ],
  next_cursor: null,
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

beforeEach(() => {
  resetCasePlayer()
  useWorldState.setState({ windowStartMs: NOW - 2 * DAY - HOUR, windowEndMs: NOW - DAY, spanMs: 7 * DAY })
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => ({
      ok: true,
      json: async () => (String(url).includes('/signals') ? SIGNALS : { data: [], next_cursor: null }),
    })),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
  resetCasePlayer()
})

const domain = () => screen.getByTestId('events-timeline-plot').getAttribute('data-domain')
const shared = () => {
  const w = useWorldState.getState()
  return `${Math.round(w.windowStartMs)}:${Math.round(w.windowEndMs)}`
}
const lens = () => screen.getByTestId('events-timeline-recentre').getAttribute('data-lens')
const laneState = (s: string) => screen.getByTestId(`events-timeline-lane-${s}`).getAttribute('data-state')

async function mount() {
  render(wrap(<EventLanes />))
  await waitFor(() => expect(screen.getByTestId('events-timeline-plot')).toBeInTheDocument())
}

describe('the Events lanes follow the one clock', () => {
  it('the axis IS the shared window, not a fit of the data', async () => {
    await mount()
    expect(domain()).toBe(shared())
    expect(lens()).toBe('off')
  })

  it('follows a window change from anyone (the scrubber, a mission, a case)', async () => {
    await mount()
    act(() => useWorldState.getState().setWindow(NOW - 10 * DAY, NOW - 9 * DAY))
    await waitFor(() => expect(domain()).toBe(shared()))
    act(() => useWorldState.getState().setSpan(DAY, NOW))
    await waitFor(() => expect(domain()).toBe(shared()))
  })

  it('centres on a case clock drive\'s `at` over the shared span', async () => {
    await mount()
    const at = NOW - 5 * DAY
    act(() => useCasePlayer.setState({ clockDrive: { atMs: at } }))
    const half = (DAY + HOUR) / 2
    await waitFor(() => expect(domain()).toBe(`${Math.round(at - half)}:${Math.round(at + half)}`))
  })
})

describe('zoom, pan and fit are a LENS — they never write the shared window', () => {
  it('every lens control moves the axis only; re-centre returns it', async () => {
    await mount()
    const before = { ...useWorldState.getState() }
    for (const id of ['zoom-in', 'zoom-out', 'pan-earlier', 'pan-later']) {
      fireEvent.click(screen.getByTestId(`events-timeline-${id}`))
    }
    await waitFor(() => expect(screen.getByTestId('events-timeline-missing')).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('events-timeline-fit'))
    fireEvent.wheel(screen.getByTestId('events-timeline-plot'), { deltaY: 100 })
    expect(domain()).not.toBe(shared())
    expect(lens()).toBe('on')
    const after = useWorldState.getState()
    expect([after.windowStartMs, after.windowEndMs, after.spanMs]).toEqual([
      before.windowStartMs,
      before.windowEndMs,
      before.spanMs,
    ])
    fireEvent.click(screen.getByTestId('events-timeline-recentre'))
    expect(domain()).toBe(shared())
    expect(lens()).toBe('off')
  })

  it('a new shared window drops the lens', async () => {
    await mount()
    fireEvent.click(screen.getByTestId('events-timeline-zoom-in'))
    expect(lens()).toBe('on')
    act(() => useWorldState.getState().setWindow(NOW - 4 * DAY, NOW - 3 * DAY))
    await waitFor(() => expect(domain()).toBe(shared()))
    expect(lens()).toBe('off')
  })
})

describe('two clocks on the lanes', () => {
  it('valid time: s1 sits 3 days back (outside the window), s2 has no valid time at all', async () => {
    await mount()
    await waitFor(() => expect(screen.getByTestId('events-timeline-missing')).toHaveTextContent('1 without a valid time'))
    expect(screen.getByTestId('events-timeline-plot')).toHaveAttribute('data-axis', 'valid')
    // Nothing in the window on the valid clock: no read yet, never "0".
    expect(laneState('signals')).toBe('no_read_yet')
    expect(screen.getByTestId('events-timeline-lane-signals')).toHaveTextContent('no read yet')
    expect(screen.getByTestId('events-timeline-empty')).toHaveTextContent('no read yet')
  })

  it('record time: both signals were LEARNED inside the window', async () => {
    await mount()
    fireEvent.click(screen.getByTestId('events-timeline-axis-record'))
    await waitFor(() => expect(laneState('signals')).toBe('measured'))
    expect(screen.getByTestId('events-timeline-lane-signals')).toHaveTextContent('2 rows')
    expect(screen.queryByTestId('events-timeline-missing')).toBeNull()
    // Switching the plotted clock is a view choice: the shared window is untouched.
    expect(domain()).toBe(shared())
  })

  it('a lane with no rows at all reads no read yet', async () => {
    await mount()
    fireEvent.click(screen.getByTestId('events-timeline-axis-record'))
    await waitFor(() => expect(laneState('signals')).toBe('measured'))
    for (const s of ['findings', 'situations', 'events']) expect(laneState(s)).toBe('no_read_yet')
  })
})
