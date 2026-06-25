/**
 * Component test for the UI-3 Target Findings panel (rebuilt against the
 * frozen `/findings` page shape with nullable severity + `data.tags`).
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import TargetFindingsPanel from './Findings'
import type { PanelRegistration } from '@/types'

function reg(): PanelRegistration {
  return {
    id: 't1',
    panel_id: 'target_findings',
    descriptor_id: 'brazil',
    descriptor_version: 'v' + 'a'.repeat(63),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Target Findings',
    mode: 'personal',
    layout_slot: 'target.findings.main',
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

function finding(over: Record<string, unknown> = {}) {
  return {
    id: 'f1',
    title: 'Troops massing near the border',
    body: 'Multiple independent sources report movement.',
    severity: null,
    confidence: 0.72,
    target_id: 'brazil',
    analyst_id: 'country_assessor',
    derived_from: ['s1', 's2'],
    produced_at: '2026-06-02T00:00:00Z',
    data: { tags: ['conflict', 'border', 'target:brazil', 'analyst:country_assessor'] },
    ...over,
  }
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('TargetFindingsPanel', () => {
  it('shows the empty state when there are no findings', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, json: async () => ({ data: [], next_cursor: null }) })),
    )
    render(wrap(<TargetFindingsPanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />))
    await waitFor(() => {
      expect(screen.getByTestId('target-findings-empty')).toBeInTheDocument()
    })
  })

  it('renders a finding, badges nullable severity as "unrated", and drops bookkeeping tags', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, json: async () => ({ data: [finding()], next_cursor: null }) })),
    )
    render(wrap(<TargetFindingsPanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />))

    await waitFor(() => {
      expect(screen.getByText('Troops massing near the border')).toBeInTheDocument()
    })
    // Nullable severity → "unrated" badge.
    expect(screen.getByText('unrated')).toBeInTheDocument()

    fireEvent.click(screen.getByText('Troops massing near the border'))
    await waitFor(() => {
      expect(screen.getByText(/evidence chain \(2\)/)).toBeInTheDocument()
    })
    // Topic tags kept; bookkeeping tags dropped.
    const tags = screen.getByTestId('target-finding-tags-f1')
    expect(tags).toHaveTextContent('conflict')
    expect(tags).toHaveTextContent('border')
    expect(tags).not.toHaveTextContent('target:brazil')
    expect(tags).not.toHaveTextContent('analyst:country_assessor')
  })

  it('degrades to the empty state on a 404', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: false, status: 404, json: async () => ({ detail: 'nope' }) })),
    )
    render(wrap(<TargetFindingsPanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />))
    await waitFor(() => {
      expect(screen.getByTestId('target-findings-empty')).toBeInTheDocument()
    })
  })
})
