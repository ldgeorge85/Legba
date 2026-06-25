/**
 * Component test for the UI-6 Global Search panel.
 *
 * `fetch` is stubbed at the HTTP boundary; each LIVE endpoint returns a canned
 * page. Asserts: fan-out hits render across every live kind (signal · finding ·
 * situation · source), a query ranks/filters them, and a kind facet toggle
 * hides a kind.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import type { PanelRegistration } from '@/types'
import GlobalSearchPanel from './Search'

function reg(): PanelRegistration {
  return {
    id: 'p', panel_id: 'system_search', descriptor_id: 'search', descriptor_version: 'v'.repeat(64),
    descriptor_family: 'target', analyst_id: null, title: 'Global Search', mode: 'personal',
    layout_slot: 'x', data_query: {}, binding: {}, retired: false,
    created_at: '2026-06-03T00:00:00Z', retired_at: null,
  }
}
function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

/** Route each LIVE endpoint to its canned payload. */
function stubFetch() {
  const mock = vi.fn((url: string) => {
    const u = String(url)
    let body: unknown = []
    if (u.includes('/signals'))
      body = { data: [{ id: 'sig1', title: 'Coup signal feed', source_id: 'source.gdelt', geo: ['BR'], tags: ['news'], target_id: 'brazil', produced_at: '2026-06-03T00:00:00Z' }] }
    else if (u.includes('/findings'))
      body = { data: [{ id: 'f1', title: 'Coup finding', body: 'army', target_id: 'brazil', severity: 'high', produced_at: '2026-06-03T00:00:00Z' }] }
    else if (u.includes('/situations'))
      body = [{ id: 's1', summary: 'Brazil escalating', target_id: 'brazil', opened_at: '2026-06-02T00:00:00Z' }]
    else if (u.includes('/registry/sources'))
      body = [{ descriptor_id: 'src.gdelt', name: 'GDELT', kind: 'gdelt_query', state: 'active' }]
    return Promise.resolve({ ok: true, json: async () => body })
  })
  vi.stubGlobal('fetch', mock)
  return mock
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('GlobalSearchPanel', () => {
  it('fans out and renders hits from every live kind (browse mode)', async () => {
    stubFetch()
    render(wrap(<GlobalSearchPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('search-hit-signal-sig1')).toBeInTheDocument())
    expect(screen.getByTestId('search-hit-finding-f1')).toBeInTheDocument()
    expect(screen.getByTestId('search-hit-situation-s1')).toBeInTheDocument()
    expect(screen.getByTestId('search-hit-source-src.gdelt')).toBeInTheDocument()
  })

  it('a query ranks matches and drops non-matches', async () => {
    stubFetch()
    render(wrap(<GlobalSearchPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('search-hit-finding-f1')).toBeInTheDocument())

    fireEvent.change(screen.getByTestId('search-input'), { target: { value: 'coup' } })
    fireEvent.submit(screen.getByTestId('search-submit').closest('form')!)

    await waitFor(() => {
      const results = screen.getByTestId('search-results')
      // signal ('Coup signal feed') + finding ('Coup finding') match; the
      // source 'GDELT'/'gdelt_query' does not.
      expect(within(results).getByTestId('search-hit-finding-f1')).toBeInTheDocument()
      expect(within(results).queryByTestId('search-hit-source-src.gdelt')).not.toBeInTheDocument()
    })
  })

  it('toggling off the finding facet hides findings', async () => {
    stubFetch()
    render(wrap(<GlobalSearchPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => expect(screen.getByTestId('search-hit-finding-f1')).toBeInTheDocument())

    fireEvent.click(screen.getByTestId('search-facet-finding'))
    await waitFor(() => {
      expect(screen.queryByTestId('search-hit-finding-f1')).not.toBeInTheDocument()
    })
    // signal still present
    expect(screen.getByTestId('search-hit-signal-sig1')).toBeInTheDocument()
  })

  it('survives an endpoint that errors (soft-fail)', async () => {
    const mock = vi.fn((url: string) => {
      if (String(url).includes('/findings')) return Promise.reject(new Error('boom'))
      return Promise.resolve({ ok: true, json: async () => [] })
    })
    vi.stubGlobal('fetch', mock)
    render(wrap(<GlobalSearchPanel registration={reg()} scope={{}} mode="personal" />))
    // Should render the empty state, not crash.
    await waitFor(() => expect(screen.getByTestId('search-results')).toBeInTheDocument())
  })
})
