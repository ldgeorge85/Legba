/**
 * Component test for the UI-5 richer Runtime Actor Health.
 *
 * Asserts:
 *  - renders actor rows incl. the NEW `source` (SourceActor) kind
 *  - kind rollup chips render
 *  - expanding an errored actor reveals its last-error inspector
 *  - kind filter narrows the list
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import ActorHealthPanel from './ActorHealth'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 'p',
    panel_id: 'system_actor_health',
    descriptor_id: 'runtime.actors',
    descriptor_version: 'v' + 'a'.repeat(63),
    descriptor_family: 'analyst',
    analyst_id: null,
    title: 'Actor Health',
    mode: 'personal',
    layout_slot: 'system.actor_health.main',
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
    actor_id: 'source:gdelt',
    actor_kind: 'source',
    descriptor_id: 'gdelt',
    descriptor_version: 'v1abc',
    lifecycle: 'active',
    last_run_at: '2026-06-03T11:59:00Z',
    last_outcome: 'ok',
    cooldown_until: null,
    error_count: 0,
    last_error: null,
    updated_at: '2026-06-03T11:59:00Z',
  },
  {
    actor_id: 'analyst:coup',
    actor_kind: 'analyst',
    descriptor_id: 'coup.analyst',
    descriptor_version: 'v2def',
    lifecycle: 'error',
    last_run_at: '2026-06-03T10:00:00Z',
    last_outcome: 'error',
    cooldown_until: '2026-06-03T13:00:00Z',
    error_count: 3,
    last_error: 'LLM timeout after 3 retries',
    updated_at: '2026-06-03T10:00:00Z',
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

describe('ActorHealthPanel', () => {
  it('renders actors incl. the source kind, with rollup chips', async () => {
    stubFetch()
    render(wrap(<ActorHealthPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('actor-row-source:gdelt')).toBeInTheDocument())
    expect(screen.getByTestId('actor-row-analyst:coup')).toBeInTheDocument()
    expect(screen.getByTestId('actor-rollup-source')).toBeInTheDocument()
    expect(screen.getByTestId('actor-kind-pill-source')).toBeInTheDocument()
  })

  it('expands an errored actor to reveal its last error', async () => {
    stubFetch()
    render(wrap(<ActorHealthPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('actor-row-analyst:coup')).toBeInTheDocument())
    fireEvent.click(within(screen.getByTestId('actor-row-analyst:coup')).getByRole('button'))
    await waitFor(() => {
      expect(screen.getByText(/LLM timeout after 3 retries/)).toBeInTheDocument()
    })
  })

  it('kind filter narrows to source actors', async () => {
    stubFetch()
    render(wrap(<ActorHealthPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('actor-row-analyst:coup')).toBeInTheDocument())
    fireEvent.change(screen.getByTestId('actor-kind-filter'), { target: { value: 'source' } })
    await waitFor(() => {
      expect(screen.queryByTestId('actor-row-analyst:coup')).not.toBeInTheDocument()
    })
    expect(screen.getByTestId('actor-row-source:gdelt')).toBeInTheDocument()
  })
})
