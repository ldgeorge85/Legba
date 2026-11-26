/**
 * Component test for the merged Provenance (`system.provenance`) — proves the
 * tab strip actually swaps between the THREE original, unmodified
 * implementations rather than silently dropping one: Why, Lineage, and the
 * Fan-out Explorer UI-B1/merges folded in (UI-D1 Table A "Evidence: Why ·
 * Lineage · Fan-out"). Flow, Trajectory and Narratives LEFT (the Registry's
 * Canvas, the Register's two tabs) — a stale saved tab naming one falls back
 * to Why, never to an empty surface.
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

  it('carries exactly Why · Lineage · Fan-out', () => {
    render(wrap(<ProvenanceMerged registration={reg()} scope={{}} mode="personal" />))
    for (const id of ['why', 'lineage', 'fanout']) expect(screen.getByTestId(`provenance-tab-${id}`)).toBeInTheDocument()
    for (const gone of ['flow', 'trajectory', 'narratives']) {
      expect(screen.queryByTestId(`provenance-tab-${gone}`)).not.toBeInTheDocument()
    }
  })

  it('switching to Lineage mounts the original lineage-walk panel', async () => {
    render(wrap(<ProvenanceMerged registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('why-findings-search')
    fireEvent.click(screen.getByTestId('provenance-tab-lineage'))
    expect(await screen.findByTestId('lineage-row-id')).toBeInTheDocument()
    expect(screen.queryByTestId('why-findings-search')).not.toBeInTheDocument()
  })

  it('switching to Fan-out mounts the original Fan-out Explorer', async () => {
    render(wrap(<ProvenanceMerged registration={reg()} scope={{}} mode="personal" />))
    await screen.findByTestId('why-findings-search')
    fireEvent.click(screen.getByTestId('provenance-tab-fanout'))
    expect(await screen.findByTestId('fanout-empty')).toBeInTheDocument()
    expect(screen.queryByTestId('why-findings-search')).not.toBeInTheDocument()
  })

  it('a deep link onto Fan-out (source.fanout\'s alias tab) opens there', async () => {
    render(wrap(<ProvenanceMerged registration={reg()} scope={{}} mode="personal" initialTab="fanout" />))
    expect(await screen.findByTestId('fanout-empty')).toBeInTheDocument()
    expect(screen.getByTestId('provenance-tab-fanout')).toHaveAttribute('aria-selected', 'true')
  })

  it('a stale saved tab from before the re-cut (flow / trajectory) falls back to Why', async () => {
    render(wrap(<ProvenanceMerged registration={reg()} scope={{}} mode="personal" initialTab="trajectory" />))
    expect(await screen.findByTestId('why-findings-search')).toBeInTheDocument()
  })
})
