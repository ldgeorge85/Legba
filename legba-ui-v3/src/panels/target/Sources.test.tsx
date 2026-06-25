/**
 * Component + unit test for the UI-3 Target Sources panel (per-source rollup
 * computed from the target's signals, left-joined with the source registry).
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import TargetSourcesPanel, {
  rollupSources,
  type SignalForRollup,
  type SourceDescriptor,
} from './Sources'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 't1',
    panel_id: 'target_sources',
    descriptor_id: 'brazil',
    descriptor_version: 'v' + 'a'.repeat(63),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Target Sources',
    mode: 'personal',
    layout_slot: 'target.sources.main',
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

describe('rollupSources', () => {
  const NOW = Date.parse('2026-06-04T12:00:00Z')

  const signals: SignalForRollup[] = [
    {
      source_id: 'source.aljazeera.world',
      produced_at: '2026-06-04T11:00:00Z', // within 24h
      data: { geo: { country: 'Russia' } },
    },
    {
      source_id: 'source.aljazeera.world',
      produced_at: '2026-06-01T11:00:00Z', // older than 24h
      geo: [],
      data: null,
    },
    {
      source_id: 'source.unknown.feed',
      produced_at: '2026-06-04T10:00:00Z',
      geo: ['ZA'],
    },
  ]

  const descriptors: SourceDescriptor[] = [
    {
      descriptor_id: 'source.aljazeera.world',
      name: 'Al Jazeera — All News',
      body: { identity: { id: 'source.aljazeera.world', kind: 'rss' } },
    },
  ]

  it('groups by source_id, counts 24h vs total, geocoded, and latest', () => {
    const out = rollupSources(signals, descriptors, NOW)
    // Busiest first.
    expect(out[0].source_id).toBe('source.aljazeera.world')
    const al = out[0]
    expect(al.total).toBe(2)
    expect(al.last24h).toBe(1)
    expect(al.geocoded).toBe(1)
    expect(al.latest).toBe('2026-06-04T11:00:00Z')
    expect(al.name).toBe('Al Jazeera — All News')
    expect(al.kind).toBe('rss')
  })

  it('left-joins as unregistered when no descriptor matches', () => {
    const out = rollupSources(signals, descriptors, NOW)
    const unk = out.find((r) => r.source_id === 'source.unknown.feed')!
    expect(unk.name).toBeNull()
    expect(unk.kind).toBeNull()
    expect(unk.geocoded).toBe(1) // geo:['ZA'] counts
  })
})

describe('TargetSourcesPanel', () => {
  it('shows the empty state when there are no signals', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, json: async () => ({ data: [], next_cursor: null }) })),
    )
    render(wrap(<TargetSourcesPanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />))
    await waitFor(() => {
      expect(screen.getByTestId('target-sources-empty')).toBeInTheDocument()
    })
  })

  it('renders a per-source rollup row', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url.includes('/registry/descriptors')) {
          return {
            ok: true,
            json: async () => [
              {
                descriptor_id: 'source.aljazeera.world',
                name: 'Al Jazeera — All News',
                body: { identity: { id: 'source.aljazeera.world', kind: 'rss' } },
              },
            ],
          }
        }
        return {
          ok: true,
          json: async () => ({
            data: [
              {
                source_id: 'source.aljazeera.world',
                produced_at: new Date().toISOString(),
                data: { geo: { country: 'Russia' } },
              },
            ],
            next_cursor: null,
          }),
        }
      }),
    )
    render(wrap(<TargetSourcesPanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />))
    await waitFor(() => {
      expect(screen.getByTestId('target-source-row-source.aljazeera.world')).toBeInTheDocument()
    })
    expect(screen.getByText('Al Jazeera — All News')).toBeInTheDocument()
    // 100% geocoded.
    expect(screen.getByTestId('target-source-geo-source.aljazeera.world')).toHaveTextContent('100%')
  })
})
