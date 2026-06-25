/**
 * Component test for the `source.detail` panel.
 * Mocks GET /registry/sources/{id} + GET /signals at the HTTP boundary.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import SourceDetailPanel from './SourceDetail'
import type { PanelRegistration } from '@/types'
import { useSelection } from '@/state/selection'

function reg(): PanelRegistration {
  return {
    id: 'd1',
    panel_id: 'source_detail',
    descriptor_id: 'source.detail',
    descriptor_version: 'v'.repeat(64),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Source Detail',
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

const SOURCE = {
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
  body: {
    cadence: { schedule: '*/15 * * * *' },
    output: { retention: 'interest', delivery: 'lossy', max_age_seconds: 86400 },
  },
}

const SIGNALS = {
  data: [
    {
      id: 'sig-1',
      data: {},
      title: 'Brazil headline',
      source_id: null,
      source_url: '',
      guid: 'g1',
      category: 'text',
      event_timestamp: null,
      language: 'pt',
      confidence: 0.9,
      classification_scores: null,
      target_id: null,
      analyst_id: null,
      produced_at: new Date(Date.now() - 60_000).toISOString(),
      derived_from: [],
      schema_uri: 'legba/signal/1.0.0',
      descriptor_source_id: 'source.rss.brazil',
      geo: ['BR'],
      tags: [],
      entity_classes: [],
    },
  ],
  next_cursor: null,
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

// ScopePicker (source id selector) is backed by /registry/descriptors.
const DESCRIPTORS = [{ descriptor_id: 'source.rss.brazil', name: 'Brazil RSS', state: 'active' }]

function mockFetch() {
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    if (url.includes('/registry/descriptors'))
      return Promise.resolve({ ok: true, json: async () => DESCRIPTORS })
    if (url.includes('/signals')) return Promise.resolve({ ok: true, json: async () => SIGNALS })
    return Promise.resolve({ ok: true, json: async () => SOURCE })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

/** Pick a source in the ScopePicker once its options have loaded. */
async function pickSource(value: string) {
  const select = screen.getByTestId('source-detail-id')
  await waitFor(() => expect(select.querySelector(`option[value="${value}"]`)).toBeInTheDocument())
  fireEvent.change(select, { target: { value } })
}

describe('SourceDetailPanel', () => {
  it('shows the empty state with no source selected', () => {
    mockFetch()
    render(wrap(<SourceDetailPanel registration={reg()} scope={{}} mode="personal" />))
    expect(screen.getByTestId('source-detail-empty')).toBeInTheDocument()
  })

  it('renders descriptor + health + stream + signals once an id is selected', async () => {
    mockFetch()
    render(wrap(<SourceDetailPanel registration={reg()} scope={{}} mode="personal" />))
    await pickSource('source.rss.brazil')
    await waitFor(() => {
      expect(screen.getByTestId('source-detail-body')).toBeInTheDocument()
    })
    expect(screen.getByTestId('source-detail-health')).toHaveTextContent(/last published/)
    expect(screen.getByTestId('source-detail-stream')).toHaveTextContent(
      'source.source.rss.brazil.signals',
    )
    expect(screen.getByTestId('source-detail-signals')).toHaveTextContent('Brazil headline')
  })

  it('shows the geo fan-out facet derived from the signals', async () => {
    mockFetch()
    render(wrap(<SourceDetailPanel registration={reg()} scope={{}} mode="personal" />))
    await pickSource('source.rss.brazil')
    await waitFor(() => {
      // signals are target-agnostic — fan-out is rostered by the geo facet
      expect(screen.getByTestId('source-detail-consumers')).toHaveTextContent('BR')
    })
  })

  it('follows a shared-store source selection', async () => {
    mockFetch()
    useSelection.getState().clear()
    render(wrap(<SourceDetailPanel registration={reg()} scope={{}} mode="personal" />))
    useSelection.getState().select({ kind: 'source', id: 'source.rss.brazil' })
    await waitFor(() => {
      expect(screen.getByTestId('source-detail-id')).toHaveValue('source.rss.brazil')
    })
    await waitFor(() => expect(screen.getByTestId('source-detail-body')).toBeInTheDocument())
  })
})
