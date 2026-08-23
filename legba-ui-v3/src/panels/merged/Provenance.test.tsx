/**
 * Component test for the merged Provenance (`system.provenance`) — proves the
 * tab strip actually swaps between the FIVE original, unmodified
 * implementations rather than silently dropping one: the U-3 trio (Why /
 * Lineage / Flow) plus the two GLASS-2 surfaces that landed here as tabs
 * instead of spending a sidebar row (Trajectory / Narratives).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import type { PanelRegistration } from '@/types'
import { useSelection } from '@/state/selection'
import ProvenanceMerged from './Provenance'

function reg(): PanelRegistration {
  return {
    id: 'p', panel_id: 'system_provenance', descriptor_id: '(singleton)', descriptor_version: '0'.repeat(64),
    descriptor_family: 'target', analyst_id: null, title: 'Provenance', mode: 'personal',
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
    vi.fn().mockResolvedValue({ ok: true, json: async () => ({ data: [], nodes: [], edges: [] }) }),
  )
}

beforeEach(() => {
  vi.restoreAllMocks()
  useSelection.getState().clear()
  stubFetch()
})

describe('ProvenanceMerged', () => {
  it('defaults to the Why tab (the node-picker empty state, nothing selected)', async () => {
    render(wrap(<ProvenanceMerged registration={reg()} scope={{}} mode="personal" />))
    expect(await screen.findByTestId('why-findings-search')).toBeInTheDocument()
    expect(screen.getByTestId('provenance-tab-why')).toHaveAttribute('aria-selected', 'true')
  })

  it('switching to Lineage mounts the original lineage-walk panel', async () => {
    render(wrap(<ProvenanceMerged registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('why-findings-search')
    fireEvent.click(screen.getByTestId('provenance-tab-lineage'))
    expect(await screen.findByTestId('lineage-row-id')).toBeInTheDocument()
    expect(screen.queryByTestId('why-findings-search')).not.toBeInTheDocument()
  })

  it('switching to Flow mounts the original registry-graph canvas (empty or loading text)', async () => {
    render(wrap(<ProvenanceMerged registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('why-findings-search')
    fireEvent.click(screen.getByTestId('provenance-tab-flow'))
    expect(
      await screen.findByText(/no descriptors registered|projecting the registry graph/i),
    ).toBeInTheDocument()
    expect(screen.queryByTestId('why-findings-search')).not.toBeInTheDocument()
  })

  it('switching to Trajectory mounts the situation-register surface (GLASS-2)', async () => {
    render(wrap(<ProvenanceMerged registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('why-findings-search')
    fireEvent.click(screen.getByTestId('provenance-tab-trajectory'))
    expect(await screen.findByTestId('trajectory-frames')).toBeInTheDocument()
    expect(screen.queryByTestId('why-findings-search')).not.toBeInTheDocument()
  })

  it('switching to Narratives mounts the contested-claim surface with its honesty note (GLASS-2)', async () => {
    render(wrap(<ProvenanceMerged registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('why-findings-search')
    fireEvent.click(screen.getByTestId('provenance-tab-narratives'))
    expect(await screen.findByTestId('narratives-honesty-note')).toHaveTextContent(
      /NOT a causal or coordination claim/,
    )
    expect(screen.queryByTestId('trajectory-frames')).not.toBeInTheDocument()
  })
})
