/**
 * Component test for the UI-3 Target Graph.
 *
 * cytoscape needs a real canvas/layout that jsdom can't render, so we
 * mock `react-cytoscapejs` to a sentinel and assert on the surrounding
 * chrome: the per-target root picker, the relationship-type filter
 * checkboxes (the core acceptance criterion), and the empty state.
 * The lineage→element projection + rel-type derivation is covered by
 * `lib/graphModel.test.ts`.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'

vi.mock('react-cytoscapejs', () => ({
  default: () => <div data-testid="cytoscape-mock" />,
}))

import TargetGraphPanel from './Graph'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 't1',
    panel_id: 'target_graph',
    descriptor_id: 'brazil',
    descriptor_version: 'v' + 'a'.repeat(63),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Target Graph',
    mode: 'personal',
    layout_slot: 'target.graph.main',
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

function routedFetch(routes: Record<string, unknown>) {
  return vi.fn(async (url: string) => {
    const key = Object.keys(routes).find((k) => url.includes(k))
    return { ok: true, json: async () => routes[key ?? ''] ?? { data: [], next_cursor: null } }
  })
}

const lineageReport = {
  root: {
    id: 'F1',
    row_kind: 'finding',
    title: 'Border buildup',
    produced_at: '2026-06-02T00:00:00Z',
    target_id: 'brazil',
    analyst_id: 'inline.brazil',
    schema_uri: 's',
    depth: 0,
  },
  nodes: [
    { id: 'S1', row_kind: 'signal', title: 'RSS', produced_at: '2026-06-01T00:00:00Z', target_id: 'brazil', analyst_id: null, schema_uri: 's', depth: 1 },
    { id: 'SIT1', row_kind: 'situation', title: 'Escalation', produced_at: '2026-06-03T00:00:00Z', target_id: 'brazil', analyst_id: 'inline.brazil', schema_uri: 's', depth: 1 },
  ],
  edges: [
    { parent: 'S1', child: 'F1' },
    { parent: 'F1', child: 'SIT1' },
  ],
  truncated_at_depth: false,
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('TargetGraphPanel', () => {
  it('shows the empty state when the target has no findings', async () => {
    vi.stubGlobal('fetch', routedFetch({ '/findings': { data: [], next_cursor: null } }))
    render(wrap(<TargetGraphPanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />))
    await waitFor(() => {
      expect(screen.getByTestId('target-graph-empty')).toBeInTheDocument()
    })
  })

  it('auto-roots on the newest finding and renders relationship-type filters', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch({
        '/findings': {
          data: [{ id: 'F1', title: 'Border buildup', severity: 'high', produced_at: '2026-06-02T00:00:00Z' }],
          next_cursor: null,
        },
        '/lineage/': lineageReport,
      }),
    )
    render(wrap(<TargetGraphPanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />))

    // Relationship filters derive from the lineage edges (child kinds:
    // finding + situation).
    await waitFor(() => {
      expect(screen.getByTestId('target-graph-rel-filters')).toBeInTheDocument()
    })
    expect(screen.getByTestId('target-graph-rel-finding')).toBeInTheDocument()
    expect(screen.getByTestId('target-graph-rel-situation')).toBeInTheDocument()
    expect(screen.getByTestId('cytoscape-mock')).toBeInTheDocument()
  })

  it('toggling a relationship-type checkbox flips its checked state', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch({
        '/findings': {
          data: [{ id: 'F1', title: 'Border buildup', severity: 'high', produced_at: '2026-06-02T00:00:00Z' }],
          next_cursor: null,
        },
        '/lineage/': lineageReport,
      }),
    )
    render(wrap(<TargetGraphPanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />))
    const box = (await screen.findByTestId('target-graph-rel-situation')) as HTMLInputElement
    expect(box.checked).toBe(true)
    fireEvent.click(box)
    expect(box.checked).toBe(false)
  })
})
