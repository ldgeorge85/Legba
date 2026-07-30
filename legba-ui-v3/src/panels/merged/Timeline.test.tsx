/**
 * Component test for the U-3 merged Timeline (`system.timeline`) — proves the
 * mode switch actually swaps between the two ORIGINAL, unmodified
 * implementations (v4 event lanes / the validity-window view) rather than
 * silently dropping one.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import type { PanelRegistration } from '@/types'
import TimelineMerged from './Timeline'

function reg(): PanelRegistration {
  return {
    id: 'p', panel_id: 'system_timeline', descriptor_id: '(singleton)', descriptor_version: '0'.repeat(64),
    descriptor_family: 'target', analyst_id: null, title: 'Timeline', mode: 'personal',
    layout_slot: 'x', data_query: {}, binding: {}, retired: false,
    created_at: '2026-06-03T00:00:00Z', retired_at: null,
  }
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

function stubFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({ ok: true, json: async () => ({ data: [], items: [], server_now: new Date().toISOString() }) }),
  )
}

beforeEach(() => {
  vi.restoreAllMocks()
  stubFetch()
})

describe('TimelineMerged', () => {
  it('defaults to the Events (v4 lanes) mode', async () => {
    render(wrap(<TimelineMerged registration={reg()} scope={{}} mode="personal" />))
    expect(await screen.findByTestId('global-timeline')).toBeInTheDocument()
    expect(screen.getByTestId('timeline-mode-events')).toHaveAttribute('aria-selected', 'true')
  })

  it('switching to Validity mounts the original validity-window panel (its own day-window control)', async () => {
    render(wrap(<TimelineMerged registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('global-timeline')
    fireEvent.click(screen.getByTestId('timeline-mode-validity'))
    expect(await screen.findByTestId('timeline-days')).toBeInTheDocument()
    expect(screen.queryByTestId('global-timeline')).not.toBeInTheDocument()
    expect(screen.getByTestId('timeline-mode-validity')).toHaveAttribute('aria-selected', 'true')
  })

  it('switching back to Events remounts the lanes view', async () => {
    render(wrap(<TimelineMerged registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('global-timeline')
    fireEvent.click(screen.getByTestId('timeline-mode-validity'))
    await screen.findByTestId('timeline-days')
    fireEvent.click(screen.getByTestId('timeline-mode-events'))
    expect(await screen.findByTestId('global-timeline')).toBeInTheDocument()
    expect(screen.queryByTestId('timeline-days')).not.toBeInTheDocument()
  })
})
