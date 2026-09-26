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
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import { useSelection } from '@/state/selection'

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
  useSelection.getState().clear()
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

/**
 * Re-rooting from the shared selection store.
 *
 * The live bug: the effect re-rooted on EVERY selection kind and ran the raw
 * kind through `toRowKind`, which coerces anything it doesn't know to
 * `'finding'`. The selection store's kinds are the room's kinds — `target`,
 * `entity`, `source`, `analyst` — none of which have a lineage table, so
 * selecting a country desk fired `GET /lineage/finding/<target-uuid>`: a 404
 * per click, and the operator's working root thrown away to get it.
 */
describe('TargetGraphPanel selection re-rooting', () => {
  const TARGET_UUID = '7c9e6679-7425-40de-944b-e07fc1f90ae7'

  function trackingFetch(routes: Record<string, unknown>) {
    const urls: string[] = []
    const fn = vi.fn(async (url: string) => {
      urls.push(url)
      const key = Object.keys(routes).find((k) => url.includes(k))
      return { ok: true, json: async () => routes[key ?? ''] ?? { data: [], next_cursor: null } }
    })
    return { fn, urls }
  }

  /** Let effects + react-query settle so a NEGATIVE assertion means something. */
  async function settle() {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0))
    })
  }

  function renderPanel() {
    render(
      wrap(<TargetGraphPanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />),
    )
  }

  it.each(['target', 'entity', 'source', 'analyst'])(
    'does NOT walk lineage when the selection kind is %s',
    async (kind) => {
      const { fn, urls } = trackingFetch({ '/findings': { data: [], next_cursor: null } })
      vi.stubGlobal('fetch', fn)
      renderPanel()
      await waitFor(() => {
        expect(screen.getByTestId('target-graph-empty')).toBeInTheDocument()
      })

      act(() => {
        useSelection.getState().select({ kind: kind as 'target', id: TARGET_UUID, label: 'Brazil' })
      })
      await settle()

      expect(urls.filter((u) => u.includes('/lineage/'))).toEqual([])
    },
  )

  it('keeps the root it already has when an unwalkable kind is selected', async () => {
    const { fn, urls } = trackingFetch({
      '/findings': {
        data: [{ id: 'F1', title: 'Border buildup', severity: 'high', produced_at: '2026-06-02T00:00:00Z' }],
        next_cursor: null,
      },
      '/lineage/': lineageReport,
    })
    vi.stubGlobal('fetch', fn)
    renderPanel()
    // Auto-rooted on the newest finding.
    await waitFor(() => {
      expect(urls.some((u) => u.includes('/lineage/finding/F1'))).toBe(true)
    })

    act(() => {
      useSelection.getState().select({ kind: 'target', id: TARGET_UUID, label: 'Brazil' })
    })
    await settle()

    // No SECOND walk, and above all not one rooted at the target's uuid.
    expect(urls.filter((u) => u.includes('/lineage/')).length).toBe(1)
    expect(urls.some((u) => u.includes(TARGET_UUID))).toBe(false)
  })

  it('DOES walk lineage when a finding is selected', async () => {
    const { fn, urls } = trackingFetch({
      '/findings': { data: [], next_cursor: null },
      '/lineage/': lineageReport,
    })
    vi.stubGlobal('fetch', fn)
    renderPanel()
    await waitFor(() => {
      expect(screen.getByTestId('target-graph-empty')).toBeInTheDocument()
    })

    act(() => {
      useSelection.getState().select({ kind: 'finding', id: 'F9', label: 'Border buildup' })
    })

    await waitFor(() => {
      expect(urls.some((u) => u.includes('/lineage/finding/F9'))).toBe(true)
    })
  })

  it('DOES walk lineage for a coerced kind carried on instanceKey', async () => {
    // `selectRow` stashes the true substrate kind on `instanceKey` when the
    // store coerced it (hypothesis→finding). That kind IS rootable, so the
    // guard must read the instanceKey, not just the coerced union member.
    const { fn, urls } = trackingFetch({
      '/findings': { data: [], next_cursor: null },
      '/lineage/': lineageReport,
    })
    vi.stubGlobal('fetch', fn)
    renderPanel()
    await waitFor(() => {
      expect(screen.getByTestId('target-graph-empty')).toBeInTheDocument()
    })

    act(() => {
      useSelection.getState().select({ kind: 'finding', id: 'H1', instanceKey: 'hypothesis' })
    })

    await waitFor(() => {
      expect(urls.some((u) => u.includes('/lineage/hypothesis/H1'))).toBe(true)
    })
  })
})
