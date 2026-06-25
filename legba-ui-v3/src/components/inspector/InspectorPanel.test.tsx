/**
 * Component test for the Inspector (redesign Move 1 — the keystone).
 *
 * Verifies the selection→detail contract: a finding selection loads its lineage
 * (the reused Why fetch), renders the header / core / body / DERIVED-FROM refs,
 * and that clicking a ref RecordLink re-selects (drill-through). Empty selection
 * renders the world-assessment one-pager (never dead space).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import InspectorPanel from './InspectorPanel'
import { useSelection } from '@/state/selection'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 'inspector',
    panel_id: 'system_inspector',
    descriptor_id: '(singleton)',
    descriptor_version: '00000000',
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Inspector',
    mode: 'personal',
    layout_slot: 'system.inspector',
    data_query: {},
    binding: {},
    retired: false,
    created_at: new Date().toISOString(),
    retired_at: null,
  }
}

function wrap(ui: ReactElement): ReactElement {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

const FINDING_LINEAGE = {
  root: {
    id: 'f1',
    row_kind: 'finding',
    title: 'Port closure, Santos',
    produced_at: '2026-06-16T09:12:00Z',
    target_id: 'brazil',
    analyst_id: 'country_assessor',
    schema_uri: 'legba/finding/v1',
    depth: 0,
  },
  nodes: [
    {
      id: 'sig-7',
      row_kind: 'signal',
      title: 'USGS feed item',
      produced_at: '2026-06-16T08:00:00Z',
      target_id: null,
      analyst_id: null,
      schema_uri: 'legba/signal/v1',
      depth: 1,
    },
  ],
  edges: [{ parent: 'sig-7', child: 'f1' }],
}

/** Route fetch by URL so the lineage walk + assessment poll both resolve. */
function mockFetch() {
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    if (url.includes('/lineage/finding/f1')) {
      return Promise.resolve({ ok: true, json: async () => FINDING_LINEAGE })
    }
    if (url.includes('/lineage/')) {
      return Promise.resolve({ ok: true, json: async () => FINDING_LINEAGE })
    }
    // world-assessment poll + anything else → empty.
    return Promise.resolve({ ok: true, json: async () => ({ data: [] }) })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('InspectorPanel', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    localStorage.clear()
    useSelection.getState().clear()
  })

  it('renders a call-to-action when nothing is selected (#90: assessment is a finding, not the empty-state)', async () => {
    mockFetch()
    render(wrap(<InspectorPanel registration={reg()} scope={{}} mode="personal" />))
    // Empty state = a plain CTA. The world assessment is no longer special-cased
    // here — it's a finding read in the Inspector like any other when selected.
    await waitFor(() =>
      expect(screen.queryByTestId('inspector-empty')).toBeInTheDocument(),
    )
  })

  it('loads a finding selection: header, body, and DERIVED-FROM ref', async () => {
    mockFetch()
    render(wrap(<InspectorPanel registration={reg()} scope={{}} mode="personal" />))
    useSelection.getState().select({ kind: 'finding', id: 'f1', label: 'Port closure', origin: 'feed' })

    // The lineage root title surfaces as the body title.
    await waitFor(() => expect(screen.getByTestId('inspector-refs')).toBeInTheDocument())

    // DERIVED FROM lists the parent signal as a RecordLink.
    const refs = screen.getByTestId('inspector-refs')
    expect(refs).toHaveTextContent('USGS feed item')

    // The Inspector body carries the resolved record id in its header.
    const body = screen.getByTestId('inspector-body')
    expect(body).toHaveTextContent('f1')
  })

  it('clicking a DERIVED-FROM ref re-selects (drill-through)', async () => {
    mockFetch()
    render(wrap(<InspectorPanel registration={reg()} scope={{}} mode="personal" />))
    useSelection.getState().select({ kind: 'finding', id: 'f1', label: 'Port closure' })
    await waitFor(() => expect(screen.getByTestId('inspector-refs')).toBeInTheDocument())

    // The ref link selects the upstream signal.
    const link = screen.getByTestId('inspector-refs').querySelector('[data-testid="record-link"]')!
    fireEvent.click(link)

    const sel = useSelection.getState().selection
    expect(sel?.kind).toBe('signal')
    expect(sel?.id).toBe('sig-7')
    // Drilling pushed the finding onto the breadcrumb.
    expect(useSelection.getState().history.map((h) => h.id)).toContain('f1')
  })
})
