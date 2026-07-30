/**
 * Component test for the UI-3 Target Situations (rebuilt against the frozen
 * `/situations` page shape).
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactElement } from 'react'
import TargetSituationsPanel from './Situations'
import type { PanelRegistration } from '@/types'
import { mockErrorResponse } from '@/test/apiMocks'

function reg(): PanelRegistration {
  return {
    id: 't1',
    panel_id: 'target_situations',
    descriptor_id: 'brazil',
    descriptor_version: 'v' + 'a'.repeat(63),
    descriptor_family: 'target',
    analyst_id: null,
    title: 'Target Situations',
    mode: 'personal',
    layout_slot: 'target.situations.main',
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

function situation(over: Record<string, unknown> = {}) {
  return {
    id: 'sit1',
    name: 'Border escalation',
    status: 'escalating',
    category: 'conflict',
    last_event_at: '2026-06-03T00:00:00Z',
    event_count: 5,
    intensity_score: 0.82,
    target_id: 'brazil',
    analyst_id: 'inline.brazil',
    produced_at: '2026-06-01T00:00:00Z',
    derived_from: ['fa11dead-beef', 'fb22dead-beef'],
    created_at: '2026-06-01T00:00:00Z',
    updated_at: '2026-06-03T00:00:00Z',
    data: {},
    ...over,
  }
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('TargetSituationsPanel', () => {
  it('shows the empty state when there are no situations', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, json: async () => ({ data: [], next_cursor: null }) })),
    )
    render(wrap(<TargetSituationsPanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />))
    await waitFor(() => {
      expect(screen.getByTestId('target-situations-empty')).toBeInTheDocument()
    })
  })

  it('buckets situations by status and expands to show finding links', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, json: async () => ({ data: [situation()], next_cursor: null }) })),
    )
    render(wrap(<TargetSituationsPanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />))
    await waitFor(() => {
      expect(screen.getByTestId('target-situations-bucket-escalating')).toBeInTheDocument()
    })
    expect(screen.getByText('Border escalation')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('target-situation-row-sit1'))
    await waitFor(() => {
      expect(screen.getByText(/contributing findings \(2\)/)).toBeInTheDocument()
    })
    // Intensity meter renders on expand (score 0.82 → 82%).
    expect(screen.getByTestId('target-situation-intensity')).toHaveTextContent('82%')
  })

  it('degrades to the empty state on a 404', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => mockErrorResponse(404, { detail: 'nope' })),
    )
    render(wrap(<TargetSituationsPanel registration={reg()} scope={{ target_id: 'brazil' }} mode="personal" />))
    await waitFor(() => {
      expect(screen.getByTestId('target-situations-empty')).toBeInTheDocument()
    })
  })
})
