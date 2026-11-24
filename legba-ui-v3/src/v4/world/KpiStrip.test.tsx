/**
 * Component test for the World rails KPI strip (U-1(b), W11-F271).
 *
 * "Signals 24h" and "Findings 24h" are real 24-hour counts read from
 * `GET /v3/counts?window_hours=24` — never the length of a newest-N page
 * (which printed "500+" under a "24h" label). A registry without the route
 * leaves the cards unread ("—"), never 0 and never a page length.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import KpiStrip, { KPI_READS, KPI_REFETCH_MS, KPI_WINDOW_HOURS } from './KpiStrip'
import { dropCachedGets } from '@/lib/queryCache'

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

function stubCounts(counts: { signals: number; findings: number } | 404, asked: string[] = []) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      const u = String(url)
      asked.push(u)
      if (u.includes('/v3/counts')) {
        if (counts === 404) return { ok: false, status: 404, statusText: 'Not Found', json: async () => ({ detail: 'Not Found' }), text: async () => '' }
        return { ok: true, json: async () => ({ window_hours: 24, since: '2026-11-13T12:00:00+00:00', ...counts }) }
      }
      if (u.includes('/signals') || u.includes('/findings')) {
        // a newest-500 page — must never be what the 24h cards print
        return { ok: true, json: async () => ({ data: Array.from({ length: 500 }, (_, i) => ({ id: `r${i}` })) }) }
      }
      // situations + sources: plain-list reads, empty is fine for this test.
      return { ok: true, json: async () => [] }
    }),
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  dropCachedGets()
})

describe('KpiStrip', () => {
  it('prints the real 24-hour counts, uncapped, under the 24h labels (W11-F271)', async () => {
    stubCounts({ signals: 4381, findings: 718 })
    render(wrap(<KpiStrip />))

    await waitFor(() => {
      const cards = screen.getAllByTestId('world-kpi-card')
      expect(cards[0].textContent).toContain((4381).toLocaleString())
      expect(cards[0].textContent).toMatch(/signals 24h/i)
      expect(cards[1].textContent).toContain('718')
      expect(cards[1].textContent).toMatch(/findings 24h/i)
    })
    expect(screen.queryByTestId('world-kpi-capped')).not.toBeInTheDocument()
    expect(screen.getAllByTestId('world-kpi-card')[0].textContent).not.toContain('500+')
  })

  it('leaves the 24h cards unread when the count route is absent, never a page length or 0', async () => {
    stubCounts(404)
    render(wrap(<KpiStrip />))
    await waitFor(() => {
      const cards = screen.getAllByTestId('world-kpi-card')
      expect(cards[3].textContent).toContain('0') // sources answered (empty list)
    })
    const cards = screen.getAllByTestId('world-kpi-card')
    for (const c of cards.slice(0, 2)) {
      expect(c.textContent).toContain('—')
      expect(c.textContent).not.toContain('500')
    }
  })

  it('counts active situations off the paged route by state, never a 0 over active rows (C2)', async () => {
    const asked: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        const u = String(url)
        if (u.includes('/situations')) {
          asked.push(u)
          // The route PAGES and filters by `state`; rows carry `status`.
          const n = u.includes('state=active') ? 3 : u.includes('state=escalating') ? 1 : 0
          return {
            ok: true,
            json: async () => ({
              data: Array.from({ length: n }, (_, i) => ({ id: `sit${i}`, status: 'active' })),
              next_cursor: null,
            }),
          }
        }
        if (u.includes('/v3/roster?family=source')) return { ok: true, json: async () => [] }
        return { ok: true, json: async () => ({ data: [] }) }
      }),
    )
    render(wrap(<KpiStrip />))
    await waitFor(() => {
      const cards = screen.getAllByTestId('world-kpi-card')
      expect(cards[2].textContent).toContain('4')
      expect(cards[2].textContent).toMatch(/active situations/i)
    })
    expect(asked.some((u) => u.includes('state=active'))).toBe(true)
    expect(asked.some((u) => u.includes('state=escalating'))).toBe(true)
  })

  it('shows the active-situation tile unread until the route answers, never 0', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        const u = String(url)
        if (u.includes('/situations')) return new Promise(() => {})
        if (u.includes('/v3/roster?family=source')) return { ok: true, json: async () => [] }
        return { ok: true, json: async () => ({ data: [] }) }
      }),
    )
    render(wrap(<KpiStrip />))
    const cards = await screen.findAllByTestId('world-kpi-card')
    expect(cards[2].textContent).not.toMatch(/^0/)
    expect(cards[2].textContent).toContain('—')
  })

  it('asks one count read for the 24h cards — no /signals or /findings page (W11-F262, W11-F271)', async () => {
    const asked: string[] = []
    stubCounts({ signals: 1, findings: 2 }, asked)
    render(wrap(<KpiStrip />))
    await waitFor(() => {
      const cards = screen.getAllByTestId('world-kpi-card')
      expect(cards[1].textContent).toContain('2')
    })
    expect(asked.some((u) => u.includes('/v3/counts?window_hours=24'))).toBe(true)
    expect(asked.filter((u) => u.includes('/signals') || u.includes('/findings'))).toEqual([])
    expect(asked.some((u) => u.includes('/v3/roster?family=source'))).toBe(true)
    expect(KPI_READS.counts).toBe(`/v3/counts?window_hours=${KPI_WINDOW_HOURS}`)
    expect(KPI_WINDOW_HOURS).toBe(24)
    expect(KPI_REFETCH_MS).toBeGreaterThanOrEqual(60_000)
  })
})
