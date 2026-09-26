/**
 * Component test for Entities (`system.entities`) — U-3 merge: Entity Graph
 * and Notable Structure now live here as tabs. Proves the tab strip actually
 * swaps between the List view and the two ORIGINAL, unmodified components
 * (EntityGraph / NotableStructure) rather than silently dropping one.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import type { PanelRegistration } from '@/types'
import EntitiesPanel from './Entities'
import { emptyMembers, resetScope, useScope } from '@/state/scope'
import { scopeFromTarget } from '@/lib/scopeFromReport'
import { selectRow, useSelection } from '@/state/selection'

function reg(): PanelRegistration {
  return {
    id: 'p', panel_id: 'system_entities', descriptor_id: '(singleton)', descriptor_version: '0'.repeat(64),
    descriptor_family: 'target', analyst_id: null, title: 'Entities', mode: 'personal',
    layout_slot: 'x', data_query: {}, binding: {}, retired: false,
    created_at: '2026-06-03T00:00:00Z', retired_at: null,
  }
}

function wrap(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{ui}</QueryClientProvider>
}

function stubFetch() {
  const mock = vi
    .fn()
    .mockResolvedValue({ ok: true, json: async () => ({ data: [], total: 0, nodes: [], edges: [] }) })
  vi.stubGlobal('fetch', mock)
  return mock
}

/** Every call the panel made, as plain URL strings. */
const urls = (m: ReturnType<typeof stubFetch>) => m.mock.calls.map((c) => String(c[0]))

beforeEach(() => {
  vi.restoreAllMocks()
  resetScope()
  useSelection.getState().clear()
  stubFetch()
})

describe('EntitiesPanel — U-3 tabs', () => {
  it('defaults to the List tab (the original roster)', async () => {
    render(wrap(<EntitiesPanel registration={reg()} scope={{}} mode="personal" />))
    expect(await screen.findByTestId('entities-empty')).toBeInTheDocument()
    expect(screen.getByTestId('entities-tab-list')).toHaveAttribute('aria-selected', 'true')
  })

  it('switching to Graph mounts the original Entity Graph panel', async () => {
    render(wrap(<EntitiesPanel registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('entities-empty')
    fireEvent.click(screen.getByTestId('entities-tab-graph'))
    expect(await screen.findByTestId('entity-graph-canvas')).toBeInTheDocument()
    expect(screen.queryByTestId('entities-empty')).not.toBeInTheDocument()
  })

  it('switching to Structure mounts the original Notable Structure panel', async () => {
    render(wrap(<EntitiesPanel registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('entities-empty')
    fireEvent.click(screen.getByTestId('entities-tab-structure'))
    expect(await screen.findByTestId('notable-structure')).toBeInTheDocument()
    expect(screen.queryByTestId('entities-empty')).not.toBeInTheDocument()
  })

  it('switching back to List remounts the roster', async () => {
    render(wrap(<EntitiesPanel registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('entities-empty')
    fireEvent.click(screen.getByTestId('entities-tab-structure'))
    await screen.findByTestId('notable-structure')
    fireEvent.click(screen.getByTestId('entities-tab-list'))
    expect(await screen.findByTestId('entities-empty')).toBeInTheDocument()
  })
})

/**
 * SCOPE filters, FOCUS highlights (WORKSTATION_V2_FLOW_DESIGN §3.1).
 *
 * Entities-per-report was not servable before this train: `/entities` took only
 * `q`/`entity_class`/`limit`, and `entity` is not a lineage `row_kind`, so a
 * caller holding a report's findings could only list the report's desk TARGETS
 * and call them entities. `finding_id` (repeatable, capped, two provenance
 * hops) is the join that was always in the substrate.
 */
describe('EntitiesPanel — scope filters the roster', () => {
  it('asks for the report’s entities by finding id', async () => {
    const mock = stubFetch()
    useScope.getState().setScope({
      kind: 'report',
      id: 'r1',
      label: 'World read',
      members: { ...emptyMembers(), findingIds: ['f1', 'f2'] },
      origin: 'navigator',
    })
    render(wrap(<EntitiesPanel registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('entities-empty')
    const call = urls(mock).find((u) => u.includes('/entities?'))!
    expect(call).toContain('finding_id=f1')
    expect(call).toContain('finding_id=f2')
  })

  it('caps the ids it sends at the server’s own limit', async () => {
    const mock = stubFetch()
    useScope.getState().setScope({
      kind: 'report',
      id: 'r1',
      label: 'Big read',
      members: {
        ...emptyMembers(),
        findingIds: Array.from({ length: 30 }, (_, i) => `f${i}`),
      },
      origin: 'navigator',
    })
    render(wrap(<EntitiesPanel registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('entities-empty')
    const call = urls(mock).find((u) => u.includes('/entities?'))!
    expect(call.match(/finding_id=/g)!.length).toBe(20)
  })

  it('a DESK scope does not send finding ids — it has none to send', async () => {
    const mock = stubFetch()
    useScope.getState().setScope(scopeFromTarget('country_g20_br', 'Brazil'))
    render(wrap(<EntitiesPanel registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('entities-empty')
    expect(urls(mock).find((u) => u.includes('/entities?'))!).not.toContain('finding_id')
  })

  it('a row click does NOT refilter the roster (focus highlights, it does not filter)', async () => {
    const mock = stubFetch()
    useScope.getState().setScope({
      kind: 'report',
      id: 'r1',
      label: 'World read',
      members: { ...emptyMembers(), findingIds: ['f1'] },
      origin: 'navigator',
    })
    render(wrap(<EntitiesPanel registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('entities-empty')
    const before = urls(mock).filter((u) => u.includes('/entities?')).length

    selectRow('finding', 'some-other-finding', 'elsewhere')
    await screen.findByTestId('entities-empty')

    expect(urls(mock).filter((u) => u.includes('/entities?')).length).toBe(before)
    expect(useScope.getState().scope?.id).toBe('r1')
  })
})
