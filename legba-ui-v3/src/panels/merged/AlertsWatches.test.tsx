/**
 * Component test for the U-3 merged Alerts & Watches (`system.alerts_watches`)
 * — proves the tab strip actually swaps between the three ORIGINAL,
 * unmodified implementations (Watches / Triggers / Deliveries) rather than
 * silently dropping one, and that the Triggers tab is labeled "preview".
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import type { PanelRegistration } from '@/types'
import AlertsWatchesMerged from './AlertsWatches'

function reg(): PanelRegistration {
  return {
    id: 'p', panel_id: 'system_alerts_watches', descriptor_id: '(singleton)', descriptor_version: '0'.repeat(64),
    descriptor_family: 'target', analyst_id: null, title: 'Alerts & Watches', mode: 'personal',
    layout_slot: 'x', data_query: {}, binding: {}, retired: false,
    created_at: '2026-06-03T00:00:00Z', retired_at: null,
  }
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

const EMPTY_ESCALATIONS = {
  summary: {
    window_hours: 24, total: 0, delivered: 0, failed: 0, logged_only: 0,
    retrying: 0, other: 0, non_delivery: 0, by_sink_status: [],
  },
  rows: [],
}

/** Per-tab endpoint shapes — each child expects a DIFFERENT payload shape
 *  (Watchlist a bare array, Escalations a {summary, rows} envelope, AlertCenter
 *  either), so a one-size-fits-all `{data:[]}` stub would crash a render. */
function stubFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) => {
      const u = String(url)
      let body: unknown = { data: [] }
      if (u.includes('/v3/watchlist')) body = []
      else if (u.includes('/v3/system/escalations')) body = EMPTY_ESCALATIONS
      else if (u.includes('/registry/descriptors')) body = []
      return Promise.resolve({ ok: true, json: async () => body })
    }),
  )
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  stubFetch()
})

describe('AlertsWatchesMerged', () => {
  it('defaults to the Watches tab (the original Watchlist panel)', async () => {
    render(wrap(<AlertsWatchesMerged registration={reg()} scope={{}} mode="personal" />))
    expect(await screen.findByTestId('watchlist-show-inactive')).toBeInTheDocument()
    expect(screen.getByTestId('alerts-watches-tab-watches')).toHaveAttribute('aria-selected', 'true')
  })

  it('labels the Triggers tab "preview"', async () => {
    render(wrap(<AlertsWatchesMerged registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('watchlist-show-inactive')
    const triggersTab = screen.getByTestId('alerts-watches-tab-triggers')
    expect(triggersTab).toHaveTextContent(/preview/i)
  })

  it('switching to Triggers mounts the original Alert Center panel', async () => {
    render(wrap(<AlertsWatchesMerged registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('watchlist-show-inactive')
    fireEvent.click(screen.getByTestId('alerts-watches-tab-triggers'))
    expect(await screen.findByTestId('alert-tail-toggle')).toBeInTheDocument()
    expect(screen.queryByTestId('watchlist-show-inactive')).not.toBeInTheDocument()
  })

  it('switching to Deliveries mounts the original Escalations panel', async () => {
    render(wrap(<AlertsWatchesMerged registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('watchlist-show-inactive')
    fireEvent.click(screen.getByTestId('alerts-watches-tab-deliveries'))
    expect(await screen.findByTestId('escalations')).toBeInTheDocument()
    expect(screen.queryByTestId('watchlist-show-inactive')).not.toBeInTheDocument()
  })
})
