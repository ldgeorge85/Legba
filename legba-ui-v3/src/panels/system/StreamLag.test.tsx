/**
 * Component test for the UI-5 NATS Consumer-Lag Monitor.
 *
 * Asserts:
 *  - renders per-source / per-target consumer rows from the mocked endpoint
 *  - severity classification (ok / critical) shows on the right rows
 *  - worst-lag-first ordering
 *  - scope filter narrows to source consumers
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import StreamLagPanel from './StreamLag'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 'p',
    panel_id: 'system_stream_lag',
    descriptor_id: 'streams.lag',
    descriptor_version: 'v' + 'a'.repeat(63),
    descriptor_family: 'analyst',
    analyst_id: null,
    title: 'Consumer-Lag Monitor',
    mode: 'personal',
    layout_slot: 'system.stream_lag.main',
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

const PAGE = [
  {
    stream: 'SIGNALS',
    durable: 'tgt-brazil',
    scope_kind: 'target',
    scope_id: 'brazil',
    num_pending: 3,
    num_ack_pending: 0,
    num_redelivered: 0,
    num_waiting: 1,
    delivered_stream_seq: 100,
    ack_floor_stream_seq: 100,
  },
  {
    stream: 'SIGNALS',
    durable: 'src-gdelt',
    scope_kind: 'source',
    scope_id: 'gdelt',
    num_pending: 1500,
    num_ack_pending: 2,
    num_redelivered: 40,
    num_waiting: 0,
    delivered_stream_seq: 9000,
    ack_floor_stream_seq: 7500,
  },
]

function stubFetch() {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => PAGE })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('StreamLagPanel', () => {
  it('renders rows worst-lag-first with severity pills', async () => {
    stubFetch()
    render(wrap(<StreamLagPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('lag-row-gdelt')).toBeInTheDocument())
    // gdelt (1500 pending, critical) sorts above brazil (3 pending, ok)
    const list = screen.getByTestId('lag-list')
    const rows = within(list).getAllByTestId(/^lag-row-/)
    expect(rows[0]).toHaveAttribute('data-testid', 'lag-row-gdelt')
    expect(within(rows[0]).getByTestId('lag-sev-critical')).toBeInTheDocument()
    expect(within(screen.getByTestId('lag-row-brazil')).getByTestId('lag-sev-ok')).toBeInTheDocument()
  })

  it('scope filter narrows to source consumers', async () => {
    stubFetch()
    render(wrap(<StreamLagPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('lag-row-brazil')).toBeInTheDocument())
    fireEvent.change(screen.getByTestId('lag-scope-filter'), { target: { value: 'source' } })
    await waitFor(() => {
      expect(screen.queryByTestId('lag-row-brazil')).not.toBeInTheDocument()
    })
    expect(screen.getByTestId('lag-row-gdelt')).toBeInTheDocument()
  })
})
