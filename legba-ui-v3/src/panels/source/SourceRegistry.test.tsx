/**
 * Component test for the `registry.sources` panel.
 * Mocks GET /registry/sources at the HTTP boundary (apiGet → fetch).
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import SourceRegistryPanel from './SourceRegistry'
import type { PanelRegistration } from '@/types'
import type { SourceDescriptorOut } from './sourceTypes'
import { useSelection } from '@/state/selection'

function reg(): PanelRegistration {
  return {
    id: 'r1',
    panel_id: 'registry_sources',
    descriptor_id: 'registry.sources',
    descriptor_version: 'v'.repeat(64),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Source Registry',
    mode: 'personal',
    layout_slot: 'main',
    data_query: {},
    binding: {},
    retired: false,
    created_at: '2026-06-01T00:00:00Z',
    retired_at: null,
  }
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

const SOURCES: SourceDescriptorOut[] = [
  {
    descriptor_id: 'source.rss.brazil',
    version: 'a'.repeat(64),
    schema_uri: 'legba/source/1.0.0',
    is_head: true,
    state: 'active',
    owner: 'op',
    name: 'Brazil RSS',
    abstraction_level: 'L1',
    inherits: [],
    created_at: '2026-06-01T00:00:00Z',
    retire_after: null,
    kind: 'rss',
    acquisition: 'poll',
    subscription_policy: 'open',
    owner_tenant: 'default',
    geo: ['BR'],
    languages: ['pt'],
    tags: ['news'],
    has_discovery: false,
    has_provision: false,
    output_subject: 'source.source.rss.brazil.signals',
    body: { identity: { id: 'source.rss.brazil' }, acquisition: 'poll' },
  },
  {
    descriptor_id: 'source.gdelt.global',
    version: 'b'.repeat(64),
    schema_uri: 'legba/source/1.0.0',
    is_head: true,
    state: 'draft',
    owner: 'op',
    name: 'GDELT Global',
    abstraction_level: 'L1',
    inherits: [],
    created_at: '2026-06-01T00:00:00Z',
    retire_after: null,
    kind: 'gdelt',
    acquisition: 'poll',
    subscription_policy: 'open',
    owner_tenant: 'default',
    geo: [],
    languages: [],
    tags: ['global'],
    has_discovery: true,
    has_provision: false,
    output_subject: 'source.source.gdelt.global.signals',
    body: { identity: { id: 'source.gdelt.global' } },
  },
]

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

function mockSourcesFetch() {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => SOURCES,
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('SourceRegistryPanel', () => {
  it('lists sources from GET /registry/sources', async () => {
    const fetchMock = mockSourcesFetch()
    render(wrap(<SourceRegistryPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => {
      expect(screen.getByTestId('source-row-source.rss.brazil')).toBeInTheDocument()
    })
    expect(screen.getByTestId('source-row-source.gdelt.global')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/registry/sources'),
      expect.anything(),
    )
  })

  it('filters by search query', async () => {
    mockSourcesFetch()
    render(wrap(<SourceRegistryPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => screen.getByTestId('source-row-source.rss.brazil'))
    fireEvent.change(screen.getByTestId('sources-search'), { target: { value: 'gdelt' } })
    expect(screen.queryByTestId('source-row-source.rss.brazil')).not.toBeInTheDocument()
    expect(screen.getByTestId('source-row-source.gdelt.global')).toBeInTheDocument()
  })

  it('expands a row and offers a lifecycle transition for its state', async () => {
    mockSourcesFetch()
    render(wrap(<SourceRegistryPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => screen.getByTestId('source-row-source.gdelt.global'))
    // draft → expand → should offer "→ configured"
    fireEvent.click(
      screen.getByTestId('source-row-source.gdelt.global').querySelector('button')!,
    )
    expect(
      screen.getByTestId('source-transition-source.gdelt.global-configured'),
    ).toBeInTheDocument()
  })

  it('opens the create editor with the starter descriptor', async () => {
    mockSourcesFetch()
    render(wrap(<SourceRegistryPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => screen.getByTestId('source-row-source.rss.brazil'))
    fireEvent.click(screen.getByTestId('sources-new'))
    // DescriptorEditor renders a textarea seeded with the starter source YAML.
    await waitFor(() => {
      const areas = Array.from(document.querySelectorAll('textarea'))
      expect(areas.length).toBeGreaterThan(0)
      expect(areas.some((a) => a.value.includes('source.rss.example'))).toBe(true)
    })
  })

  it('selects the source in the shared store when "open detail" is clicked', async () => {
    mockSourcesFetch()
    useSelection.getState().clear()
    render(wrap(<SourceRegistryPanel registration={reg()} scope={{}} mode="personal" />))
    await waitFor(() => screen.getByTestId('source-row-source.rss.brazil'))
    fireEvent.click(
      screen.getByTestId('source-row-source.rss.brazil').querySelector('button')!,
    )
    fireEvent.click(screen.getByTestId('source-open-detail-source.rss.brazil'))
    const sel = useSelection.getState().selection
    expect(sel?.kind).toBe('source')
    expect(sel?.id).toBe('source.rss.brazil')
  })
})
