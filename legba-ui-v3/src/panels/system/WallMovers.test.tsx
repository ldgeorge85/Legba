/**
 * Component test for `system.wall_movers` (U-4, COHERENCE_WAVES_PLAN_2026-07-28
 * §U-4) — the standalone boot-grid "what changed since I last looked" tile.
 *
 * Asserts it renders the movers content (band changes, situations) on a bare
 * mount with no user action — same data shape `Wall.test`-adjacent coverage
 * (`wallModel.test.ts`) already exercises for the full Wall's quadrant, just
 * through this thinner standalone panel.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import WallMoversPanel from './WallMovers'
import type { PanelRegistration } from '@/types'
import type { SinceResponse } from '@/lib/wallModel'

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
    created_at: '2026-07-28T00:00:00Z',
    retired_at: null,
  }
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

function sinceResponse(overrides: Partial<SinceResponse> = {}): SinceResponse {
  return {
    cursor: '2026-07-27T12:00:00Z',
    server_now: '2026-07-28T12:00:00Z',
    counts: {},
    new_findings: { items: [], total: 0, truncated: false },
    superseded: { items: [], total: 0, truncated: false },
    band_changes: { items: [], total: 0, truncated: false },
    situations: { items: [], total: 0, truncated: false },
    alerts: { items: [], total: 0, truncated: false },
    ...overrides,
  }
}

function stubFetch(body: SinceResponse) {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => body })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('WallMoversPanel', () => {
  it('renders a band change with no user action (cold-boot answer to "what changed")', async () => {
    stubFetch(
      sinceResponse({
        band_changes: {
          items: [
            {
              target_id: 'country_g20_br',
              dimension: 'stability',
              from_band: 'Moderate',
              to_band: 'Low',
              direction: 'deterioration',
              severity: 'high',
              from_scorecard_row_id: 'a',
              to_scorecard_row_id: 'b',
              changed_at: '2026-07-28T10:00:00Z',
            },
          ],
          total: 1,
          truncated: false,
        },
      }),
    )
    render(wrap(<WallMoversPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('wall-band-change')).toBeInTheDocument())
    expect(screen.getByTestId('wall-movers-strip')).toBeInTheDocument()
  })

  it('renders the honest empty state when nothing moved', async () => {
    stubFetch(sinceResponse())
    render(wrap(<WallMoversPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('wall-movers-empty')).toBeInTheDocument())
  })

  it('does NOT render the other Wall quadrants (band grid / newest findings / health) — this tile is movers-only', async () => {
    stubFetch(sinceResponse())
    render(wrap(<WallMoversPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('wall-movers-strip')).toBeInTheDocument())
    expect(screen.queryByTestId('wall-band-grid')).not.toBeInTheDocument()
    expect(screen.queryByTestId('wall-new-findings')).not.toBeInTheDocument()
    expect(screen.queryByTestId('wall-health')).not.toBeInTheDocument()
    // And no doubled chrome: exactly one "Movers since last visit"-style
    // header from PanelChrome, not a second nested Quadrant header on top.
    expect(screen.queryByText('Movers since last visit')).not.toBeInTheDocument()
  })
})
