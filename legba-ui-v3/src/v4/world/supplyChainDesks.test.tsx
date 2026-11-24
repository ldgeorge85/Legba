/**
 * The supply-chain desk hook reads one timestamp per desk, so its findings
 * read is a summary-weight page (W11-F262: 8.56 MB of the live landing at full
 * weight after roll 80).
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useSupplyChainDesks, DISRUPTION_RECENCY_READ } from './supplyChainDesks'
import { dropCachedGets } from '@/lib/queryCache'

afterEach(() => {
  vi.restoreAllMocks()
  dropCachedGets()
})

describe('useSupplyChainDesks', () => {
  it('reads the disruption recency at summary weight and keeps the newest per desk', async () => {
    const asked: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        const u = String(url)
        asked.push(u)
        if ((u.includes('/registry/descriptors') || u.includes('/v3/roster'))) {
          return {
            ok: true,
            json: async () => [
              { descriptor_id: 'lane_a', state: 'active', body: { scope: { tags: ['supply_chain'] } } },
              { descriptor_id: 'lane_b', state: 'draft', body: { scope: { tags: ['supply_chain'] } } },
            ],
          }
        }
        return {
          ok: true,
          json: async () => ({
            data: [
              { target_id: 'lane_a', produced_at: '2026-11-14T01:00:00Z' },
              { target_id: 'lane_a', produced_at: '2026-11-14T03:00:00Z' },
            ],
            next_cursor: null,
          }),
        }
      }),
    )
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    )
    const { result } = renderHook(() => useSupplyChainDesks(), { wrapper })
    await waitFor(() => expect(result.current.desks).toHaveLength(1))
    expect(result.current.desks[0]).toEqual({ targetId: 'lane_a', latestFindingAt: '2026-11-14T03:00:00Z' })
    const findings = asked.filter((u) => u.includes('/findings'))
    expect(findings).toHaveLength(1)
    expect(findings[0]).toContain('fields=summary')
    expect(DISRUPTION_RECENCY_READ).toMatch(/[?&]fields=summary$/)
  })
})
