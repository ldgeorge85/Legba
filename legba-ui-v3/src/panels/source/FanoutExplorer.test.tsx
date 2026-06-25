/**
 * Component test for the fan-out / provenance explorer.
 * Mocks GET /signals (hop 1) + GET /findings (hop 2, joined on derived_from)
 * at the HTTP boundary.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import FanoutExplorerPanel from './FanoutExplorer'
import type { PanelRegistration } from '@/types'
import { useSelection } from '@/state/selection'

function reg(): PanelRegistration {
  return {
    id: 'f1',
    panel_id: 'source_fanout',
    descriptor_id: 'source.fanout',
    descriptor_version: 'v'.repeat(64),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Fan-out Explorer',
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
      produced_at: '2026-06-02T10:00:00Z',
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

// hop 2 — findings whose derived_from cites sig-1 (the provenance edge)
const FINDINGS = {
  data: [
    {
      id: 'find-1',
      title: 'Credibility shift',
      body: 'army movement',
      severity: 'high',
      confidence: 0.82,
      data: {},
      target_id: 'brazil',
      analyst_id: 'cred',
      derived_from: ['sig-1', 'sig-9'],
      produced_at: '2026-06-02T10:05:00Z',
    },
    {
      id: 'find-2',
      title: 'Unrelated finding',
      body: '',
      severity: null,
      confidence: 0.4,
      data: {},
      target_id: 'usa',
      analyst_id: 'cred',
      derived_from: ['sig-7'],
      produced_at: '2026-06-02T10:06:00Z',
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
    if (url.includes('/findings')) return Promise.resolve({ ok: true, json: async () => FINDINGS })
    if (url.includes('/signals')) return Promise.resolve({ ok: true, json: async () => SIGNALS })
    return Promise.resolve({ ok: true, json: async () => ({ data: [], next_cursor: null }) })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

/** Pick a source in the ScopePicker once its options have loaded. */
async function pickSource(value: string) {
  const select = screen.getByTestId('fanout-source-id')
  await waitFor(() => expect(select.querySelector(`option[value="${value}"]`)).toBeInTheDocument())
  fireEvent.change(select, { target: { value } })
}

describe('FanoutExplorerPanel', () => {
  it('shows the empty state with no source', () => {
    mockFetch()
    render(wrap(<FanoutExplorerPanel registration={reg()} scope={{}} mode="personal" />))
    expect(screen.getByTestId('fanout-empty')).toBeInTheDocument()
  })

  it('lists the source signals (hop 1)', async () => {
    mockFetch()
    render(wrap(<FanoutExplorerPanel registration={reg()} scope={{}} mode="personal" />))
    await pickSource('source.rss.brazil')
    await waitFor(() => {
      expect(screen.getByTestId('fanout-signal-sig-1')).toBeInTheDocument()
    })
  })

  it('joins findings on derived_from (hop 2) when a signal is picked', async () => {
    const fetchMock = mockFetch()
    render(wrap(<FanoutExplorerPanel registration={reg()} scope={{}} mode="personal" />))
    await pickSource('source.rss.brazil')
    await waitFor(() => screen.getByTestId('fanout-signal-sig-1'))
    fireEvent.click(screen.getByTestId('fanout-signal-sig-1'))
    await waitFor(() => {
      expect(screen.getByTestId('fanout-node-find-1')).toBeInTheDocument()
    })
    // only findings citing sig-1 in derived_from appear (find-2 cites sig-7).
    expect(screen.queryByTestId('fanout-node-find-2')).not.toBeInTheDocument()
    expect(screen.getByTestId('fanout-summary')).toHaveTextContent('1 finding → brazil')
    // Provenance is reconstructed from /findings, not /lineage.
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/findings'),
      expect.anything(),
    )
    expect(fetchMock).not.toHaveBeenCalledWith(
      expect.stringContaining('/lineage/'),
      expect.anything(),
    )
  })

  it('selects a finding in the shared store when a downstream node is clicked', async () => {
    mockFetch()
    useSelection.getState().clear()
    render(wrap(<FanoutExplorerPanel registration={reg()} scope={{}} mode="personal" />))
    await pickSource('source.rss.brazil')
    await waitFor(() => screen.getByTestId('fanout-signal-sig-1'))
    fireEvent.click(screen.getByTestId('fanout-signal-sig-1'))
    await waitFor(() => screen.getByTestId('fanout-node-find-1'))
    fireEvent.click(screen.getByTestId('fanout-node-find-1'))
    const sel = useSelection.getState().selection
    expect(sel?.kind).toBe('finding')
    expect(sel?.id).toBe('find-1')
  })
})
