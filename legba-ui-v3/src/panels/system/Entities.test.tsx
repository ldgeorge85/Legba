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
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({ ok: true, json: async () => ({ data: [], total: 0, nodes: [], edges: [] }) }),
  )
}

beforeEach(() => {
  vi.restoreAllMocks()
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
