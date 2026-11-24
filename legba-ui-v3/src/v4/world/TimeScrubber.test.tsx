/**
 * The scrubber over TWO CLOCKS (W8 UI-B4/time) and THE ONE CLOCK (UI-D1 D6).
 *
 *   - both axes — valid time and record time — render from a fixture of the
 *     live route shapes, as density strips and per-layer figures;
 *   - a temporal layer with no rows in the window reads *no read yet*; a
 *     route that failed reads *unmeasured*; the as-of is the record time;
 *   - every window write the bar makes goes through `setWindow` (thumbs, span
 *     presets, playback).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import TimeScrubber from './TimeScrubber'
import { useWorldState } from './worldState'

const HOUR = 3_600_000
const DAY = 24 * HOUR
const NOW = Date.now()
const iso = (msAgo: number) => new Date(NOW - msAgo).toISOString()

/** `/signals`: two published inside the window, one published BEFORE it but
 *  fetched inside it, one with no publisher date at all. */
const SIGNALS = {
  data: [
    { id: 's1', title: 'a', created_at: iso(2 * HOUR), data: { published_at: iso(3 * HOUR) } },
    { id: 's2', title: 'b', created_at: iso(1 * HOUR), data: { published_at: iso(90 * 60_000) } },
    { id: 's3', title: 'c', created_at: iso(4 * HOUR), data: { published_at: iso(3 * DAY) } },
    { id: 's4', title: 'd', created_at: iso(5 * HOUR), data: {} },
  ],
  next_cursor: null,
}
/** `/findings`: one, recorded in the window. */
const FINDINGS = { data: [{ id: 'f1', title: 'f', produced_at: iso(6 * HOUR), created_at: iso(6 * HOUR) }], next_cursor: null }
/** `/situations`: none in the window (last event and record both a week back). */
const SITUATIONS = {
  data: [{ id: 'x1', name: 'old', status: 'closed', last_event_at: iso(8 * DAY), created_at: iso(8 * DAY) }],
  next_cursor: null,
}

let failEvents = false

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

beforeEach(() => {
  failEvents = false
  useWorldState.setState({
    windowStartMs: NOW - DAY,
    windowEndMs: NOW,
    spanMs: DAY,
    playing: false,
    layers: { signals: true, findings: true, situations: true, entities: false, events: true },
  })
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      const u = String(url)
      if (u.includes('/v3/events')) {
        return failEvents
          ? { ok: false, status: 500, json: async () => ({}), text: async () => 'boom' }
          : { ok: true, json: async () => ({ data: [], next_cursor: null }) }
      }
      const body = u.includes('/signals') ? SIGNALS : u.includes('/findings') ? FINDINGS : u.includes('/situations') ? SITUATIONS : {}
      return { ok: true, json: async () => body }
    }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

const state = (id: string) => screen.getByTestId(`scrubber-layer-${id}`).getAttribute('data-state')
const total = (axis: string) =>
  (screen.getByTestId(`scrubber-axis-${axis}`).getAttribute('data-buckets') ?? '')
    .split(',')
    .reduce((a, b) => a + Number(b), 0)

describe('both clocks render under the thumbs', () => {
  it('valid and record strips, from the same rows, count different things', async () => {
    render(wrap(<TimeScrubber />))
    await waitFor(() => expect(state('signals:record')).toBe('measured'))
    // Valid: s1, s2 inside the 24h span; s3 published 3 days back (outside);
    // s4 has no valid time; f1 produced 6h back. Record: s1..s4 and f1.
    expect(total('valid')).toBe(3)
    expect(total('record')).toBe(5)
    expect(screen.getByTestId('scrubber-layer-signals:valid')).toHaveTextContent('signals 2 rows')
    expect(screen.getByTestId('scrubber-layer-signals:record')).toHaveTextContent('signals 4 rows')
  })

  it('a layer with no rows in the window reads "no read yet", never 0', async () => {
    render(wrap(<TimeScrubber />))
    await waitFor(() => expect(state('signals:record')).toBe('measured'))
    expect(state('situations:valid')).toBe('no_read_yet')
    expect(state('situations:record')).toBe('no_read_yet')
    expect(screen.getByTestId('scrubber-layer-situations:valid')).toHaveTextContent('situations no read yet')
    expect(screen.getByTestId('scrubber-axis-valid-line').textContent).not.toMatch(/situations 0/)
  })

  it('a route that failed is unmeasured, not empty', async () => {
    failEvents = true
    render(wrap(<TimeScrubber />))
    await waitFor(() => expect(state('events:valid')).toBe('unmeasured'))
  })

  it('the as-of is the latest RECORD time among the rows counted', async () => {
    render(wrap(<TimeScrubber />))
    await waitFor(() => expect(state('signals:record')).toBe('measured'))
    // The latest record time among the valid-axis rows is s2's (1h ago).
    const s2 = new Date(NOW - HOUR).toISOString()
    const z = `${s2.slice(0, 10)} ${s2.slice(11, 16)}Z`
    expect(screen.getByTestId('scrubber-axis-valid-asof')).toHaveTextContent(`as of ${z}`)
    expect(screen.getByTestId('scrubber-axis-record-asof')).toHaveTextContent(`as of ${z}`)
  })

  it('only the layers that are on are drawn', async () => {
    act(() => useWorldState.getState().setLayers(['signals']))
    render(wrap(<TimeScrubber />))
    await waitFor(() => expect(state('signals:record')).toBe('measured'))
    expect(screen.queryByTestId('scrubber-layer-findings:valid')).toBeNull()
    expect(total('record')).toBe(4)
  })
})

describe('the scrubber drives the one clock through setWindow only', () => {
  it('thumbs, span presets and playback all write through setWindow', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const real = useWorldState.getState().setWindow
    const spy = vi.fn(real)
    useWorldState.setState({ setWindow: spy })
    try {
      render(wrap(<TimeScrubber />))
      // A span preset: `setSpan` records the span, then calls setWindow.
      fireEvent.click(screen.getByRole('button', { name: '7d' }))
      expect(spy).toHaveBeenCalledTimes(1)
      expect(useWorldState.getState().spanMs).toBe(7 * DAY)
      const [s, e] = spy.mock.calls[0]
      expect(e - s).toBe(7 * DAY)
      // A thumb.
      fireEvent.change(screen.getByLabelText('Window start'), { target: { value: String(NOW - 2 * DAY) } })
      expect(spy).toHaveBeenCalledTimes(2)
      // Playback from a window that ends in the past.
      act(() => useWorldState.setState({ windowEndMs: NOW - DAY }))
      fireEvent.click(screen.getByLabelText('Play playback'))
      act(() => {
        vi.advanceTimersByTime(1_100)
      })
      expect(spy.mock.calls.length).toBeGreaterThanOrEqual(3)
    } finally {
      useWorldState.setState({ setWindow: real, playing: false })
      vi.useRealTimers()
    }
  })
})
