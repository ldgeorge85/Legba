/**
 * Component test for the World rails KPI strip (U-1(b)).
 *
 * The substrate has no cheap count endpoint for "how many signals/findings
 * total" (see the module doc), so the strip reads a capped page and shows its
 * length, suffixing `+` when the page is saturated. This pins the honesty
 * contract: a saturated page is marked `capped` (tooltip: "display cap, not a
 * count"), and an under-cap page renders as a plain exact count with no such
 * marker.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import KpiStrip from './KpiStrip'

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

function stubFetch(signalCount: number, findingCount: number) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      const u = String(url)
      if (u.includes('/signals')) {
        return {
          ok: true,
          json: async () => ({ data: Array.from({ length: signalCount }, (_, i) => ({ id: `s${i}` })) }),
        }
      }
      if (u.includes('/findings')) {
        return {
          ok: true,
          json: async () => ({ data: Array.from({ length: findingCount }, (_, i) => ({ id: `f${i}` })) }),
        }
      }
      // situations + sources: plain-list reads, empty is fine for this test.
      return { ok: true, json: async () => [] }
    }),
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('KpiStrip', () => {
  it('marks a saturated page capped with an honest "display cap, not a count" tooltip', async () => {
    stubFetch(500, 500)
    render(wrap(<KpiStrip />))

    const capped = await waitFor(() => screen.getAllByTestId('world-kpi-capped'))
    expect(capped).toHaveLength(2) // signals + findings, both saturated at 500
    for (const el of capped) {
      expect(el.textContent).toBe('500+')
      expect(el.getAttribute('title')).toMatch(/display cap/i)
      expect(el.getAttribute('title')).toMatch(/not a live total/i)
    }
  })

  it('renders a plain exact count with no cap marker when under the limit', async () => {
    stubFetch(42, 7)
    render(wrap(<KpiStrip />))

    await waitFor(() => {
      const cards = screen.getAllByTestId('world-kpi-card')
      expect(cards[0].textContent).toContain('42')
      expect(cards[1].textContent).toContain('7')
    })
    expect(screen.queryByTestId('world-kpi-capped')).not.toBeInTheDocument()
  })
})
