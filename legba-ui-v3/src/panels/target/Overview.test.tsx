/**
 * Component test for TargetOverviewPanel — source-first version.
 *
 * The panel reads the live runtime endpoints (GET /targets/{id}/runtime +
 * /signals + /findings); the old /rollup contract was retired in the pivot.
 * These tests cover the 404-soft-fail path and the descriptor-render path.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import TargetOverviewPanel from './Overview'
import type { PanelRegistration } from '@/types'

function reg(overrides: Partial<PanelRegistration> = {}): PanelRegistration {
  return {
    id: 't1',
    panel_id: 'target_overview',
    descriptor_id: 'brazil',
    descriptor_version: 'v' + 'a'.repeat(63),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Brazil Overview',
    mode: 'personal',
    layout_slot: 'dashboard.brazil.overview',
    data_query: {},
    binding: { target_id: 'brazil' },
    retired: false,
    created_at: '2026-05-20T00:00:00Z',
    retired_at: null,
    ...overrides,
  }
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

/** Route a stubbed fetch by URL fragment; unmatched → 404. */
function routedFetch(routes: Record<string, unknown>) {
  return vi.fn((url: string) => {
    const u = String(url)
    for (const [frag, body] of Object.entries(routes)) {
      if (u.includes(frag)) {
        return Promise.resolve({ ok: true, status: 200, json: async () => body })
      }
    }
    return Promise.resolve({ ok: false, status: 404, json: async () => ({}) })
  })
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('TargetOverviewPanel', () => {
  it('renders gracefully when the runtime endpoints 404', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: false, status: 404, json: async () => ({}) }),
    )
    render(
      wrap(<TargetOverviewPanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />),
    )
    await waitFor(() => {
      expect(screen.getByText(/no descriptor row/i)).toBeInTheDocument()
    })
  })

  it('renders the descriptor when /targets/{id}/runtime returns data', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch({
        '/targets/brazil/runtime': {
          descriptor_id: 'brazil',
          active_descriptor: {
            descriptor_id: 'brazil',
            version: 'a'.repeat(64),
            schema_uri: 'iglu:legba/target/jsonschema/3-0-0',
            state: 'active',
            name: 'Brazil',
            abstraction_level: 'L1',
            source_count: 3,
          },
          actors: [],
        },
        '/signals': { data: [] },
        '/findings': { data: [] },
      }),
    )
    render(
      wrap(<TargetOverviewPanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />),
    )
    await waitFor(() => {
      expect(screen.getByText('Brazil')).toBeInTheDocument()
    })
    expect(screen.getByText('active')).toBeInTheDocument()
  })
})
