/**
 * Component test for the UI-3 Target Timeline.
 *
 * recharts' ResponsiveContainer needs a sized DOM which jsdom lacks, so
 * we assert on the chrome/subtitle/empty-state + the situation fetch
 * wiring rather than SVG geometry (the point/span math is covered by
 * `lib/timelinePoints.test.ts`).
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import TargetTimelinePanel from './Timeline'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 't1',
    panel_id: 'target_timeline',
    descriptor_id: 'brazil',
    descriptor_version: 'v' + 'a'.repeat(63),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Target Timeline',
    mode: 'personal',
    layout_slot: 'target.timeline.main',
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-05-20T00:00:00Z',
    retired_at: null,
  }
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

/** Route a mocked fetch by path → page payload. */
function routedFetch(routes: Record<string, unknown>) {
  return vi.fn(async (url: string) => {
    const key = Object.keys(routes).find((k) => url.includes(k))
    return {
      ok: true,
      json: async () => routes[key ?? ''] ?? { data: [], next_cursor: null },
    }
  })
}

describe('TargetTimelinePanel', () => {
  it('shows the empty state when there is no substrate data', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch({
        '/signals': { data: [], next_cursor: null },
        '/findings': { data: [], next_cursor: null },
        '/situations': { data: [], next_cursor: null },
      }),
    )
    render(wrap(<TargetTimelinePanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />))
    await waitFor(() => {
      expect(screen.getByTestId('target-timeline-empty')).toBeInTheDocument()
    })
  })

  it('summarizes signals + findings + situations in the subtitle', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch({
        '/signals': {
          data: [{ id: 's1', title: 's', category: 'news', produced_at: '2026-06-01T00:00:00Z' }],
          next_cursor: null,
        },
        '/findings': {
          data: [
            { id: 'f1', title: 'f', analyst_id: 'a', severity: 'high', produced_at: '2026-06-02T00:00:00Z' },
          ],
          next_cursor: null,
        },
        '/situations': {
          data: [
            {
              id: 'sit1',
              name: 'Escalation',
              status: 'active',
              category: 'conflict',
              produced_at: '2026-06-01T00:00:00Z',
              last_event_at: '2026-06-03T00:00:00Z',
              event_count: 5,
              intensity_score: 0.8,
            },
          ],
          next_cursor: null,
        },
      }),
    )
    render(wrap(<TargetTimelinePanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />))
    await waitFor(() => {
      expect(screen.getByText(/1 signals · 1 findings · 1 situations/)).toBeInTheDocument()
    })
  })

  it('degrades gracefully when /situations 404s', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url.includes('/situations')) {
          return { ok: false, status: 404, json: async () => ({ detail: 'no situations' }) }
        }
        return {
          ok: true,
          json: async () => ({
            data: [{ id: 's1', title: 's', category: 'news', produced_at: '2026-06-01T00:00:00Z' }],
            next_cursor: null,
          }),
        }
      }),
    )
    render(wrap(<TargetTimelinePanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />))
    await waitFor(() => {
      expect(screen.getByText(/1 signals · 1 findings · 0 situations/)).toBeInTheDocument()
    })
  })
})
