/**
 * Component test for the UI-5 Governor Events panel.
 *
 * Asserts:
 *  - renders block + allow rows from the mocked `/registry/governor_events`
 *  - block cause-breakdown chips render
 *  - a mocked WS tail appends a live block row + badges it
 *  - decision filter re-queries
 *
 * `@/lib/ws` is mocked to drive the live tail deterministically; `fetch` is
 * stubbed at the HTTP boundary.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import type { RegistryEvent } from '@/lib/ws'
import type { PanelRegistration } from '@/types'

let capturedOnEvent: ((ev: RegistryEvent) => void) | null = null
const closeSpy = vi.fn()
vi.mock('@/lib/ws', () => ({
  subscribeRegistryEvents: (_f: string, onEvent: (ev: RegistryEvent) => void) => {
    capturedOnEvent = onEvent
    return { close: closeSpy }
  },
}))

import GovernorEventsPanel from './GovernorEvents'

function reg(): PanelRegistration {
  return {
    id: 'p',
    panel_id: 'system_governor',
    descriptor_id: 'governor.events',
    descriptor_version: 'v' + 'a'.repeat(63),
    descriptor_family: 'analyst',
    analyst_id: null,
    title: 'Governor Events',
    mode: 'personal',
    layout_slot: 'system.governor.main',
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
    pack_id: 'osint',
    decision: 'block',
    cause: 'over_budget',
    tool_name: 'web.search',
    budget_account: 'system',
    requested_by: 'analyst:x',
    tenant_id: 'default',
    cap_dimension: 'usd',
    cap_limit: 5,
    observed_value: 6.2,
    detail: 'daily usd cap exceeded',
    occurred_at: '2026-06-03T09:00:00Z',
  },
  {
    pack_id: 'osint',
    decision: 'allow',
    cause: 'ok',
    tool_name: 'web.fetch',
    budget_account: 'system',
    requested_by: 'analyst:x',
    tenant_id: 'default',
    cap_dimension: null,
    cap_limit: null,
    observed_value: null,
    detail: '',
    occurred_at: '2026-06-03T08:00:00Z',
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
  capturedOnEvent = null
  closeSpy.mockClear()
})

describe('GovernorEventsPanel', () => {
  it('renders block + allow rows and cause chips', async () => {
    stubFetch()
    render(wrap(<GovernorEventsPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('governor-list')).toBeInTheDocument())
    expect(screen.getByTestId('governor-row-block')).toBeInTheDocument()
    expect(screen.getByTestId('governor-row-allow')).toBeInTheDocument()
    expect(screen.getByTestId('governor-causes')).toHaveTextContent('over_budget: 1')
  })

  it('appends a live block from the mocked WS tail', async () => {
    stubFetch()
    render(wrap(<GovernorEventsPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('governor-list')).toBeInTheDocument())
    expect(capturedOnEvent).toBeTypeOf('function')

    act(() => {
      capturedOnEvent!({
        type: 'event',
        subject: 'governor.events.default.osint.block',
        payload: {
          pack_id: 'osint',
          decision: 'block',
          cause: 'rate_limited',
          occurred_at: '2026-06-03T12:00:00Z',
        },
        ts: '2026-06-03T12:00:00Z',
      })
    })

    await waitFor(() => {
      expect(screen.getByTestId('governor-causes')).toHaveTextContent('rate_limited: 1')
    })
  })

  it('decision filter re-queries with decision=block', async () => {
    const fetchMock = stubFetch()
    render(wrap(<GovernorEventsPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('governor-list')).toBeInTheDocument())
    fireEvent.change(screen.getByTestId('governor-decision-filter'), {
      target: { value: 'block' },
    })
    await waitFor(() => {
      const calls = fetchMock.mock.calls.map((c) => String(c[0]))
      expect(calls.some((u) => u.includes('decision=block'))).toBe(true)
    })
  })
})
