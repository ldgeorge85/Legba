/**
 * The time band: a histogram by when things happened, a brush that is the one
 * writer of the shared window, the future side with forecast ticks, and the
 * two times named in plain words.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import TimeBand from './TimeBand'
import { useWorldState } from './worldState'
import { resetAmbientPins } from '@/lib/consultContext'
import { useLedgerView, resetLedgerView } from '@/panels/ledger/ledgerView'
import { resetScope, useScope } from '@/state/scope'
import { scopeFromTarget } from '@/lib/scopeFromReport'

const panelCalls: Array<[string, string, Record<string, unknown>]> = []
vi.mock('@/lib/panelActions', async (orig) => {
  const real = await orig<typeof import('@/lib/panelActions')>()
  return {
    ...real,
    applyPanelAction: (id: string, action: string, args: Record<string, unknown> = {}) => {
      panelCalls.push([id, action, args])
      return { state: 'executed' }
    },
  }
})

const HOUR = 3_600_000
const DAY = 24 * HOUR
const NOW = Date.now()
const iso = (msAgo: number) => new Date(NOW - msAgo).toISOString()

const SIGNALS = {
  data: [
    { id: 's1', title: 'a', created_at: iso(2 * HOUR), data: { published_at: iso(3 * HOUR) } },
    { id: 's2', title: 'b', created_at: iso(1 * HOUR), data: { published_at: iso(90 * 60_000) } },
    { id: 's3', title: 'c', created_at: iso(4 * HOUR), data: { published_at: iso(3 * DAY) } },
    { id: 's4', title: 'd', created_at: iso(5 * HOUR), data: {} },
  ],
  next_cursor: null,
}
const FINDINGS = { data: [{ id: 'f1', title: 'f', produced_at: iso(6 * HOUR), created_at: iso(6 * HOUR) }], next_cursor: null }
const LEDGER = {
  version: 'x',
  available: true,
  forecasts: [
    { id: '1', slug: 'soon', question: 'Will the strait reopen?', resolver_kind: 'm', due_at: new Date(NOW + 5 * DAY).toISOString(), status: 'open', numbers: [] },
    { id: '2', slug: 'done', question: 'Closed?', resolver_kind: 'm', due_at: new Date(NOW + 2 * DAY).toISOString(), status: 'resolved', numbers: [] },
  ],
}

let urls: string[] = []
let failEvents = false

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

beforeEach(() => {
  urls = []
  failEvents = false
  panelCalls.length = 0
  resetAmbientPins()
  resetScope()
  resetLedgerView()
  useWorldState.setState({ windowStartMs: NOW - DAY, windowEndMs: NOW, spanMs: DAY, playing: false, asOfMs: null })
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      const u = String(url)
      urls.push(u)
      if (u.includes('/v3/events')) {
        return failEvents
          ? { ok: false, status: 500, json: async () => ({}), text: async () => 'boom' }
          : { ok: true, json: async () => ({ data: [], next_cursor: null }) }
      }
      const body = u.includes('/ledger/forecasts')
        ? LEDGER
        : u.includes('/signals')
          ? SIGNALS
          : u.includes('/findings')
            ? FINDINGS
            : { data: [], next_cursor: null }
      return { ok: true, json: async () => body }
    }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

const buckets = (id: string) =>
  (screen.getByTestId(id).getAttribute('data-buckets') ?? '').split(',').reduce((a, b) => a + Number(b), 0)

describe('the chart', () => {
  it('counts items by when they happened; the lane under it by when recorded', async () => {
    render(wrap(<TimeBand />))
    await waitFor(() => expect(buckets('time-band-recorded')).toBe(5))
    // s1, s2, s3 (three days back, inside the week the band shows) and f1;
    // s4 carries no date of its own.
    expect(buckets('time-band-histogram')).toBe(4)
    expect(screen.getByTestId('time-band-chart').getAttribute('title')).toMatch(/1 carry no date of their own/)
  })

  it('a zero bucket draws nothing; a non-zero bucket keeps the minimum height, never a sliver below it', async () => {
    render(wrap(<TimeBand />))
    await waitFor(() => expect(buckets('time-band-histogram')).toBe(4))
    for (const id of ['time-band-histogram', 'time-band-recorded']) {
      const row = screen.getByTestId(id)
      const counts = (row.getAttribute('data-buckets') ?? '').split(',').map(Number)
      const bars = [...row.children] as HTMLElement[]
      expect(bars).toHaveLength(counts.length)
      // the far-left bucket of this read is empty: nothing is drawn there
      expect(counts[0]).toBe(0)
      bars.forEach((bar, i) => {
        const h = parseFloat(bar.style.height || '0')
        if (counts[i] === 0) expect(h, `${id} bucket ${i}`).toBe(0)
        else expect(h, `${id} bucket ${i}`).toBeGreaterThanOrEqual(id === 'time-band-histogram' ? 8 : 20)
      })
    }
  })

  it('both rows are named in plain words', () => {
    render(wrap(<TimeBand />))
    expect(screen.getByText('When it happened')).toBeInTheDocument()
    expect(screen.getByText('When recorded')).toBeInTheDocument()
  })

  it('counts everything with no subject, and only the subject once one is the point', async () => {
    render(wrap(<TimeBand />))
    expect(screen.getByTestId('time-band-counting')).toHaveTextContent('Counting: everything')
    await waitFor(() => expect(urls.some((u) => u.includes('/signals'))).toBe(true))
    expect(urls.some((u) => u.includes('target_id='))).toBe(false)
    act(() => useScope.getState().setScope(scopeFromTarget('brazil', 'Brazil')))
    await waitFor(() => expect(urls.some((u) => u.includes('/signals') && u.includes('target_id=brazil'))).toBe(true))
    expect(urls.some((u) => u.includes('/findings') && u.includes('target_id=brazil'))).toBe(true)
    expect(screen.getByTestId('time-band-counting')).toHaveTextContent('Counting: Brazil')
  })

  it('a read that failed is named, not drawn as quiet', async () => {
    failEvents = true
    render(wrap(<TimeBand />))
    await waitFor(() => expect(screen.getByTestId('time-band-counting')).toHaveTextContent('events not read'))
  })
})

describe('the band is the window’s one writer', () => {
  it('offers 24h, 7d, 30d and no speed buttons; a span writes through setWindow', () => {
    const real = useWorldState.getState().setWindow
    const spy = vi.fn(real)
    useWorldState.setState({ setWindow: spy })
    try {
      render(wrap(<TimeBand />))
      const group = screen.getByRole('group', { name: 'Window span' })
      expect([...group.querySelectorAll('button')].map((b) => b.textContent)).toEqual(['24h', '7d', '30d'])
      expect(screen.queryByText('2x')).toBeNull()
      fireEvent.click(screen.getByRole('button', { name: '7d' }))
      expect(spy).toHaveBeenCalledTimes(1)
      const [s, e] = spy.mock.calls[0]
      expect(e - s).toBe(7 * DAY)
      expect(screen.getByRole('button', { name: '7d' })).toHaveAttribute('aria-pressed', 'true')
    } finally {
      useWorldState.setState({ setWindow: real })
    }
  })

  it('the brush reads the window in words and moves with the keyboard', () => {
    useWorldState.setState({ windowStartMs: NOW - 3 * DAY, windowEndMs: NOW - 2 * DAY })
    render(wrap(<TimeBand />))
    const brush = screen.getByRole('slider', { name: 'Time window' })
    expect(brush.getAttribute('aria-valuetext')).toMatch(/ to .* UTC \(24 hours\)$/)
    const before = useWorldState.getState()
    fireEvent.keyDown(brush, { key: 'ArrowRight' })
    const moved = useWorldState.getState()
    expect(moved.windowStartMs).toBeGreaterThan(before.windowStartMs)
    expect(moved.windowEndMs - moved.windowStartMs).toBe(DAY)
    fireEvent.keyDown(brush, { key: 'ArrowRight', shiftKey: true })
    const grown = useWorldState.getState()
    expect(grown.windowStartMs).toBe(moved.windowStartMs)
    expect(grown.windowEndMs - grown.windowStartMs).toBeGreaterThan(DAY)
    fireEvent.keyDown(brush, { key: 'End' })
    expect(useWorldState.getState().windowEndMs).toBe(Math.max(NOW, useWorldState.getState().windowEndMs))
    expect(screen.getByTestId('time-band-window')).toHaveTextContent(/to now/)
  })

  it('play steps the window a day at a time and stops at now', () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      useWorldState.setState({ windowStartMs: NOW - 3 * DAY, windowEndMs: NOW - 2 * DAY })
      render(wrap(<TimeBand />))
      fireEvent.click(screen.getByTestId('time-band-play'))
      act(() => {
        vi.advanceTimersByTime(1_050)
      })
      expect(useWorldState.getState().windowEndMs).toBe(NOW - DAY)
      act(() => {
        vi.advanceTimersByTime(3_000)
      })
      expect(useWorldState.getState().playing).toBe(false)
      expect(NOW + 10_000 - useWorldState.getState().windowEndMs).toBeGreaterThanOrEqual(0)
    } finally {
      useWorldState.setState({ playing: false })
      vi.useRealTimers()
    }
  })
})

describe('the future side', () => {
  it('draws a tick per open forecast; focus shows the question, a click opens it on the Ledger', async () => {
    render(wrap(<TimeBand />))
    const tick = await screen.findByTestId('time-band-forecast-soon')
    expect(screen.queryByTestId('time-band-forecast-done')).toBeNull()
    expect(tick).toHaveAccessibleName(/Forecast due .*: Will the strait reopen\?/)
    fireEvent.focus(tick)
    expect(screen.getByTestId('time-band-tick-detail')).toHaveTextContent('Will the strait reopen?')
    fireEvent.click(tick)
    expect(useLedgerView.getState().forecast).toBe('soon')
    expect(panelCalls.map(([id, a]) => `${id} ${a}`)).toEqual(['view:forecasts open', 'view:forecasts show_forecast'])
  })
})

describe('as recorded', () => {
  it('the marker sits at now; arrows move it back, and End returns it', () => {
    render(wrap(<TimeBand />))
    const marker = screen.getByRole('slider', { name: 'As recorded' })
    expect(marker).toHaveAttribute('aria-valuetext', 'now')
    expect(screen.getByTestId('time-band-as-of')).toHaveTextContent('As recorded: now')
    fireEvent.keyDown(marker, { key: 'ArrowLeft' })
    const at = useWorldState.getState().asOfMs
    expect(at).not.toBeNull()
    expect(at!).toBeLessThan(NOW)
    expect(marker.getAttribute('aria-valuetext')).toMatch(/^the record as of \d+ \w+ \d\d:\d\d UTC$/)
    expect(screen.getByTestId('time-band-as-of')).toHaveTextContent(/As recorded .* UTC/)
    fireEvent.keyDown(marker, { key: 'End' })
    expect(useWorldState.getState().asOfMs).toBeNull()
  })

  it('on a past record the chart counts only what was recorded by then, and the ticks read the ledger as it stood', async () => {
    useWorldState.setState({ asOfMs: NOW - 150 * 60_000 })
    render(wrap(<TimeBand />))
    // Recorded by 2.5 h ago: s1 (2 h ago) no, s3 (4 h) yes, s4 (5 h) yes but
    // undated, f1 (6 h) yes; s2 (1 h) no.
    await waitFor(() => expect(buckets('time-band-recorded')).toBe(3))
    expect(buckets('time-band-histogram')).toBe(2)
    expect(screen.getByTestId('time-band-counting')).toHaveTextContent('signals and findings recorded by then')
    await waitFor(() => expect(urls.some((u) => u.includes('/v3/ledger/forecasts?at='))).toBe(true))
    fireEvent.click(screen.getByTestId('time-band-as-of-return'))
    expect(useWorldState.getState().asOfMs).toBeNull()
  })
})
